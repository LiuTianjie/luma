from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lae_store import (
    CredentialLeaseRejected,
    EncryptedSourceConnectionSecret,
    SourceConnectionCryptoError,
    SourceConnectionKeyRing,
    canonical_allowed_host,
)
from lae_store.ids import require_opaque_id
from lae_store.models import BuilderTask, SourceConnection, SourceCredentialLease

_CONSUMER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class EphemeralGitCredential:
    """A one-call in-memory credential; repr deliberately excludes the secret."""

    provider: str
    username: str | None
    secret: str = field(repr=False)
    allowed_host: str
    credential_version: int


class ConnectionCredentialBroker(Protocol):
    async def claim(
        self,
        lease_id: str,
        *,
        consumer_id: str,
        repository: str,
    ) -> EphemeralGitCredential: ...


class UnavailableConnectionCredentialBroker:
    """Production-safe default until a mutually authenticated endpoint exists."""

    async def claim(
        self,
        lease_id: str,
        *,
        consumer_id: str,
        repository: str,
    ) -> EphemeralGitCredential:
        del lease_id, consumer_id, repository
        raise CredentialLeaseRejected("source credential broker is unavailable")


class PostgresConnectionCredentialBroker:
    """Atomically redeem one tenant/consumer/host-bound private Git lease.

    This is the durable broker port, not a public API.  Production remains
    fail-closed until LAE exposes it through a mutually authenticated internal
    endpoint that Luma Builder can call using the opaque ``credentialLeaseId``.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        key_ring: SourceConnectionKeyRing,
    ) -> None:
        self._sessions = sessions
        self._key_ring = key_ring

    async def claim(
        self,
        lease_id: str,
        *,
        consumer_id: str,
        repository: str,
    ) -> EphemeralGitCredential:
        try:
            require_opaque_id(lease_id, prefix="lease")
            if (
                not isinstance(consumer_id, str)
                or _CONSUMER.fullmatch(consumer_id) is None
            ):
                raise ValueError("invalid consumer")
            requested_host = canonical_allowed_host(repository)
        except ValueError as exc:
            raise CredentialLeaseRejected("credential lease claim is invalid") from exc

        async with self._sessions() as session:
            async with session.begin():
                # Connection mutations lock connection -> lease. Read only the
                # opaque FK first, then take locks in that same order to avoid
                # a rotate/revoke vs redeem deadlock.
                reference = (
                    await session.execute(
                        select(
                            SourceCredentialLease.tenant_id,
                            SourceCredentialLease.source_connection_id,
                        ).where(SourceCredentialLease.id == lease_id)
                    )
                ).one_or_none()
                if reference is None or reference.source_connection_id is None:
                    raise CredentialLeaseRejected("credential lease is unavailable")
                connection = await session.scalar(
                    select(SourceConnection)
                    .where(
                        SourceConnection.tenant_id == reference.tenant_id,
                        SourceConnection.id == reference.source_connection_id,
                    )
                    .with_for_update()
                )
                lease = await session.scalar(
                    select(SourceCredentialLease)
                    .where(
                        SourceCredentialLease.id == lease_id,
                        SourceCredentialLease.tenant_id == reference.tenant_id,
                        SourceCredentialLease.source_connection_id
                        == reference.source_connection_id,
                    )
                    .with_for_update()
                )
                # PostgreSQL now() is fixed at transaction start. Use the wall
                # clock after lock acquisition so time spent waiting cannot
                # extend a credential lease beyond its TTL.
                now = await session.scalar(select(func.clock_timestamp()))
                if (
                    now is None
                    or lease is None
                    or lease.status != "issued"
                    or lease.consumed_at is not None
                    or lease.revoked_at is not None
                    or lease.expires_at <= now
                    or lease.source_connection_id is None
                    or lease.consumer_binding_hash is None
                    or lease.binding_key_version is None
                ):
                    raise CredentialLeaseRejected("credential lease is unavailable")
                if not hmac.compare_digest(lease.consumer_id, consumer_id):
                    raise CredentialLeaseRejected("credential lease is unavailable")
                if not hmac.compare_digest(lease.allowed_host, requested_host):
                    raise CredentialLeaseRejected("credential lease is unavailable")

                task = await session.scalar(
                    select(BuilderTask).where(
                        BuilderTask.tenant_id == lease.tenant_id,
                        BuilderTask.id == lease.builder_task_id,
                    )
                )
                if (
                    connection is None
                    or connection.revoked_at is not None
                    or task is None
                    or task.action != "source.analyze"
                    or task.credential_lease_id != lease.id
                    or not hmac.compare_digest(task.luma_principal_id, consumer_id)
                    or not hmac.compare_digest(connection.allowed_host, requested_host)
                    or not self._key_ring.verify_lease_binding(
                        lease.consumer_binding_hash,
                        key_version=lease.binding_key_version,
                        tenant_id=lease.tenant_id,
                        lease_id=lease.id,
                        connection_id=connection.id,
                        builder_task_id=lease.builder_task_id,
                        consumer_id=consumer_id,
                        allowed_host=requested_host,
                    )
                ):
                    raise CredentialLeaseRejected("credential lease is unavailable")

                try:
                    plaintext = self._key_ring.decrypt(
                        EncryptedSourceConnectionSecret(
                            ciphertext=connection.secret_ciphertext,
                            nonce=connection.secret_nonce,
                            checksum=connection.secret_checksum,
                            key_version=connection.key_version,
                        ),
                        tenant_id=connection.tenant_id,
                        connection_id=connection.id,
                        provider=connection.provider,
                        allowed_host=connection.allowed_host,
                        username=connection.username,
                        credential_version=connection.credential_version,
                    )
                except SourceConnectionCryptoError as exc:
                    raise CredentialLeaseRejected(
                        "credential lease is unavailable"
                    ) from exc
                lease.status = "consumed"
                lease.consumed_at = now
                lease.updated_at = now
                connection.last_used_at = now
                connection.updated_at = now
                await session.flush()
                return EphemeralGitCredential(
                    provider=connection.provider,
                    username=connection.username,
                    secret=plaintext.secret,
                    allowed_host=connection.allowed_host,
                    credential_version=connection.credential_version,
                )
