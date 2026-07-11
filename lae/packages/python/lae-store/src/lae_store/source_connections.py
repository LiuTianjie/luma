from __future__ import annotations

import hashlib
import hmac
import os
import re
import struct
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .analysis_requests import canonical_allowed_host, canonical_https_repository
from .errors import (
    IdempotencyKeyReused,
    ResourceNotFound,
    SourceConnectionConflict,
)
from .ids import new_id, require_opaque_id
from .models import (
    IdempotencyRecord,
    Operation,
    SourceConnection,
    SourceCredentialLease,
)
from .repositories import IdempotencyInput, Principal, TenantScope
from .security import ensure_persistable_payload
from .tokens import keyed_request_hash

SOURCE_CONNECTION_CREATE_ROUTE = "/v1/source-connections"
SOURCE_CONNECTION_ROTATE_ROUTE = "/v1/source-connections/{connection_id}/rotate"
SOURCE_CONNECTION_REVOKE_ROUTE = "/v1/source-connections/{connection_id}"

_PROVIDERS = frozenset({"github", "gitea", "generic"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NONCE_BYTES = 12
_MAX_SECRET_BYTES = 4096


class SourceConnectionCryptoError(RuntimeError):
    """Stable crypto error that never carries credential material."""


@dataclass(frozen=True, slots=True)
class SourceConnectionPlaintext:
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class EncryptedSourceConnectionSecret:
    ciphertext: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    checksum: bytes = field(repr=False)
    key_version: int


class SourceConnectionKeyRing:
    """Versioned AES-256-GCM and HMAC keys for private Git credentials."""

    def __init__(
        self,
        *,
        current_version: int,
        encryption_keys: Mapping[int, bytes],
        hmac_keys: Mapping[int, bytes],
    ) -> None:
        if (
            isinstance(current_version, bool)
            or not isinstance(current_version, int)
            or current_version < 1
        ):
            raise ValueError("source connection key version must be positive")
        encrypted = _validated_key_map(encryption_keys, exact=True, label="encryption")
        hashed = _validated_key_map(hmac_keys, exact=False, label="HMAC")
        if current_version not in encrypted or current_version not in hashed:
            raise ValueError("current source connection key version is unavailable")
        if set(encrypted) != set(hashed):
            raise ValueError(
                "source connection encryption and HMAC key versions differ"
            )
        self._current_version = current_version
        self._encryption_keys = encrypted
        self._hmac_keys = hashed

    @property
    def current_version(self) -> int:
        return self._current_version

    def encrypt(
        self,
        plaintext: SourceConnectionPlaintext,
        *,
        tenant_id: str,
        connection_id: str,
        provider: str,
        allowed_host: str,
        username: str | None,
        credential_version: int,
    ) -> EncryptedSourceConnectionSecret:
        raw = _secret_bytes(plaintext.secret)
        version = self._current_version
        aad = self._aad(
            tenant_id=tenant_id,
            connection_id=connection_id,
            provider=provider,
            allowed_host=allowed_host,
            username=username,
            credential_version=credential_version,
            key_version=version,
        )
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self._encryption_keys[version]).encrypt(nonce, raw, aad)
        checksum = hmac.new(
            self._hmac_keys[version],
            b"lae.source-connection-checksum.v1\0" + aad + b"\0" + raw,
            hashlib.sha256,
        ).digest()
        return EncryptedSourceConnectionSecret(
            ciphertext=ciphertext,
            nonce=nonce,
            checksum=checksum,
            key_version=version,
        )

    def decrypt(
        self,
        encrypted: EncryptedSourceConnectionSecret,
        *,
        tenant_id: str,
        connection_id: str,
        provider: str,
        allowed_host: str,
        username: str | None,
        credential_version: int,
    ) -> SourceConnectionPlaintext:
        encryption_key = self._encryption_keys.get(encrypted.key_version)
        checksum_key = self._hmac_keys.get(encrypted.key_version)
        if encryption_key is None or checksum_key is None:
            raise SourceConnectionCryptoError(
                "source connection key version is unavailable"
            )
        if (
            not isinstance(encrypted.nonce, bytes)
            or len(encrypted.nonce) != _NONCE_BYTES
            or not isinstance(encrypted.ciphertext, bytes)
            or not 16 <= len(encrypted.ciphertext) <= _MAX_SECRET_BYTES + 16
            or not isinstance(encrypted.checksum, bytes)
            or len(encrypted.checksum) != 32
        ):
            raise SourceConnectionCryptoError("source connection envelope is invalid")
        aad = self._aad(
            tenant_id=tenant_id,
            connection_id=connection_id,
            provider=provider,
            allowed_host=allowed_host,
            username=username,
            credential_version=credential_version,
            key_version=encrypted.key_version,
        )
        try:
            raw = AESGCM(encryption_key).decrypt(
                encrypted.nonce, encrypted.ciphertext, aad
            )
        except InvalidTag as exc:
            raise SourceConnectionCryptoError(
                "source connection envelope authentication failed"
            ) from exc
        expected = hmac.new(
            checksum_key,
            b"lae.source-connection-checksum.v1\0" + aad + b"\0" + raw,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, encrypted.checksum):
            raise SourceConnectionCryptoError(
                "source connection checksum verification failed"
            )
        try:
            secret = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceConnectionCryptoError(
                "source connection plaintext encoding is invalid"
            ) from exc
        try:
            _secret_bytes(secret)
        except ValueError as exc:
            raise SourceConnectionCryptoError(
                "source connection plaintext is invalid"
            ) from exc
        return SourceConnectionPlaintext(secret)

    def lease_binding_digest(
        self,
        *,
        key_version: int,
        tenant_id: str,
        lease_id: str,
        connection_id: str,
        builder_task_id: str,
        consumer_id: str,
        allowed_host: str,
    ) -> bytes:
        key = self._hmac_keys.get(key_version)
        if key is None:
            raise SourceConnectionCryptoError(
                "source connection HMAC key version is unavailable"
            )
        binding = _framed(
            (
                "lae.source-credential-lease-binding.v1",
                tenant_id,
                lease_id,
                connection_id,
                builder_task_id,
                consumer_id,
                allowed_host,
                str(key_version),
            )
        )
        return hmac.new(key, binding, hashlib.sha256).digest()

    def verify_lease_binding(self, expected: bytes, **fields: Any) -> bool:
        if not isinstance(expected, bytes) or len(expected) != 32:
            return False
        try:
            actual = self.lease_binding_digest(**fields)
        except (TypeError, ValueError, SourceConnectionCryptoError):
            return False
        return hmac.compare_digest(actual, expected)

    @staticmethod
    def _aad(
        *,
        tenant_id: str,
        connection_id: str,
        provider: str,
        allowed_host: str,
        username: str | None,
        credential_version: int,
        key_version: int,
    ) -> bytes:
        return _framed(
            (
                "lae.source-connection-envelope.v1",
                tenant_id,
                connection_id,
                provider,
                allowed_host,
                username or "",
                str(credential_version),
                str(key_version),
            )
        )


@dataclass(frozen=True, slots=True)
class CreateSourceConnection:
    scope: TenantScope
    principal: Principal
    provider: str
    display_name: str
    base_url: str
    username: str | None
    secret: str = field(repr=False)
    idempotency_key: str = field(repr=False)

    def __post_init__(self) -> None:
        provider = canonical_provider(self.provider)
        display_name = canonical_display_name(self.display_name)
        base_url = canonical_source_base_url(self.base_url)
        allowed_host = canonical_allowed_host(_base_probe_repository(base_url))
        username = canonical_source_username(self.username)
        _secret_bytes(self.secret)
        if provider == "github" and (
            base_url != "https://github.com" or allowed_host != "github.com"
        ):
            raise ValueError("GitHub connections must use https://github.com")
        IdempotencyInput(
            key=self.idempotency_key,
            method="POST",
            route_template=SOURCE_CONNECTION_CREATE_ROUTE,
            request_hash=b"\0" * 32,
        )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "username", username)

    @property
    def allowed_host(self) -> str:
        return canonical_allowed_host(_base_probe_repository(self.base_url))


@dataclass(frozen=True, slots=True)
class RotateSourceConnection:
    scope: TenantScope
    principal: Principal
    connection_id: str
    secret: str = field(repr=False)
    idempotency_key: str = field(repr=False)
    username: str | None = None
    username_provided: bool = False

    def __post_init__(self) -> None:
        require_opaque_id(self.connection_id, prefix="conn")
        _secret_bytes(self.secret)
        if not isinstance(self.username_provided, bool):
            raise ValueError("source connection username update flag is invalid")
        if self.username_provided:
            object.__setattr__(
                self, "username", canonical_source_username(self.username)
            )
        elif self.username is not None:
            raise ValueError("source connection username update is ambiguous")
        IdempotencyInput(
            key=self.idempotency_key,
            method="POST",
            route_template=SOURCE_CONNECTION_ROTATE_ROUTE,
            request_hash=b"\0" * 32,
        )


@dataclass(frozen=True, slots=True)
class RevokeSourceConnection:
    scope: TenantScope
    principal: Principal
    connection_id: str
    idempotency_key: str = field(repr=False)

    def __post_init__(self) -> None:
        require_opaque_id(self.connection_id, prefix="conn")
        IdempotencyInput(
            key=self.idempotency_key,
            method="DELETE",
            route_template=SOURCE_CONNECTION_REVOKE_ROUTE,
            request_hash=b"\0" * 32,
        )


@dataclass(frozen=True, slots=True)
class SourceConnectionRecord:
    id: str
    provider: str
    display_name: str
    base_url: str
    allowed_host: str
    username: str | None
    credential_version: int
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    def public_body(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "displayName": self.display_name,
            "baseUrl": self.base_url,
            "allowedHost": self.allowed_host,
            "username": self.username,
            "credentialVersion": self.credential_version,
            "createdAt": _timestamp(self.created_at),
            "updatedAt": _timestamp(self.updated_at),
            "lastUsedAt": _timestamp(self.last_used_at),
            "revokedAt": _timestamp(self.revoked_at),
        }


@dataclass(frozen=True, slots=True)
class SourceConnectionMutationResult:
    response_body: dict[str, Any]
    replayed: bool


class PostgresSourceConnectionStore:
    """Encrypted source connection catalog with atomic idempotent mutations."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        key_ring: SourceConnectionKeyRing,
        *,
        idempotency_hash_key: bytes,
    ) -> None:
        if (
            not isinstance(idempotency_hash_key, bytes)
            or len(idempotency_hash_key) < 32
        ):
            raise ValueError(
                "source connection idempotency key must be at least 256 bits"
            )
        self._sessions = sessions
        self._key_ring = key_ring
        self._idempotency_hash_key = idempotency_hash_key

    @property
    def key_ring(self) -> SourceConnectionKeyRing:
        return self._key_ring

    async def create(
        self, command: CreateSourceConnection
    ) -> SourceConnectionMutationResult:
        idempotency = self._idempotency(
            command.principal,
            command.idempotency_key,
            "POST",
            SOURCE_CONNECTION_CREATE_ROUTE,
            {
                "provider": command.provider,
                "displayName": command.display_name,
                "baseUrl": command.base_url,
                "username": command.username,
                "secret": command.secret,
            },
        )
        async with self._sessions() as session:
            async with session.begin():
                existing, now = await self._lock_idempotency(
                    session, command.scope, command.principal, idempotency
                )
                if existing is not None:
                    return self._replay(existing, idempotency)
                connection_id = new_id("conn")
                encrypted = self._key_ring.encrypt(
                    SourceConnectionPlaintext(command.secret),
                    tenant_id=command.scope.tenant_id,
                    connection_id=connection_id,
                    provider=command.provider,
                    allowed_host=command.allowed_host,
                    username=command.username,
                    credential_version=1,
                )
                row = SourceConnection(
                    id=connection_id,
                    tenant_id=command.scope.tenant_id,
                    provider=command.provider,
                    display_name=command.display_name,
                    base_url=command.base_url,
                    allowed_host=command.allowed_host,
                    username=command.username,
                    secret_ciphertext=encrypted.ciphertext,
                    secret_nonce=encrypted.nonce,
                    secret_checksum=encrypted.checksum,
                    key_version=encrypted.key_version,
                    credential_version=1,
                )
                session.add(row)
                await session.flush()
                body = {"connection": _record(row).public_body()}
                await self._record_idempotency(
                    session,
                    command.scope,
                    command.principal,
                    idempotency,
                    body,
                    status=201,
                    kind="source.connection-create",
                    target_id=row.id,
                    now=now,
                )
                return SourceConnectionMutationResult(body, replayed=False)

    async def list(
        self, scope: TenantScope, *, limit: int = 100
    ) -> tuple[SourceConnectionRecord, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("source connection list limit must be 1-100")
        async with self._sessions() as session:
            rows = await session.scalars(
                select(SourceConnection)
                .where(SourceConnection.tenant_id == scope.tenant_id)
                .order_by(
                    SourceConnection.created_at.desc(), SourceConnection.id.desc()
                )
                .limit(limit)
            )
            return tuple(_record(row) for row in rows)

    async def rotate(
        self, command: RotateSourceConnection
    ) -> SourceConnectionMutationResult:
        idempotency = self._idempotency(
            command.principal,
            command.idempotency_key,
            "POST",
            SOURCE_CONNECTION_ROTATE_ROUTE,
            {
                "connectionId": command.connection_id,
                "username": command.username,
                "usernameProvided": command.username_provided,
                "secret": command.secret,
            },
        )
        async with self._sessions() as session:
            async with session.begin():
                existing, now = await self._lock_idempotency(
                    session, command.scope, command.principal, idempotency
                )
                if existing is not None:
                    return self._replay(existing, idempotency)
                row = await self._connection(
                    session, command.scope, command.connection_id, for_update=True
                )
                if row.revoked_at is not None:
                    raise ResourceNotFound("source connection not found")
                credential_version = row.credential_version + 1
                username = (
                    command.username if command.username_provided else row.username
                )
                encrypted = self._key_ring.encrypt(
                    SourceConnectionPlaintext(command.secret),
                    tenant_id=row.tenant_id,
                    connection_id=row.id,
                    provider=row.provider,
                    allowed_host=row.allowed_host,
                    username=username,
                    credential_version=credential_version,
                )
                row.username = username
                row.secret_ciphertext = encrypted.ciphertext
                row.secret_nonce = encrypted.nonce
                row.secret_checksum = encrypted.checksum
                row.key_version = encrypted.key_version
                row.credential_version = credential_version
                row.updated_at = now
                await self._revoke_open_leases(session, row.id, now)
                await session.flush()
                body = {"connection": _record(row).public_body()}
                await self._record_idempotency(
                    session,
                    command.scope,
                    command.principal,
                    idempotency,
                    body,
                    status=200,
                    kind="source.connection-rotate",
                    target_id=row.id,
                    now=now,
                )
                return SourceConnectionMutationResult(body, replayed=False)

    async def revoke(
        self, command: RevokeSourceConnection
    ) -> SourceConnectionMutationResult:
        idempotency = self._idempotency(
            command.principal,
            command.idempotency_key,
            "DELETE",
            SOURCE_CONNECTION_REVOKE_ROUTE,
            {"connectionId": command.connection_id},
        )
        async with self._sessions() as session:
            async with session.begin():
                existing, now = await self._lock_idempotency(
                    session, command.scope, command.principal, idempotency
                )
                if existing is not None:
                    return self._replay(existing, idempotency)
                row = await self._connection(
                    session, command.scope, command.connection_id, for_update=True
                )
                if row.revoked_at is None:
                    row.revoked_at = now
                    row.updated_at = now
                    await self._revoke_open_leases(session, row.id, now)
                    await session.flush()
                body = {"connection": _record(row).public_body()}
                await self._record_idempotency(
                    session,
                    command.scope,
                    command.principal,
                    idempotency,
                    body,
                    status=204,
                    kind="source.connection-revoke",
                    target_id=row.id,
                    now=now,
                )
                return SourceConnectionMutationResult(body, replayed=False)

    async def _connection(
        self,
        session: AsyncSession,
        scope: TenantScope,
        connection_id: str,
        *,
        for_update: bool,
    ) -> SourceConnection:
        require_opaque_id(connection_id, prefix="conn")
        statement = select(SourceConnection).where(
            SourceConnection.tenant_id == scope.tenant_id,
            SourceConnection.id == connection_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = await session.scalar(statement)
        if row is None:
            raise ResourceNotFound("source connection not found")
        return row

    async def _lock_idempotency(
        self,
        session: AsyncSession,
        scope: TenantScope,
        principal: Principal,
        idempotency: IdempotencyInput,
    ) -> tuple[IdempotencyRecord | None, datetime]:
        lock_scope = (
            f"source-connection:{scope.tenant_id}:{principal.type}:"
            f"{principal.id}:{idempotency.method}:{idempotency.route_template}:"
            f"{idempotency.key}"
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": lock_scope},
        )
        now = await session.scalar(select(func.now()))
        if now is None:
            raise SourceConnectionConflict("database clock is unavailable")
        existing = await session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.tenant_id == scope.tenant_id,
                IdempotencyRecord.principal_type == principal.type,
                IdempotencyRecord.principal_id == principal.id,
                IdempotencyRecord.method == idempotency.method,
                IdempotencyRecord.route_template == idempotency.route_template,
                IdempotencyRecord.key == idempotency.key,
            )
            .with_for_update()
        )
        if existing is not None and existing.expires_at > now:
            if not hmac.compare_digest(existing.request_hash, idempotency.request_hash):
                raise IdempotencyKeyReused(
                    "idempotency key was used for another source connection request"
                )
            return existing, now
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        return None, now

    @staticmethod
    def _replay(
        existing: IdempotencyRecord, idempotency: IdempotencyInput
    ) -> SourceConnectionMutationResult:
        if not hmac.compare_digest(existing.request_hash, idempotency.request_hash):
            raise IdempotencyKeyReused("idempotency key was reused")
        body = existing.response_body
        try:
            connection = body["connection"]
            if (
                set(body) != {"connection"}
                or not isinstance(connection, dict)
                or "id" not in connection
                or "secret" in connection
                or "token" in connection
                or "ciphertext" in connection
            ):
                raise KeyError("invalid response")
        except (KeyError, TypeError) as exc:
            raise SourceConnectionConflict(
                "idempotent source connection response is invalid"
            ) from exc
        return SourceConnectionMutationResult(body, replayed=True)

    async def _record_idempotency(
        self,
        session: AsyncSession,
        scope: TenantScope,
        principal: Principal,
        idempotency: IdempotencyInput,
        body: dict[str, Any],
        *,
        status: int,
        kind: str,
        target_id: str,
        now: datetime,
    ) -> None:
        ensure_persistable_payload(body)
        operation = Operation(
            id=new_id("op"),
            tenant_id=scope.tenant_id,
            principal_type=principal.type,
            principal_id=principal.id,
            kind=kind,
            target_type="source-connection",
            target_id=target_id,
            status="succeeded",
            phase=kind,
            result=body,
            finished_at=now,
            last_event_seq=0,
        )
        session.add(operation)
        await session.flush()
        session.add(
            IdempotencyRecord(
                tenant_id=scope.tenant_id,
                principal_type=principal.type,
                principal_id=principal.id,
                key=idempotency.key,
                method=idempotency.method,
                route_template=idempotency.route_template,
                request_hash=idempotency.request_hash,
                response_status=status,
                response_body=body,
                operation_id=operation.id,
                expires_at=now + idempotency.retention,
            )
        )
        try:
            await session.flush()
        except IntegrityError as exc:
            raise SourceConnectionConflict(
                "source connection mutation conflicts with durable state"
            ) from exc

    async def _revoke_open_leases(
        self, session: AsyncSession, connection_id: str, now: datetime
    ) -> None:
        await session.execute(
            update(SourceCredentialLease)
            .where(
                SourceCredentialLease.source_connection_id == connection_id,
                SourceCredentialLease.status.in_(("issued", "claimed")),
            )
            .values(status="revoked", revoked_at=now, updated_at=now)
        )

    def _idempotency(
        self,
        principal: Principal,
        key: str,
        method: str,
        route: str,
        payload: dict[str, Any],
    ) -> IdempotencyInput:
        del principal
        return IdempotencyInput(
            key=key,
            method=method,
            route_template=route,
            request_hash=keyed_request_hash(payload, self._idempotency_hash_key),
        )


def canonical_provider(value: str) -> str:
    if not isinstance(value, str) or value not in _PROVIDERS:
        raise ValueError("source connection provider is invalid")
    return value


def canonical_display_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("source connection display name is invalid")
    result = value.strip()
    if not 1 <= len(result) <= 120 or _CONTROL.search(result):
        raise ValueError("source connection display name is invalid")
    return result


def canonical_source_username(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or _CONTROL.search(value)
    ):
        raise ValueError("source connection username is invalid")
    return value


def canonical_source_base_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or _CONTROL.search(value)
    ):
        raise ValueError("source connection base URL is invalid")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.startswith("//")
        or "\\" in parsed.path
    ):
        raise ValueError("source connection base URL must be credential-free HTTPS")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source connection base URL port is invalid") from exc
    path = parsed.path.rstrip("/")
    probe = urllib.parse.urlunsplit(
        (
            "https",
            parsed.netloc,
            f"{path}/.lae-host-check" if path else "/.lae-host-check",
            "",
            "",
        )
    )
    canonical_probe = canonical_https_repository(probe)
    canonical = urllib.parse.urlsplit(canonical_probe)
    assert canonical.hostname is not None
    hostname = canonical.hostname
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = host if port in {None, 443} else f"{host}:{port}"
    return urllib.parse.urlunsplit(("https", authority, path, "", ""))


def _base_probe_repository(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(
        (
            "https",
            parsed.netloc,
            f"{path}/.lae-host-check" if path else "/.lae-host-check",
            "",
            "",
        )
    )


def _secret_bytes(value: str) -> bytes:
    if not isinstance(value, str) or _CONTROL.search(value):
        raise ValueError("source connection secret is invalid")
    raw = value.encode("utf-8")
    if not 1 <= len(raw) <= _MAX_SECRET_BYTES:
        raise ValueError("source connection secret size is invalid")
    return raw


def _validated_key_map(
    values: Mapping[int, bytes], *, exact: bool, label: str
) -> dict[int, bytes]:
    normalized: dict[int, bytes] = {}
    for version, key in values.items():
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("source connection key version must be positive")
        if not isinstance(key, bytes) or (len(key) != 32 if exact else len(key) < 32):
            qualifier = "exactly" if exact else "at least"
            raise ValueError(
                f"source connection {label} keys must contain {qualifier} 256 bits"
            )
        normalized[version] = key
    return normalized


def _framed(values: tuple[str, ...]) -> bytes:
    result = bytearray()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("source connection binding field is invalid")
        raw = value.encode("utf-8")
        if len(raw) > 4096:
            raise ValueError("source connection binding field is invalid")
        result.extend(struct.pack(">I", len(raw)))
        result.extend(raw)
    return bytes(result)


def _record(row: SourceConnection) -> SourceConnectionRecord:
    return SourceConnectionRecord(
        id=row.id,
        provider=row.provider,
        display_name=row.display_name,
        base_url=row.base_url,
        allowed_host=row.allowed_host,
        username=row.username,
        credential_version=row.credential_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
