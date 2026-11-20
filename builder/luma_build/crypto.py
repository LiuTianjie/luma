"""At-rest credential encryption for luma-build SQLite state.

Runners never store credentials locally; the server decrypts only the single
matching Git token and Registry push credential in memory when a lease is won.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from .errors import BuildError


_PBKDF2_SALT = b"luma-build:secret-box:v2"
_PBKDF2_ITERATIONS = 200_000


def _derive_legacy_fernet_key(raw_secret: str) -> bytes:
    digest = hashlib.sha256(raw_secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _derive_fernet_key(raw_secret: str) -> bytes:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        raw_secret.encode("utf-8"),
        _PBKDF2_SALT,
        _PBKDF2_ITERATIONS,
        dklen=32,
    )
    return base64.urlsafe_b64encode(digest)


class SecretBox:
    """Encrypt and decrypt secrets stored in the luma-build database."""

    def __init__(self, key_bytes: bytes, *, fallback_keys: tuple[bytes, ...] = ()) -> None:
        try:
            from cryptography.fernet import Fernet, MultiFernet
        except ImportError as exc:
            raise BuildError(
                "cryptography is required on the luma-build server; install 'luma-build[server]'"
            ) from exc
        primary = Fernet(key_bytes)
        if fallback_keys:
            self._fernet: Fernet | MultiFernet = MultiFernet([primary, *(Fernet(k) for k in fallback_keys)])
        else:
            self._fernet = primary

    @classmethod
    def from_env_or_file(cls, data_dir: Path) -> SecretBox:
        raw_env = os.environ.get("LUMA_BUILD_MASTER_KEY", "").strip()
        if raw_env:
            if len(raw_env) < 16:
                raise BuildError("LUMA_BUILD_MASTER_KEY must be at least 16 characters")
            return cls(
                _derive_fernet_key(raw_env),
                fallback_keys=(_derive_legacy_fernet_key(raw_env),),
            )

        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            data_dir.chmod(0o700)
        except OSError:
            pass
        key_file = data_dir / "master.key"
        if key_file.is_file():
            key_text = key_file.read_text(encoding="ascii").strip()
            if not key_text:
                raise BuildError(f"empty master key file: {key_file}")
            return cls(key_text.encode("ascii"))

        from cryptography.fernet import Fernet

        generated = Fernet.generate_key()
        tmp = key_file.with_name(f".master.key.{os.getpid()}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(generated + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, key_file)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return cls(generated)

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise BuildError("stored credential could not be decrypted with current master key") from exc
