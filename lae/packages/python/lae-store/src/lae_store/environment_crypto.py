from __future__ import annotations

import hashlib
import hmac
import json
import struct
from dataclasses import dataclass, field
from typing import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .application_catalog import EncryptedEnvironmentValue

_ENVELOPE_MAGIC = b"LAEENV\x01"
_NONCE_BYTES = 12
_MAX_PLAINTEXT_BYTES = 64 * 1024


class EnvironmentCryptoError(RuntimeError):
    """Stable, non-secret-bearing environment crypto failure."""


@dataclass(frozen=True, slots=True)
class EnvironmentPlaintext:
    """Ephemeral plaintext input that is deliberately omitted from repr."""

    service_scope: str
    name: str
    value: str = field(repr=False)
    is_sensitive: bool = True
    required: bool = False


@dataclass(frozen=True, slots=True)
class EnvironmentCiphertext:
    """Opaque envelope plus integrity metadata safe for durable storage."""

    envelope: bytes = field(repr=False)
    checksum: bytes = field(repr=False)
    key_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, bytes) or not self.envelope:
            raise ValueError("environment envelope is invalid")
        if not isinstance(self.checksum, bytes) or len(self.checksum) != 32:
            raise ValueError("environment checksum must contain 32 bytes")
        if isinstance(self.key_version, bool) or self.key_version < 1:
            raise ValueError("environment key version must be positive")


class EnvironmentKeyRing:
    """Versioned AES-256-GCM envelope adapter for application environment values.

    The database stores the key version next to the opaque envelope.  AAD binds
    every ciphertext to exactly one tenant, application, service scope, name and
    key version, so moving rows across any of those boundaries fails closed.
    """

    def __init__(
        self,
        *,
        current_version: int,
        keys: Mapping[int, bytes],
        checksum_key: bytes,
    ) -> None:
        if isinstance(current_version, bool) or current_version < 1:
            raise ValueError("environment key version must be positive")
        normalized: dict[int, bytes] = {}
        for version, key in keys.items():
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ValueError("environment key version must be positive")
            if not isinstance(key, bytes) or len(key) != 32:
                raise ValueError("environment encryption keys must be 256 bits")
            normalized[version] = key
        if current_version not in normalized:
            raise ValueError("current environment encryption key is unavailable")
        if not isinstance(checksum_key, bytes) or len(checksum_key) < 32:
            raise ValueError("environment checksum HMAC key must be at least 256 bits")
        self._current_version = current_version
        self._keys = normalized
        self._checksum_key = checksum_key

    @property
    def current_version(self) -> int:
        return self._current_version

    def encrypt(
        self,
        plaintext: str,
        *,
        tenant_id: str,
        application_id: str,
        service_scope: str,
        name: str,
    ) -> EnvironmentCiphertext:
        raw = self._plaintext_bytes(plaintext)
        version = self._current_version
        aad = self._aad(
            tenant_id=tenant_id,
            application_id=application_id,
            service_scope=service_scope,
            name=name,
            key_version=version,
        )
        # AESGCM.generate_key is not a nonce generator; os.urandom is used by
        # cryptography's backend through this standard-library import path.
        import os

        nonce = os.urandom(_NONCE_BYTES)
        sealed = AESGCM(self._keys[version]).encrypt(nonce, raw, aad)
        checksum = hmac.new(
            self._checksum_key,
            b"lae.environment-checksum.v1\0" + aad + b"\0" + raw,
            hashlib.sha256,
        ).digest()
        return EnvironmentCiphertext(
            envelope=_ENVELOPE_MAGIC + nonce + sealed,
            checksum=checksum,
            key_version=version,
        )

    def encrypt_value(
        self,
        value: EnvironmentPlaintext,
        *,
        tenant_id: str,
        application_id: str,
    ) -> EncryptedEnvironmentValue:
        encrypted = self.encrypt(
            value.value,
            tenant_id=tenant_id,
            application_id=application_id,
            service_scope=value.service_scope,
            name=value.name,
        )
        return EncryptedEnvironmentValue(
            service_scope=value.service_scope,
            name=value.name,
            envelope_ciphertext=encrypted.envelope,
            checksum=encrypted.checksum,
            key_version=encrypted.key_version,
            is_sensitive=value.is_sensitive,
            required=value.required,
            source="user",
        )

    def decrypt(
        self,
        encrypted: EnvironmentCiphertext,
        *,
        tenant_id: str,
        application_id: str,
        service_scope: str,
        name: str,
    ) -> str:
        key = self._keys.get(encrypted.key_version)
        if key is None:
            raise EnvironmentCryptoError("environment key version is unavailable")
        envelope = encrypted.envelope
        if (
            not isinstance(envelope, bytes)
            or len(envelope) < len(_ENVELOPE_MAGIC) + _NONCE_BYTES + 16
            or not envelope.startswith(_ENVELOPE_MAGIC)
        ):
            raise EnvironmentCryptoError("environment envelope is invalid")
        aad = self._aad(
            tenant_id=tenant_id,
            application_id=application_id,
            service_scope=service_scope,
            name=name,
            key_version=encrypted.key_version,
        )
        offset = len(_ENVELOPE_MAGIC)
        nonce = envelope[offset : offset + _NONCE_BYTES]
        sealed = envelope[offset + _NONCE_BYTES :]
        try:
            raw = AESGCM(key).decrypt(nonce, sealed, aad)
        except InvalidTag as exc:
            raise EnvironmentCryptoError("environment envelope authentication failed") from exc
        expected = hmac.new(
            self._checksum_key,
            b"lae.environment-checksum.v1\0" + aad + b"\0" + raw,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, encrypted.checksum):
            raise EnvironmentCryptoError("environment checksum verification failed")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvironmentCryptoError("environment plaintext encoding is invalid") from exc

    @staticmethod
    def _plaintext_bytes(value: str) -> bytes:
        if not isinstance(value, str):
            raise ValueError("environment value must be a string")
        raw = value.encode("utf-8")
        if len(raw) > _MAX_PLAINTEXT_BYTES:
            raise ValueError("environment value exceeds 64 KiB")
        return raw

    @staticmethod
    def _aad(
        *,
        tenant_id: str,
        application_id: str,
        service_scope: str,
        name: str,
        key_version: int,
    ) -> bytes:
        fields = (
            "lae.environment-envelope.v1",
            tenant_id,
            application_id,
            service_scope,
            name,
            str(key_version),
        )
        encoded = []
        for field_value in fields:
            if not isinstance(field_value, str):
                raise ValueError("environment AAD field is invalid")
            raw = field_value.encode("utf-8")
            if not raw or len(raw) > 512:
                raise ValueError("environment AAD field is invalid")
            encoded.append(struct.pack(">H", len(raw)) + raw)
        return b"".join(encoded)


def key_ring_manifest(key_ring: EnvironmentKeyRing) -> str:
    """Return non-secret readiness metadata for diagnostics/tests."""

    return json.dumps(
        {"algorithm": "AES-256-GCM", "currentKeyVersion": key_ring.current_version},
        separators=(",", ":"),
        sort_keys=True,
    )
