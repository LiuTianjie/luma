"""Host-owned Nomad CA and automatically provisioned, file-backed TLS identities.

Only the manager's root supervisor can sign. Control can request a *client*
identity for an enrolled name/address, but cannot read the CA private key.
Private keys and tokens never enter agent task records or Nomad job JSON.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .errors import LumaError
from .nomad_security import ADMIN_ROOT, ROLES, mesh_ip, private_read, private_write, read_private_json, security_root

CERT_DAYS = 365
RENEW_DAYS = 30


def tls_paths(root: Path) -> dict[str, str]:
    return {"ca_file": str(root / "ca.pem"), "cert_file": str(root / "cert.pem"), "key_file": str(root / "key.pem")}


def _pem(value: Any) -> str:
    return value.public_bytes(serialization.Encoding.PEM).decode()


def _key(path: Path):
    saved = private_read(path, optional=True)
    if saved:
        return serialization.load_pem_private_key(saved.encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    private_write(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    return key


def ensure_ca(admin_root: Path = ADMIN_ROOT) -> tuple[Any, x509.Certificate]:
    root = admin_root / "ca"
    key = _key(root / "key.pem")
    saved = private_read(root / "cert.pem", optional=True)
    if saved:
        cert = x509.load_pem_x509_certificate(saved.encode())
        if cert.public_key().public_numbers() != key.public_key().public_numbers():
            raise LumaError("Nomad CA key and certificate do not match; refusing to replace the CA")
        if cert.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=CERT_DAYS):
            raise LumaError("Nomad CA expires too soon; perform a reviewed CA rotation")
        return key, cert
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Luma Nomad CA " + uuid.uuid4().hex)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                        data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                                        encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    private_write(root / "cert.pem", _pem(cert))
    return key, cert


def create_csr(root: Path = ADMIN_ROOT / "tls") -> str:
    key = _key(root / "key.pem")
    return _pem(x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Luma Nomad pending identity")])
    ).sign(key, hashes.SHA256()))


def sign_csr(csr_pem: str, *, role: str, address: str, admin_root: Path = ADMIN_ROOT) -> dict[str, str]:
    if role not in {"server", "client", *ROLES}:
        raise LumaError("invalid Nomad TLS role")
    address = mesh_ip(address)
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        public_key = csr.public_key()
        if not csr.is_signature_valid or not isinstance(public_key, ec.EllipticCurvePublicKey) or public_key.curve.name != "secp256r1":
            raise ValueError("invalid CSR")
    except (ValueError, TypeError):
        raise LumaError("invalid Nomad TLS certificate request") from None
    key, ca = ensure_ca(admin_root)
    # Names/extensions come from the trusted installer, never from the CSR.
    agent = role in {"server", "client"}
    name = f"{role}.global.nomad" if agent else "luma-" + role
    usages = [ExtendedKeyUsageOID.CLIENT_AUTH]
    if agent:
        usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
    names: list[Any] = [x509.DNSName(name)]
    if agent:
        names += [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.IPAddress(ipaddress.ip_address(address))]
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca.subject).public_key(public_key).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=CERT_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                        data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                                        encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage(usages), critical=True)
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .sign(key, hashes.SHA256()))
    return {"ca": _pem(ca), "cert": _pem(cert), "expiresAt": cert.not_valid_after_utc.isoformat()}


def install_certificate(bundle: Mapping[str, Any], *, root: Path = ADMIN_ROOT / "tls") -> None:
    """Install a response only if it matches the locally generated private key."""
    try:
        ca = x509.load_pem_x509_certificate(str(bundle["ca"]).encode())
        cert = x509.load_pem_x509_certificate(str(bundle["cert"]).encode())
        cert.verify_directly_issued_by(ca)
        if cert.public_key().public_numbers() != _key(root / "key.pem").public_key().public_numbers():
            raise ValueError("key mismatch")
        if cert.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=1):
            raise ValueError("expired certificate")
        old_ca = private_read(root / "ca.pem", optional=True)
        if old_ca and old_ca.strip() != str(bundle["ca"]).strip():
            raise ValueError("unexpected CA replacement")
    except (KeyError, ValueError, TypeError):
        raise LumaError("Nomad TLS certificate verification failed; existing identity was retained") from None
    private_write(root / "ca.pem", str(bundle["ca"]))
    private_write(root / "cert.pem", str(bundle["cert"]))


def expires_soon(root: Path) -> bool:
    saved = private_read(root / "cert.pem", optional=True)
    if not saved:
        return True
    cert = x509.load_pem_x509_certificate(saved.encode())
    return cert.not_valid_after_utc < datetime.now(timezone.utc) + timedelta(days=RENEW_DAYS)


def prepare_manager(address: str, *, admin_root: Path = ADMIN_ROOT, service_root: Path | None = None) -> dict[str, str]:
    service_root = service_root or security_root()
    ensure_ca(admin_root)
    for role in ("server", *ROLES):
        root = admin_root / "tls" if role == "server" else service_root / (role + ".d")
        if expires_soon(root):
            install_certificate(sign_csr(create_csr(root), role=role, address=address, admin_root=admin_root), root=root)
        if role in {"exporter", "logs"} and os.geteuid() == 0:
            root.chmod(0o755)
            for name in ("ca.pem", "cert.pem", "key.pem"):
                os.chown(root / name, 65534, 65534)
    return tls_paths(admin_root / "tls")


def client_tls(role: str = "control") -> dict[str, str] | None:
    explicit = {"ca_file": os.environ.get("NOMAD_CACERT"), "cert_file": os.environ.get("NOMAD_CLIENT_CERT"), "key_file": os.environ.get("NOMAD_CLIENT_KEY")}
    if any(explicit.values()):
        if not all(explicit.values()):
            raise LumaError("Nomad TLS requires a CA, client certificate and private key")
        return {key: str(value) for key, value in explicit.items()}
    root = ADMIN_ROOT / "tls" if role == "agent" else security_root() / (role + ".d")
    if (root / "cert.pem").exists():
        return tls_paths(root)
    return None


def endpoint(address: str) -> str:
    parsed = urlsplit(address)
    transport = read_private_json(security_root() / "transport.json", optional=True)
    cluster = read_private_json(security_root() / "cluster.json", optional=True)
    local = parsed.hostname in {"127.0.0.1", "localhost", transport.get("address")}
    if parsed.port == 4646 and (cluster.get("tlsRequired") or local and transport.get("http")):
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return address


def request_enrollment(node_name: str, address: str, *, timeout: float = 25) -> dict[str, str]:
    """Control requests a client identity over its private host mailbox."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", node_name):
        raise LumaError("invalid Nomad enrollment name")
    request_id = uuid.uuid4().hex
    root = security_root() / "enrollment"
    path = root / (request_id + ".request.json")
    response = root / (request_id + ".response.json")
    private_write(path, json.dumps({"schema": 1, "node": node_name, "address": mesh_ip(address), "createdAt": time.time()}))
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            bundle = read_private_json(response, optional=True)
            if bundle:
                if bundle.get("error"):
                    raise LumaError(str(bundle["error"]))
                return bundle
            time.sleep(0.2)
        raise LumaError("Nomad certificate enrollment is pending; the manager security supervisor has not acknowledged it")
    finally:
        path.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


def sign_enrollments(state: Mapping[str, Any], *, admin_root: Path = ADMIN_ROOT) -> None:
    root = security_root() / "enrollment"
    for path in sorted(root.glob("*.request.json"))[:32]:
        request = read_private_json(path)
        response = path.with_name(path.name.replace(".request.json", ".response.json"))
        if response.exists():
            continue
        try:
            if time.time() - float(request.get("createdAt") or 0) > 60:
                raise LumaError("Nomad enrollment request expired")
            name, address = str(request.get("node") or ""), mesh_ip(str(request.get("address") or ""))
            record = (state.get("nodes") or {}).get(name) or {}
            if record.get("tailscaleIP") != address or not record.get("pendingEnrollment") and not (record.get("agent") or {}).get("tokenHash"):
                raise LumaError("Nomad TLS enrollment does not match the registered node identity")
            # Per-request key; it is delivered only in the authenticated join
            # response and removed from the mailbox after receipt/expiry.
            import tempfile
            with tempfile.TemporaryDirectory(prefix=".enroll-", dir=admin_root) as directory:
                key_root = Path(directory)
                bundle = sign_csr(create_csr(key_root), role="client", address=address, admin_root=admin_root)
                bundle["key"] = private_read(key_root / "key.pem")
                bundle["caSHA256"] = hashlib.sha256(bundle["ca"].encode()).hexdigest()
            private_write(response, json.dumps(bundle))
        except LumaError as exc:
            private_write(response, json.dumps({"error": str(exc)}))
    for path in root.glob("*.json"):
        if time.time() - path.stat().st_mtime > 300:
            path.unlink(missing_ok=True)
