from __future__ import annotations

import hmac
import ipaddress
import re
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import (
    IdempotencyKeyReused,
    OperationConflict,
    ResourceNotFound,
    SourceConnectionConflict,
    SourceConnectionHostMismatch,
    SourceConnectionUnavailable,
)
from .ids import new_id, require_opaque_id
from .models import (
    Analysis,
    Application,
    BuilderTask,
    IdempotencyRecord,
    Operation,
    SourceConnection,
    SourceCredentialLease,
    SourceRevision,
)
from .repositories import EventInput, IdempotencyInput, Principal, TenantScope, _append_event
from .security import ensure_persistable_payload
from .tokens import keyed_request_hash, keyed_secret_hash

ANALYSIS_CREATE_ROUTE = "/v1/analyses"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_GIT_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\\\[]")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PRIVATE_DNS_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".test",
    ".example",
    ".invalid",
)


@dataclass(frozen=True, slots=True)
class CreateAnalysisRequest:
    scope: TenantScope
    principal: Principal
    application_id: str
    repository: str
    ref: str
    subdirectory: str
    region: str
    public_protocols: tuple[str, ...]
    idempotency_key: str
    connection_id: str | None = None

    def __post_init__(self) -> None:
        require_opaque_id(self.application_id, prefix="app")
        if self.connection_id is not None:
            require_opaque_id(self.connection_id, prefix="conn")
        object.__setattr__(
            self, "repository", canonical_https_repository(self.repository)
        )
        object.__setattr__(self, "ref", canonical_git_ref(self.ref))
        object.__setattr__(
            self, "subdirectory", canonical_subdirectory(self.subdirectory)
        )
        if self.region not in {"cn", "global"}:
            raise ValueError("analysis region is invalid")
        if self.public_protocols != ("http",):
            raise ValueError("only the public HTTP protocol is supported")
        # Reuse the canonical idempotency validator.  The request digest is
        # calculated inside the store so public callers can never choose it.
        IdempotencyInput(
            key=self.idempotency_key,
            method="POST",
            route_template=ANALYSIS_CREATE_ROUTE,
            request_hash=b"\0" * 32,
        )

    def hash_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "applicationId": self.application_id,
            "source": {
                "type": "git",
                "repository": self.repository,
                "ref": self.ref,
                "subdirectory": self.subdirectory,
            },
            "intent": {
                "region": self.region,
                "publicProtocols": list(self.public_protocols),
            },
        }
        if self.connection_id is not None:
            payload["source"]["connectionId"] = self.connection_id
        return payload


@dataclass(frozen=True, slots=True)
class AnalysisRequestRecord:
    analysis_id: str
    operation_id: str
    source_revision_id: str
    application_id: str
    analysis_status: str
    operation_status: str
    replayed: bool

    def public_body(self) -> dict[str, Any]:
        return {
            "analysis": {"id": self.analysis_id, "status": self.analysis_status},
            "operation": {"id": self.operation_id, "status": self.operation_status},
            "links": {
                "analysis": f"/v1/analyses/{self.analysis_id}",
                "events": f"/v1/operations/{self.operation_id}/events",
            },
        }


class PostgresAnalysisRequestStore:
    """Atomically enqueue a tenant-scoped, crash-resumable source analysis."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        luma_cluster_id: str,
        luma_principal_id: str,
        hash_key: bytes,
        hash_key_version: int = 1,
        credential_lease_ttl: timedelta = timedelta(minutes=15),
        connection_key_ring: Any | None = None,
    ) -> None:
        self._sessions = sessions
        self._cluster = _require_identifier(
            luma_cluster_id, field="luma_cluster_id"
        )
        self._principal = _require_identifier(
            luma_principal_id, field="luma_principal_id"
        )
        if not isinstance(hash_key, bytes) or len(hash_key) < 32:
            raise ValueError(
                "analysis request HMAC key must contain at least 256 bits"
            )
        if not isinstance(hash_key_version, int) or hash_key_version < 1:
            raise ValueError("analysis request HMAC key version must be positive")
        if not timedelta(minutes=1) <= credential_lease_ttl <= timedelta(hours=1):
            raise ValueError("credential lease TTL must be between 1 and 60 minutes")
        self._hash_key = hash_key
        self._hash_key_version = hash_key_version
        self._credential_lease_ttl = credential_lease_ttl
        self._connection_key_ring = connection_key_ring

    async def create(self, command: CreateAnalysisRequest) -> AnalysisRequestRecord:
        request_hash = keyed_request_hash(command.hash_payload(), self._hash_key)
        idempotency = IdempotencyInput(
            key=command.idempotency_key,
            method="POST",
            route_template=ANALYSIS_CREATE_ROUTE,
            request_hash=request_hash,
        )
        lock_scope = (
            f"analysis-create:{command.scope.tenant_id}:{command.principal.type}:"
            f"{command.principal.id}:{idempotency.key}"
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    # Serializing exactly one idempotency scope avoids a
                    # rollback/re-query race while leaving unrelated tenants
                    # and principals fully concurrent.
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended(:analysis_create_scope, 0))"
                        ),
                        {"analysis_create_scope": lock_scope},
                    )
                    now = await session.scalar(select(func.now()))
                    if now is None:  # PostgreSQL always returns a value.
                        raise OperationConflict("database clock is unavailable")
                    existing = await session.scalar(
                        select(IdempotencyRecord)
                        .where(
                            IdempotencyRecord.tenant_id == command.scope.tenant_id,
                            IdempotencyRecord.principal_type
                            == command.principal.type,
                            IdempotencyRecord.principal_id == command.principal.id,
                            IdempotencyRecord.method == idempotency.method,
                            IdempotencyRecord.route_template
                            == idempotency.route_template,
                            IdempotencyRecord.key == idempotency.key,
                        )
                        .with_for_update()
                    )
                    if existing is not None and existing.expires_at > now:
                        if not hmac.compare_digest(
                            existing.request_hash, idempotency.request_hash
                        ):
                            raise IdempotencyKeyReused(
                                "idempotency key was used for another request"
                            )
                        return await self._replay(
                            session, command.scope, existing
                        )
                    if existing is not None:
                        await session.delete(existing)
                        await session.flush()

                    application = await session.scalar(
                        select(Application)
                        .where(
                            Application.tenant_id == command.scope.tenant_id,
                            Application.id == command.application_id,
                            Application.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if application is None:
                        # Cross-tenant and nonexistent ids remain
                        # indistinguishable at the public boundary.
                        raise ResourceNotFound("application not found")

                    allowed_host = canonical_allowed_host(command.repository)
                    connection: SourceConnection | None = None
                    if command.connection_id is not None:
                        if self._connection_key_ring is None:
                            # Anonymous Git remains available when the private
                            # connection capability is not configured.
                            raise SourceConnectionUnavailable(
                                "private source connection capability is unavailable"
                            )
                        connection = await session.scalar(
                            select(SourceConnection)
                            .where(
                                SourceConnection.tenant_id
                                == command.scope.tenant_id,
                                SourceConnection.id == command.connection_id,
                                SourceConnection.revoked_at.is_(None),
                            )
                            .with_for_update()
                        )
                        if connection is None:
                            # Foreign, absent and revoked connections are
                            # deliberately indistinguishable.
                            raise ResourceNotFound("source connection not found")
                        if not hmac.compare_digest(
                            connection.allowed_host, allowed_host
                        ):
                            raise SourceConnectionHostMismatch(
                                "repository host does not match source connection"
                            )

                    source = SourceRevision(
                        id=new_id("src"),
                        tenant_id=command.scope.tenant_id,
                        application_id=application.id,
                        kind="git",
                        connection_id=(connection.id if connection is not None else None),
                        repository=command.repository,
                        ref=command.ref,
                        subdirectory=command.subdirectory,
                    )
                    session.add(source)
                    await session.flush()

                    operation = Operation(
                        id=new_id("op"),
                        tenant_id=command.scope.tenant_id,
                        principal_type=command.principal.type,
                        principal_id=command.principal.id,
                        kind="source.analyze",
                        target_type="source-revision",
                        target_id=source.id,
                        status="queued",
                        phase="source.analyze",
                        last_event_seq=0,
                    )
                    session.add(operation)
                    await session.flush()

                    lease_id = new_id("lease")
                    task = BuilderTask(
                        id=new_id("btask"),
                        tenant_id=command.scope.tenant_id,
                        application_id=application.id,
                        source_revision_id=source.id,
                        operation_id=operation.id,
                        luma_cluster_id=self._cluster,
                        luma_principal_id=self._principal,
                        action="source.analyze",
                        credential_lease_id=lease_id,
                        idempotency_key_hash=keyed_secret_hash(
                            _builder_idempotency_key(operation.id),
                            self._hash_key,
                            domain="lae.builder-idempotency.v1",
                        ),
                        request_digest=keyed_request_hash(
                            {
                                "action": "source.analyze",
                                "tenantRef": command.scope.tenant_id,
                                "applicationRef": application.id,
                                "sourceRevisionRef": source.id,
                                "repository": source.repository,
                                "ref": source.ref,
                                "subdirectory": source.subdirectory,
                                "credentialLeaseRef": lease_id,
                            },
                            self._hash_key,
                        ),
                        hash_key_version=self._hash_key_version,
                        event_cursor=0,
                        checkpoint_version=0,
                    )
                    session.add(task)
                    await session.flush()

                    binding_hash: bytes | None = None
                    binding_key_version: int | None = None
                    if connection is not None:
                        binding_key_version = self._connection_key_ring.current_version
                        binding_hash = self._connection_key_ring.lease_binding_digest(
                            key_version=binding_key_version,
                            tenant_id=command.scope.tenant_id,
                            lease_id=lease_id,
                            connection_id=connection.id,
                            builder_task_id=task.id,
                            consumer_id=self._principal,
                            allowed_host=allowed_host,
                        )
                    session.add(
                        SourceCredentialLease(
                            id=lease_id,
                            tenant_id=command.scope.tenant_id,
                            source_connection_id=(
                                connection.id if connection is not None else None
                            ),
                            source_revision_id=source.id,
                            operation_id=operation.id,
                            builder_task_id=task.id,
                            allowed_action="source.fetch",
                            allowed_host=allowed_host,
                            consumer_id=self._principal,
                            consumer_binding_hash=binding_hash,
                            binding_key_version=binding_key_version,
                            status="issued",
                            expires_at=now + self._credential_lease_ttl,
                        )
                    )
                    analysis = Analysis(
                        id=new_id("ana"),
                        tenant_id=command.scope.tenant_id,
                        application_id=application.id,
                        source_revision_id=source.id,
                        operation_id=operation.id,
                        status="queued",
                        policy_version=None,
                        agent_image_digest=None,
                        resolved_commit_full=None,
                        source_tree_digest=None,
                        source_snapshot_id=None,
                        source_snapshot_digest=None,
                        deployment_plan_digest=None,
                        build_plan_digest=None,
                        evidence_digest=None,
                        artifact_state="descriptor-only",
                        plan_stored=False,
                    )
                    session.add(analysis)
                    await _append_event(
                        session,
                        operation,
                        EventInput(
                            type="operation.queued",
                            phase="source.analyze",
                            status="queued",
                            message="Operation queued",
                            data={},
                        ),
                    )
                    response_body = AnalysisRequestRecord(
                        analysis_id=analysis.id,
                        operation_id=operation.id,
                        source_revision_id=source.id,
                        application_id=application.id,
                        analysis_status=analysis.status,
                        operation_status=operation.status,
                        replayed=False,
                    ).public_body()
                    ensure_persistable_payload(response_body)
                    session.add(
                        IdempotencyRecord(
                            tenant_id=command.scope.tenant_id,
                            principal_type=command.principal.type,
                            principal_id=command.principal.id,
                            key=idempotency.key,
                            method=idempotency.method,
                            route_template=idempotency.route_template,
                            request_hash=idempotency.request_hash,
                            response_status=202,
                            response_body=response_body,
                            operation_id=operation.id,
                            expires_at=now + idempotency.retention,
                        )
                    )
                    await session.flush()
                    return AnalysisRequestRecord(
                        analysis_id=analysis.id,
                        operation_id=operation.id,
                        source_revision_id=source.id,
                        application_id=application.id,
                        analysis_status=analysis.status,
                        operation_status=operation.status,
                        replayed=False,
                    )
        except (
            IdempotencyKeyReused,
            ResourceNotFound,
            SourceConnectionConflict,
            SourceConnectionHostMismatch,
            SourceConnectionUnavailable,
        ):
            raise
        except IntegrityError as exc:
            raise OperationConflict(
                "analysis request conflicts with durable state"
            ) from exc
        except DBAPIError:
            raise

    @staticmethod
    async def _replay(
        session: AsyncSession,
        scope: TenantScope,
        idempotency: IdempotencyRecord,
    ) -> AnalysisRequestRecord:
        operation_id = idempotency.operation_id
        row = (
            await session.execute(
                select(Analysis, Operation)
                .join(
                    Operation,
                    (Operation.tenant_id == Analysis.tenant_id)
                    & (Operation.id == Analysis.operation_id),
                )
                .where(
                    Analysis.tenant_id == scope.tenant_id,
                    Analysis.operation_id == operation_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise OperationConflict("idempotent analysis state is incomplete")
        analysis, operation = row
        body = idempotency.response_body
        try:
            analysis_response = body["analysis"]
            operation_response = body["operation"]
            links = body["links"]
            if (
                set(body) != {"analysis", "operation", "links"}
                or set(analysis_response) != {"id", "status"}
                or set(operation_response) != {"id", "status"}
                or set(links) != {"analysis", "events"}
                or analysis_response["id"] != analysis.id
                or operation_response["id"] != operation.id
                or analysis_response["status"] != "queued"
                or operation_response["status"] != "queued"
                or idempotency.response_status != 202
            ):
                raise KeyError("invalid historical response")
        except (KeyError, TypeError) as exc:
            raise OperationConflict("idempotent analysis response is invalid") from exc
        return AnalysisRequestRecord(
            analysis_id=analysis.id,
            operation_id=operation.id,
            source_revision_id=analysis.source_revision_id,
            application_id=analysis.application_id,
            analysis_status=str(analysis_response["status"]),
            operation_status=str(operation_response["status"]),
            replayed=True,
        )


def canonical_https_repository(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("repository must be a bounded HTTPS URL")
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("repository port is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path
        or parsed.path == "/"
        or parsed.path.startswith("//")
        or "\\" in parsed.path
    ):
        raise ValueError("repository must be a credential-free HTTPS URL")
    hostname = parsed.hostname.lower().rstrip(".")
    labels = hostname.split(".")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip_literal = False
    else:
        is_ip_literal = True
    if (
        not hostname
        or not hostname.isascii()
        or len(hostname) > 253
        or port == 0
        or is_ip_literal
        or hostname == "localhost"
        or "." not in hostname
        or any(
            hostname == suffix.removeprefix(".") or hostname.endswith(suffix)
            for suffix in _PRIVATE_DNS_SUFFIXES
        )
        or any(_DNS_LABEL.fullmatch(label) is None for label in labels)
        or all(label.isdigit() for label in labels)
    ):
        raise ValueError("repository host is invalid")
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = host if port in {None, 443} else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(("https", authority, path, "", ""))


def canonical_allowed_host(repository: str) -> str:
    parsed = urllib.parse.urlsplit(canonical_https_repository(repository))
    port = parsed.port
    assert parsed.hostname is not None
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    return host if port in {None, 443} else f"{host}:{port}"


def canonical_git_ref(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 255
        or _GIT_REF_FORBIDDEN.search(value)
        or value.startswith(("-", ".", "/"))
        or value.endswith((".", "/", ".lock"))
        or ".." in value
        or "@{" in value
        or "//" in value
    ):
        raise ValueError("Git ref is invalid")
    return value


def canonical_subdirectory(value: str) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("source subdirectory is invalid")
    if not value:
        return ""
    if (
        value.startswith("/")
        or any(character in value for character in "\x00\r\n\\")
    ):
        raise ValueError("source subdirectory is invalid")
    normalized = value.strip("/")
    parts = normalized.split("/")
    if not normalized or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("source subdirectory must stay within the repository")
    return normalized


def _builder_idempotency_key(operation_id: str) -> str:
    require_opaque_id(operation_id, prefix="op")
    return f"lae:{operation_id}:source-analyze:v1"


def _require_identifier(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} has invalid format")
    return value
