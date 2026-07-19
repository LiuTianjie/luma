from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .analysis_requests import canonical_allowed_host, canonical_https_repository
from .errors import CredentialLeaseRejected
from .ids import require_opaque_id
from .models import (
    Application,
    BuilderTask,
    Operation,
    SourceConnection,
    SourceCredentialLease,
    SourceRevision,
)
from .source_connections import (
    EncryptedSourceConnectionSecret,
    SourceConnectionCryptoError,
    SourceConnectionKeyRing,
)


CREDENTIAL_REDEMPTION_REQUEST_SCHEMA = "luma.credential-redemption/v1"
CREDENTIAL_REDEMPTION_RESULT_SCHEMA = "luma.credential-redemption-result/v1"
CREDENTIAL_REDEMPTION_MAX_TTL_SECONDS = 300
CREDENTIAL_REDEMPTION_FIELDS = frozenset(
    {
        "schemaVersion",
        "leaseId",
        "builderTaskId",
        "externalOperationId",
        "principalRef",
        "tenantRef",
        "applicationRef",
        "repository",
    }
)
_PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UPSTREAM_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_GENERIC_REJECTION = "credential lease is unavailable"


@dataclass(frozen=True, slots=True)
class CredentialRedemptionRequest:
    lease_id: str
    builder_task_id: str
    external_operation_id: str
    principal_ref: str
    tenant_ref: str
    application_ref: str
    repository: str

    def __post_init__(self) -> None:
        try:
            require_opaque_id(self.lease_id, prefix="lease")
            require_opaque_id(self.external_operation_id, prefix="op")
            require_opaque_id(self.tenant_ref, prefix="ten")
            require_opaque_id(self.application_ref, prefix="app")
            if (
                not isinstance(self.builder_task_id, str)
                or _UPSTREAM_TASK.fullmatch(self.builder_task_id) is None
                or not isinstance(self.principal_ref, str)
                or _PRINCIPAL.fullmatch(self.principal_ref) is None
            ):
                raise ValueError("invalid upstream binding")
            canonical = canonical_https_repository(self.repository)
            if not hmac.compare_digest(canonical, self.repository):
                raise ValueError("repository must be canonical")
        except (TypeError, ValueError) as exc:
            raise CredentialLeaseRejected(_GENERIC_REJECTION) from exc

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> CredentialRedemptionRequest:
        if (
            not isinstance(body, Mapping)
            or set(body) != CREDENTIAL_REDEMPTION_FIELDS
            or body.get("schemaVersion") != CREDENTIAL_REDEMPTION_REQUEST_SCHEMA
        ):
            raise CredentialLeaseRejected(_GENERIC_REJECTION)
        values = {
            "lease_id": body.get("leaseId"),
            "builder_task_id": body.get("builderTaskId"),
            "external_operation_id": body.get("externalOperationId"),
            "principal_ref": body.get("principalRef"),
            "tenant_ref": body.get("tenantRef"),
            "application_ref": body.get("applicationRef"),
            "repository": body.get("repository"),
        }
        if any(not isinstance(value, str) for value in values.values()):
            raise CredentialLeaseRejected(_GENERIC_REJECTION)
        return cls(**values)  # type: ignore[arg-type]

    def binding_body(self) -> dict[str, str]:
        return {
            "leaseId": self.lease_id,
            "builderTaskId": self.builder_task_id,
            "externalOperationId": self.external_operation_id,
            "principalRef": self.principal_ref,
            "tenantRef": self.tenant_ref,
            "applicationRef": self.application_ref,
            "repository": self.repository,
        }


@dataclass(frozen=True, slots=True)
class CredentialRedemptionResult:
    request: CredentialRedemptionRequest
    kind: str
    expires_at: int
    username: str = ""
    password: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.kind not in {"none", "git-https"}:
            raise ValueError("credential redemption result kind is invalid")
        if (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, int)
            or self.expires_at <= 0
        ):
            raise ValueError("credential redemption expiry is invalid")
        if self.kind == "none":
            if self.username or self.password:
                raise ValueError("anonymous redemption cannot contain credentials")
            return
        if (
            not isinstance(self.username, str)
            or not 1 <= len(self.username) <= 256
            or any(character in self.username for character in "\0\r\n")
            or not isinstance(self.password, str)
            or not 1 <= len(self.password) <= 8192
            or any(character in self.password for character in "\0\r\n")
        ):
            raise ValueError("Git HTTPS credential shape is invalid")

    def public_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schemaVersion": CREDENTIAL_REDEMPTION_RESULT_SCHEMA,
            **self.request.binding_body(),
            "kind": self.kind,
            "expiresAt": self.expires_at,
        }
        if self.kind == "git-https":
            body["credential"] = {
                "username": self.username,
                "password": self.password,
            }
        return body


class PostgresCredentialRedemptionBroker:
    """Exact, single-use LAE-to-Luma credential redemption boundary."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        key_ring: SourceConnectionKeyRing | None,
    ) -> None:
        self._sessions = sessions
        self._key_ring = key_ring

    async def redeem(
        self, request: CredentialRedemptionRequest
    ) -> CredentialRedemptionResult:
        async with self._sessions() as session:
            async with session.begin():
                # Source connection mutation uses connection -> lease lock
                # order. Read only opaque references before taking the same
                # order here, preventing rotate/revoke races and deadlocks.
                reference = (
                    await session.execute(
                        select(
                            SourceCredentialLease.tenant_id,
                            SourceCredentialLease.source_connection_id,
                        ).where(SourceCredentialLease.id == request.lease_id)
                    )
                ).one_or_none()
                if reference is None:
                    raise CredentialLeaseRejected(_GENERIC_REJECTION)

                connection: SourceConnection | None = None
                if reference.source_connection_id is not None:
                    if self._key_ring is None:
                        raise CredentialLeaseRejected(_GENERIC_REJECTION)
                    connection = await session.scalar(
                        select(SourceConnection)
                        .where(
                            SourceConnection.tenant_id == reference.tenant_id,
                            SourceConnection.id == reference.source_connection_id,
                        )
                        .with_for_update()
                    )
                connection_predicate = (
                    SourceCredentialLease.source_connection_id.is_(None)
                    if reference.source_connection_id is None
                    else SourceCredentialLease.source_connection_id
                    == reference.source_connection_id
                )
                lease = await session.scalar(
                    select(SourceCredentialLease)
                    .where(
                        SourceCredentialLease.id == request.lease_id,
                        SourceCredentialLease.tenant_id == reference.tenant_id,
                        connection_predicate,
                    )
                    .with_for_update()
                )
                now = await session.scalar(select(func.clock_timestamp()))
                if now is None or not self._valid_open_lease(
                    lease, request=request, now=now
                ):
                    raise CredentialLeaseRejected(_GENERIC_REJECTION)
                assert lease is not None

                operation = await session.scalar(
                    select(Operation)
                    .where(
                        Operation.tenant_id == request.tenant_ref,
                        Operation.id == request.external_operation_id,
                    )
                    .with_for_update()
                )
                task = await session.scalar(
                    select(BuilderTask)
                    .where(
                        BuilderTask.tenant_id == request.tenant_ref,
                        BuilderTask.id == lease.builder_task_id,
                    )
                    .with_for_update()
                )
                source = await session.scalar(
                    select(SourceRevision).where(
                        SourceRevision.tenant_id == request.tenant_ref,
                        SourceRevision.id == lease.source_revision_id,
                        SourceRevision.application_id == request.application_ref,
                        SourceRevision.deleted_at.is_(None),
                    )
                )
                application = await session.scalar(
                    select(Application).where(
                        Application.tenant_id == request.tenant_ref,
                        Application.id == request.application_ref,
                        Application.deleted_at.is_(None),
                    )
                )
                requested_host = canonical_allowed_host(request.repository)
                if not self._valid_graph(
                    request,
                    lease=lease,
                    task=task,
                    operation=operation,
                    source=source,
                    application=application,
                    connection=connection,
                    requested_host=requested_host,
                ):
                    raise CredentialLeaseRejected(_GENERIC_REJECTION)

                assert task is not None
                if task.luma_task_id is None:
                    # Luma may lease the Builder task immediately after the
                    # create response, before the Worker can checkpoint the
                    # returned upstream ID. The authenticated, fully bound
                    # redemption request closes that callback race by setting
                    # the same immutable ID under this row lock. A different
                    # ID remains rejected by _valid_graph above.
                    task.luma_task_id = request.builder_task_id
                    task.updated_at = now

                expiry = min(
                    int(lease.expires_at.timestamp()),
                    int(now.timestamp()) + CREDENTIAL_REDEMPTION_MAX_TTL_SECONDS,
                )
                if expiry <= int(now.timestamp()):
                    raise CredentialLeaseRejected(_GENERIC_REJECTION)

                username = ""
                password = ""
                kind = "none"
                if connection is not None:
                    assert self._key_ring is not None
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
                    except SourceConnectionCryptoError:
                        raise CredentialLeaseRejected(_GENERIC_REJECTION) from None
                    kind = "git-https"
                    username = connection.username or (
                        "x-access-token"
                        if connection.provider == "github"
                        else "oauth2"
                    )
                    password = plaintext.secret

                # Consumption and credential decryption occur in one locked
                # transaction. A replay can never retrieve a second plaintext.
                lease.status = "consumed"
                lease.consumed_at = now
                lease.updated_at = now
                if connection is not None:
                    connection.last_used_at = now
                    connection.updated_at = now
                await session.flush()
                return CredentialRedemptionResult(
                    request=request,
                    kind=kind,
                    expires_at=expiry,
                    username=username,
                    password=password,
                )

    @staticmethod
    def _valid_open_lease(
        lease: SourceCredentialLease | None,
        *,
        request: CredentialRedemptionRequest,
        now: datetime,
    ) -> bool:
        return bool(
            lease is not None
            and lease.status == "issued"
            and lease.consumed_at is None
            and lease.revoked_at is None
            and lease.expires_at > now
            and lease.allowed_action == "source.fetch"
            and hmac.compare_digest(lease.tenant_id, request.tenant_ref)
            and hmac.compare_digest(
                lease.operation_id, request.external_operation_id
            )
            and hmac.compare_digest(lease.consumer_id, request.principal_ref)
        )

    def _valid_graph(
        self,
        request: CredentialRedemptionRequest,
        *,
        lease: SourceCredentialLease,
        task: BuilderTask | None,
        operation: Operation | None,
        source: SourceRevision | None,
        application: Application | None,
        connection: SourceConnection | None,
        requested_host: str,
    ) -> bool:
        if (
            task is None
            or task.action != "source.analyze"
            or task.cancel_forwarded_at is not None
            or task.upstream_status in {"failed", "timed_out", "canceled"}
            or not hmac.compare_digest(task.tenant_id, request.tenant_ref)
            or not hmac.compare_digest(task.application_id, request.application_ref)
            or not hmac.compare_digest(task.source_revision_id, lease.source_revision_id)
            or not hmac.compare_digest(task.operation_id, request.external_operation_id)
            or not hmac.compare_digest(task.id, lease.builder_task_id)
            or not hmac.compare_digest(task.credential_lease_id, request.lease_id)
            or (
                task.luma_task_id is not None
                and not hmac.compare_digest(
                    task.luma_task_id, request.builder_task_id
                )
            )
            or not hmac.compare_digest(task.luma_principal_id, request.principal_ref)
            or operation is None
            or operation.status not in {"queued", "running"}
            or operation.cancel_requested_at is not None
            or not hmac.compare_digest(operation.tenant_id, request.tenant_ref)
            or not hmac.compare_digest(operation.id, request.external_operation_id)
            or application is None
            or not hmac.compare_digest(application.tenant_id, request.tenant_ref)
            or not hmac.compare_digest(application.id, request.application_ref)
            or source is None
            or source.kind != "git"
            or source.repository is None
            or not hmac.compare_digest(source.repository, request.repository)
            or not hmac.compare_digest(source.id, lease.source_revision_id)
            or not hmac.compare_digest(source.application_id or "", request.application_ref)
            or not hmac.compare_digest(lease.allowed_host, requested_host)
        ):
            return False
        operation_binding = (
            operation.kind == "source.analyze"
            and operation.target_type == "source-revision"
            and hmac.compare_digest(operation.target_id, source.id)
        ) or (
            operation.kind == "application.check-update"
            and operation.target_type == "application"
            and hmac.compare_digest(operation.target_id, request.application_ref)
        )
        if not operation_binding:
            return False

        if lease.source_connection_id is None:
            return bool(
                connection is None
                and source.connection_id is None
                and lease.consumer_binding_hash is None
                and lease.binding_key_version is None
            )
        if (
            connection is None
            or self._key_ring is None
            or connection.revoked_at is not None
            or source.connection_id is None
            or not hmac.compare_digest(source.connection_id, connection.id)
            or not hmac.compare_digest(lease.source_connection_id, connection.id)
            or not hmac.compare_digest(connection.tenant_id, request.tenant_ref)
            or not hmac.compare_digest(connection.allowed_host, requested_host)
            or lease.consumer_binding_hash is None
            or lease.binding_key_version is None
        ):
            return False
        return self._key_ring.verify_lease_binding(
            lease.consumer_binding_hash,
            key_version=lease.binding_key_version,
            tenant_id=request.tenant_ref,
            lease_id=request.lease_id,
            connection_id=connection.id,
            builder_task_id=lease.builder_task_id,
            consumer_id=request.principal_ref,
            allowed_host=requested_host,
        )


__all__ = [
    "CREDENTIAL_REDEMPTION_FIELDS",
    "CREDENTIAL_REDEMPTION_MAX_TTL_SECONDS",
    "CREDENTIAL_REDEMPTION_REQUEST_SCHEMA",
    "CREDENTIAL_REDEMPTION_RESULT_SCHEMA",
    "CredentialRedemptionRequest",
    "CredentialRedemptionResult",
    "PostgresCredentialRedemptionBroker",
]
