"""Authenticated encryption for private Control data and migration archives.

The recovery key belongs to the manager host, outside Control's mount tree.
Control obtains the same key over authenticated Nomad transport from an
encrypted Variable; application identities have no permission on that path.
This separates database/config backups from decryption authority. It does not
claim to protect a running manager from its own root administrator.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from .errors import LumaError

KEY_ROOT = Path("/etc/luma/secrets")
KEY_FILE = KEY_ROOT / "control-data.key"
KEY_VARIABLE = "luma/control/storage-key"
FORMAT = "luma.aes256gcm.v1"
_keys: dict[tuple[str, str], tuple[bytes, bytes]] = {}
_key_lock = threading.RLock()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def profile_path() -> Path:
    from .nomad_security import security_root
    return security_root() / "secrets.json"


def profile() -> dict[str, Any]:
    from .nomad_security import read_private_json, security_root
    value = read_private_json(profile_path(), optional=True)
    if not value and read_private_json(security_root() / "cluster.json", optional=True).get("secretsRequired"):
        raise LumaError("encrypted storage profile is missing; restore its private backup")
    if value and (value.get("schema") != 1 or value.get("format") != FORMAT or not value.get("keyId")):
        raise LumaError("invalid encrypted storage profile; refusing plaintext fallback")
    return value


def enabled() -> bool:
    return bool(profile())


def _key_document(path: Path) -> dict[str, Any]:
    from .nomad_security import read_private_json
    value = read_private_json(path)
    if value.get("schema") != 1 or not isinstance(value.get("id"), str):
        raise LumaError("invalid Control recovery key")
    try:
        raw = base64.b64decode(value["key"], validate=True)
    except (ValueError, KeyError, TypeError):
        raise LumaError("invalid Control recovery key") from None
    if len(raw) != 32:
        raise LumaError("invalid Control recovery key")
    return value


def _client(*, administrator: bool = False):
    from .nomad_api import NomadApi
    from .nomad_security import ADMIN_ROOT, nomad_token, private_read
    token = private_read(ADMIN_ROOT / "management.token").strip() if administrator else nomad_token()
    return NomadApi("http://127.0.0.1:4646", token=token)


def provision() -> dict[str, Any]:
    """Host-only provisioning, after ACL and verified TLS are active."""
    from .nomad_security import private_write, read_private_json, security_root
    if os.geteuid() != 0:
        raise LumaError("encrypted storage provisioning requires the manager host administrator")
    if Path("/opt/luma") == KEY_ROOT.resolve() or Path("/opt/luma") in KEY_ROOT.resolve().parents:
        raise LumaError("Control recovery key must remain outside the Control mount tree")
    cluster = read_private_json(security_root() / "cluster.json")
    transport = read_private_json(security_root() / "transport.json")
    if not cluster.get("aclRequired") or not transport.get("http"):
        raise LumaError("encrypted storage requires active Nomad ACLs and verified TLS")
    with _key_lock:
        existing = profile()
        if KEY_FILE.exists():
            document = _key_document(KEY_FILE)
        elif existing:
            # Never replace a missing key with an unrelated newly generated one.
            raise LumaError("Control recovery key is missing; restore its private backup")
        else:
            document = {"schema": 1, "id": secrets.token_hex(16), "key": base64.b64encode(secrets.token_bytes(32)).decode()}
            private_write(KEY_FILE, _json(document) + "\n")
        if existing and existing["keyId"] != document["id"]:
            raise LumaError("Control recovery key does not match the storage profile")
        client = _client(administrator=True)
        from .nomad_secrets import read_variable, validate_workload_acl, write_variable
        validate_workload_acl(client)
        current = read_variable(client, KEY_VARIABLE)
        items = {"id": document["id"], "key": document["key"]}
        if current is not None and current.get("Items") != items:
            raise LumaError("Nomad storage key differs from the private recovery key; no key was overwritten")
        write_variable(client, KEY_VARIABLE, items, previous=current)
        confirmed = read_variable(client, KEY_VARIABLE)
        if confirmed is None or confirmed.get("Items") != items:
            raise LumaError("encrypted storage key could not be verified")
        settings = {"schema": 1, "format": FORMAT, "keyId": document["id"], "keyVariable": KEY_VARIABLE}
        private_write(profile_path(), _json(settings) + "\n")
        cluster["secretsRequired"] = True
        private_write(security_root() / "cluster.json", _json(cluster) + "\n")
        return settings


def _derived_keys(required_id: str | None = None) -> tuple[str, bytes, bytes] | None:
    settings = profile()
    if not settings:
        if required_id:
            raise LumaError("encrypted data requires its storage profile and recovery key")
        return None
    identifier = settings["keyId"]
    if required_id is not None and required_id != identifier:
        raise LumaError("encrypted data requires a different recovery key")
    cache_key = (str(profile_path()), identifier)
    with _key_lock:
        cached = _keys.get(cache_key)
        if cached is not None:
            return identifier, *cached
        if os.geteuid() == 0 and KEY_FILE.exists():
            document = _key_document(KEY_FILE)
        else:
            from .nomad_secrets import read_variable
            variable = read_variable(_client(), KEY_VARIABLE)
            document = (variable or {}).get("Items") or {}
        try:
            material = base64.b64decode(document["key"], validate=True)
            if document["id"] != identifier or len(material) != 32:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise LumaError("Control decryption key is unavailable; refusing plaintext fallback") from None
        derived = HKDF(algorithm=hashes.SHA256(), length=64, salt=None, info=b"luma-control-storage-v1").derive(material)
        keys = (derived[:32], derived[32:])
        _keys[cache_key] = keys
        return identifier, *keys


def context(table: str, *identity: object) -> str:
    return _json([table, *identity])


def opaque_id(value: Any, purpose: str) -> str:
    keys = _derived_keys()
    if keys is None:
        raise LumaError("encrypted storage must be initialized before distributing application secrets")
    return hmac.new(keys[2], _json([purpose, value]).encode(), hashlib.sha256).hexdigest()[:32]


def sealed(value: Any) -> bool:
    return isinstance(value, dict) and value.get("format") == FORMAT


def dumps(value: Any, purpose: str, *, previous: str | None = None, required: bool = False) -> str:
    """Encrypt one JSON value, retaining the old envelope for an unchanged value."""
    clear = _json(value)
    keys = _derived_keys()
    if keys is None:
        if required:
            raise LumaError("encrypted storage is required; refusing a plaintext write")
        return clear
    identifier, encryption_key, digest_key = keys
    # History filters use this allowlisted non-secret field without decrypting
    # every row. It is authenticated along with the row identity.
    origin = value.get("origin", "") if isinstance(value, dict) else ""
    origin = origin if isinstance(origin, str) and len(origin) <= 128 else ""
    aad = _json([FORMAT, identifier, purpose, origin]).encode()
    fingerprint = hmac.new(digest_key, aad + clear.encode(), hashlib.sha256).hexdigest()
    if previous:
        try:
            old = json.loads(previous)
            if (sealed(old) and old.get("keyId") == identifier and old.get("context") == purpose
                    and hmac.compare_digest(str(old.get("fingerprint") or ""), fingerprint)):
                # Verify rather than carrying corrupted ciphertext forward.
                loads(previous, purpose, required=True)
                return previous
        except (ValueError, TypeError):
            pass
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(encryption_key).encrypt(nonce, clear.encode(), aad)
    return _json({"format": FORMAT, "keyId": identifier, "context": purpose, "origin": origin,
                  "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(encrypted).decode(),
                  "fingerprint": fingerprint})


def loads(raw: str, purpose: str, *, required: bool = False) -> Any:
    value = json.loads(raw)
    if not sealed(value):
        if required:
            raise LumaError("unencrypted data found in encrypted storage")
        return value
    if value.get("context") != purpose:
        raise LumaError("encrypted data does not belong to this record")
    keys = _derived_keys(str(value.get("keyId") or ""))
    if keys is None:
        raise LumaError("Control decryption key is unavailable")
    identifier, encryption_key, digest_key = keys
    try:
        nonce = base64.b64decode(value["nonce"], validate=True)
        encrypted = base64.b64decode(value["ciphertext"], validate=True)
        if len(nonce) != 12:
            raise ValueError()
        aad = _json([FORMAT, identifier, purpose, value.get("origin", "")]).encode()
        clear = AESGCM(encryption_key).decrypt(nonce, encrypted, aad)
        expected = hmac.new(digest_key, aad + clear, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(value.get("fingerprint") or "")):
            raise ValueError()
        return json.loads(clear)
    except (ValueError, KeyError, TypeError, InvalidTag, UnicodeError):
        raise LumaError("encrypted data failed authentication; restore a verified backup") from None


def protect_text(value: str, purpose: str) -> str:
    return dumps(value, context("file", purpose), required=True) + "\n"


def read_protected_text(value: str, purpose: str) -> str:
    result = loads(value, context("file", purpose), required=True)
    if not isinstance(result, str):
        raise LumaError("invalid encrypted file")
    return result


def store_credential_file(path: Path, value: str) -> None:
    from .nomad_security import private_write
    payload = dumps(value, context("credential-file", str(path)), required=True) if enabled() else value
    private_write(path, payload)


def read_credential_file(path: Path) -> str:
    from .nomad_security import private_read
    raw = private_read(path)
    try:
        encrypted = sealed(json.loads(raw))
    except ValueError:
        encrypted = False
    if encrypted:
        value = loads(raw, context("credential-file", str(path)), required=True)
        if not isinstance(value, str):
            raise LumaError("invalid encrypted credential file")
        return value
    return raw


def _archive_chunks(source: Path):
    """Yield authenticated archive plaintext; callers publish only after EOF."""
    with source.open("rb") as handle:
        header = handle.readline(4096)
        try:
            settings = json.loads(header)
            if settings.get("format") != "luma.archive.aes256gcm.v1":
                raise ValueError()
            keys = _derived_keys(settings["keyId"])
            if keys is None:
                raise ValueError()
            nonce = base64.b64decode(settings["nonce"], validate=True)
            if len(nonce) != 12:
                raise ValueError()
            length = source.stat().st_size - len(header) - 16
            if length < 0:
                raise ValueError()
            handle.seek(-16, os.SEEK_END)
            decryptor = Cipher(algorithms.AES(keys[1]), modes.GCM(nonce, handle.read(16))).decryptor()
            decryptor.authenticate_additional_data(header)
            handle.seek(len(header))
            while length:
                chunk = handle.read(min(length, 1024 * 1024))
                if not chunk:
                    raise ValueError()
                length -= len(chunk)
                yield decryptor.update(chunk)
            yield decryptor.finalize()
        except (ValueError, KeyError, TypeError, InvalidTag):
            raise LumaError("encrypted archive failed authentication") from None


def verify_archive(source: Path, original: Path | None = None) -> None:
    digest = hashlib.sha256()
    for chunk in _archive_chunks(source):
        digest.update(chunk)
    if original is not None:
        expected = hashlib.sha256()
        with original.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                expected.update(chunk)
        if not hmac.compare_digest(digest.digest(), expected.digest()):
            raise LumaError("encrypted archive differs from its source; source was retained")


def encrypt_file(source: Path, destination: Path | None = None) -> Path:
    """Archive a private file atomically, retaining the source until verified."""
    source = Path(source)
    destination = Path(destination or str(source) + ".enc")
    if source.is_symlink() or destination.is_symlink() or source.resolve() == destination.resolve():
        raise LumaError("unsafe encrypted archive path")
    if destination.exists():
        verify_archive(destination, source)
        return destination
    keys = _derived_keys()
    if keys is None:
        raise LumaError("encrypted archive requires the manager recovery key")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    nonce = secrets.token_bytes(12)
    header = (_json({"format": "luma.archive.aes256gcm.v1", "keyId": keys[0], "nonce": base64.b64encode(nonce).decode()}) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=".luma-encrypt-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as original:
            os.fchmod(output.fileno(), 0o600)
            output.write(header)
            encryptor = Cipher(algorithms.AES(keys[1]), modes.GCM(nonce)).encryptor()
            encryptor.authenticate_additional_data(header)
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                output.write(encryptor.update(chunk))
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())
        verify_archive(temporary, source)
        os.replace(temporary, destination)
        from .control.database import _fsync_directory
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def decrypt_file(source: Path, destination: Path) -> None:
    """Explicit offline recovery; never overwrite an existing destination."""
    if destination.exists() or destination.is_symlink() or source.is_symlink():
        raise LumaError("backup restore destination must be a new private file")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".luma-decrypt-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            for chunk in _archive_chunks(source):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, destination)
        from .control.database import _fsync_directory
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Private Control storage key and backup recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("provision")
    store = commands.add_parser("store-credential")
    store.add_argument("path", type=Path)
    restore = commands.add_parser("decrypt-backup")
    restore.add_argument("source", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    if args.command == "provision":
        provision()
        print("Encrypted Control storage key provisioned")
    elif args.command == "store-credential":
        import sys
        value = sys.stdin.read(65537)
        if len(value.encode()) > 65536:
            raise LumaError("credential exceeds the supported size")
        store_credential_file(args.path, value)
        print("Private credential stored")
    else:
        decrypt_file(args.source, args.destination)
        print("Private backup restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
