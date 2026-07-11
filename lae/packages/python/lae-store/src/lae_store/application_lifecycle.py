from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .analysis_requests import canonical_allowed_host
from .errors import (
    ApplicationLifecycleConflict,
    ApplicationLifecycleSourceUnavailable,
    ApplicationLifecycleStateConflict,
    ApplicationRollbackUnavailable,
    IdempotencyKeyReused,
    OperationConflict,
    ResourceNotFound,
    SourceConnectionUnavailable,
)
from .ids import new_id, require_opaque_id
from .models import (
    APPLICATION_MUTATION_KINDS,
    Analysis,
    AppRevision,
    Application,
    ApplicationLifecycleRequest,
    ApplicationService,
    BuilderTask,
    Deployment,
    IdempotencyRecord,
    Operation,
    SourceConnection,
    SourceCredentialLease,
    SourceRevision,
)
from .repositories import EventInput, IdempotencyInput, Principal, TenantScope, _append_event
from .security import ensure_persistable_payload
from .tokens import keyed_request_hash, keyed_secret_hash


APPLICATION_ACTION_ROUTE = "/v1/applications/{application_id}/actions/{action}"
APPLICATION_ACTIONS = frozenset(
    {"check-update", "suspend", "resume", "restart", "rollback", "delete"}
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTIVE_STATUSES = ("queued", "running")


@dataclass(frozen=True, slots=True)
class UpdateCheckBinding:
    luma_cluster_id: str
    luma_principal_id: str
    hash_key: bytes
    hash_key_version: int = 1
    credential_lease_ttl: timedelta = timedelta(minutes=15)
    connection_key_ring: Any | None = None

    def __post_init__(self) -> None:
        for value in (self.luma_cluster_id, self.luma_principal_id):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError("update-check Luma binding is invalid")
        if not isinstance(self.hash_key, bytes) or len(self.hash_key) < 32:
            raise ValueError("update-check HMAC key must contain at least 256 bits")
        if not isinstance(self.hash_key_version, int) or self.hash_key_version < 1:
            raise ValueError("update-check HMAC key version must be positive")
        if not timedelta(minutes=1) <= self.credential_lease_ttl <= timedelta(hours=1):
            raise ValueError("update-check credential lease TTL is invalid")


@dataclass(frozen=True, slots=True)
class RequestApplicationAction:
    scope: TenantScope
    application_id: str
    action: str
    rollback_deployment_id: str | None = None

    def __post_init__(self) -> None:
        require_opaque_id(self.application_id, prefix="app")
        if self.action not in APPLICATION_ACTIONS:
            raise ValueError("application action is unsupported")
        if self.rollback_deployment_id is not None:
            require_opaque_id(self.rollback_deployment_id, prefix="dep")
        if self.action != "rollback" and self.rollback_deployment_id is not None:
            raise ValueError("rollback deployment is only valid for rollback")


@dataclass(frozen=True, slots=True)
class ApplicationActionResult:
    body: dict[str, Any]
    replayed: bool

    def __post_init__(self) -> None:
        ensure_persistable_payload(self.body)


class PostgresApplicationLifecycleStore:
    """Atomic, tenant-fenced admission for application lifecycle actions."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        idempotency_hash_key: bytes,
        update_check: UpdateCheckBinding | None = None,
    ) -> None:
        if (
            not isinstance(idempotency_hash_key, bytes)
            or len(idempotency_hash_key) < 32
        ):
            raise ValueError("lifecycle idempotency HMAC key must be at least 256 bits")
        self._sessions = sessions
        self._idempotency_hash_key = idempotency_hash_key
        self._update_check = update_check

    def idempotency(
        self,
        command: RequestApplicationAction,
        *,
        key: str,
    ) -> IdempotencyInput:
        return IdempotencyInput(
            key=key,
            method="POST",
            route_template=APPLICATION_ACTION_ROUTE,
            request_hash=keyed_request_hash(
                {
                    "applicationId": command.application_id,
                    "action": command.action,
                    "rollbackDeploymentId": command.rollback_deployment_id,
                },
                self._idempotency_hash_key,
            ),
        )

    async def request(
        self,
        command: RequestApplicationAction,
        *,
        principal: Principal,
        idempotency: IdempotencyInput,
    ) -> ApplicationActionResult:
        self._validate_idempotency(idempotency)
        lock_scope = (
            f"lae:lifecycle:{command.scope.tenant_id}:{principal.type}:"
            f"{principal.id}:{idempotency.key}"
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended(:lifecycle_idempotency_scope, 0))"
                        ),
                        {"lifecycle_idempotency_scope": lock_scope},
                    )
                    now = await self._database_now(session)
                    existing = await self._idempotency_record(
                        session,
                        command.scope,
                        principal,
                        idempotency,
                        for_update=True,
                    )
                    if existing is not None and existing.expires_at > now:
                        return self._replay(existing, idempotency)
                    if existing is not None:
                        await session.delete(existing)
                        await session.flush()

                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended(:application_scope, 0))"
                        ),
                        {
                            "application_scope": (
                                f"lae:application-catalog:{command.scope.tenant_id}:"
                                f"{command.application_id}"
                            )
                        },
                    )
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
                        raise ResourceNotFound("application not found")
                    if application.desired_state not in {"running", "suspended"}:
                        raise ApplicationLifecycleStateConflict(
                            "application is already pending deletion"
                        )

                    active = await session.scalar(
                        select(Operation.id).where(
                            Operation.tenant_id == command.scope.tenant_id,
                            Operation.target_type == "application",
                            Operation.target_id == application.id,
                            Operation.kind.in_(APPLICATION_MUTATION_KINDS),
                            Operation.status.in_(_ACTIVE_STATUSES),
                        )
                    )
                    if active is not None:
                        raise ApplicationLifecycleConflict(
                            "an application mutation is already in progress"
                        )

                    current_deployment, current_revision = await self._current_binding(
                        session, command.scope, application
                    )
                    rollback = await self._validate_transition(
                        session,
                        command,
                        application,
                        current_deployment=current_deployment,
                    )

                    operation = Operation(
                        id=new_id("op"),
                        tenant_id=command.scope.tenant_id,
                        principal_type=principal.type,
                        principal_id=principal.id,
                        kind=f"application.{command.action}",
                        target_type="application",
                        target_id=application.id,
                        status="queued",
                        phase=(
                            "source.analyze"
                            if command.action == "check-update"
                            else "application.lifecycle"
                        ),
                        last_event_seq=0,
                    )
                    session.add(operation)
                    await session.flush()

                    base_source: SourceRevision | None = None
                    source: SourceRevision | None = None
                    analysis: Analysis | None = None
                    if command.action == "check-update":
                        base_source, source, analysis = await self._create_update_check(
                            session,
                            command.scope,
                            application,
                            operation,
                            current_revision=current_revision,
                            now=now,
                        )

                    requested_state = self._requested_state(command.action)
                    previous_state = application.desired_state
                    if requested_state is not None:
                        application.desired_state = requested_state
                        services = await session.scalars(
                            select(ApplicationService)
                            .where(
                                ApplicationService.tenant_id
                                == command.scope.tenant_id,
                                ApplicationService.application_id == application.id,
                            )
                            .with_for_update()
                        )
                        for service in services:
                            service.desired_state = requested_state
                            service.updated_at = now
                        application.updated_at = now

                    binding_source_id = (
                        source.id
                        if source is not None
                        else current_revision.source_revision_id
                        if current_revision is not None
                        else None
                    )
                    session.add(
                        ApplicationLifecycleRequest(
                            operation_id=operation.id,
                            tenant_id=command.scope.tenant_id,
                            application_id=application.id,
                            action=command.action,
                            previous_desired_state=previous_state,
                            requested_desired_state=requested_state,
                            base_source_revision_id=(
                                base_source.id if base_source is not None else None
                            ),
                            source_revision_id=binding_source_id,
                            source_deployment_id=(
                                current_deployment.id
                                if current_deployment is not None
                                else None
                            ),
                            rollback_deployment_id=(
                                rollback.id if rollback is not None else None
                            ),
                            analysis_id=analysis.id if analysis is not None else None,
                        )
                    )
                    await _append_event(
                        session,
                        operation,
                        EventInput(
                            type="operation.queued",
                            phase=operation.phase,
                            status="queued",
                            message="Operation queued",
                            data={},
                        ),
                    )
                    body = self._public_body(
                        application,
                        operation,
                        analysis=analysis,
                        rollback=rollback,
                    )
                    ensure_persistable_payload(body)
                    session.add(
                        IdempotencyRecord(
                            tenant_id=command.scope.tenant_id,
                            principal_type=principal.type,
                            principal_id=principal.id,
                            key=idempotency.key,
                            method=idempotency.method,
                            route_template=idempotency.route_template,
                            request_hash=idempotency.request_hash,
                            response_status=202,
                            response_body=body,
                            operation_id=operation.id,
                            expires_at=now + idempotency.retention,
                        )
                    )
                    await session.flush()
                    return ApplicationActionResult(body, replayed=False)
        except (
            ApplicationLifecycleConflict,
            ApplicationLifecycleSourceUnavailable,
            ApplicationLifecycleStateConflict,
            ApplicationRollbackUnavailable,
            IdempotencyKeyReused,
            ResourceNotFound,
            SourceConnectionUnavailable,
        ):
            raise
        except IntegrityError as exc:
            # The exact idempotency scope may have won a concurrent insert.
            async with self._sessions() as session:
                existing = await self._idempotency_record(
                    session,
                    command.scope,
                    principal,
                    idempotency,
                    for_update=False,
                )
                if existing is not None:
                    return self._replay(existing, idempotency)
            raise ApplicationLifecycleConflict(
                "application lifecycle state changed concurrently"
            ) from exc

    async def get_request(
        self, scope: TenantScope, operation_id: str
    ) -> ApplicationLifecycleRequest:
        """Internal worker/reconciler boundary for immutable request facts."""

        require_opaque_id(operation_id, prefix="op")
        async with self._sessions() as session:
            request = await session.scalar(
                select(ApplicationLifecycleRequest).where(
                    ApplicationLifecycleRequest.tenant_id == scope.tenant_id,
                    ApplicationLifecycleRequest.operation_id == operation_id,
                )
            )
        if request is None:
            raise ResourceNotFound("application lifecycle request not found")
        return request

    async def _validate_transition(
        self,
        session: AsyncSession,
        command: RequestApplicationAction,
        application: Application,
        *,
        current_deployment: Deployment | None,
    ) -> Deployment | None:
        action = command.action
        if action == "suspend" and application.desired_state != "running":
            raise ApplicationLifecycleStateConflict("application is not running")
        if action == "resume" and application.desired_state != "suspended":
            raise ApplicationLifecycleStateConflict("application is not suspended")
        if action in {"restart", "rollback"} and application.desired_state != "running":
            raise ApplicationLifecycleStateConflict("application is not running")
        if action in {"suspend", "resume", "restart", "rollback"}:
            if (
                application.kind == "pending"
                or current_deployment is None
                or current_deployment.status != "succeeded"
            ):
                raise ApplicationLifecycleStateConflict(
                    "application has no active deployment"
                )
        if action != "rollback":
            return None

        target_id = command.rollback_deployment_id
        if target_id is None and current_deployment is not None:
            target_id = current_deployment.previous_deployment_id
        if target_id is None or (
            current_deployment is not None and target_id == current_deployment.id
        ):
            raise ApplicationRollbackUnavailable(
                "application has no previous deployment"
            )
        target = await session.scalar(
            select(Deployment).where(
                Deployment.tenant_id == command.scope.tenant_id,
                Deployment.application_id == application.id,
                Deployment.id == target_id,
                Deployment.status == "succeeded",
            )
        )
        if target is None:
            # Foreign, failed, absent and another application's deployment are
            # deliberately indistinguishable.
            raise ApplicationRollbackUnavailable(
                "rollback deployment is unavailable"
            )
        return target

    async def _current_binding(
        self,
        session: AsyncSession,
        scope: TenantScope,
        application: Application,
    ) -> tuple[Deployment | None, AppRevision | None]:
        deployment: Deployment | None = None
        revision: AppRevision | None = None
        if application.current_deployment_id is not None:
            deployment = await session.scalar(
                select(Deployment).where(
                    Deployment.tenant_id == scope.tenant_id,
                    Deployment.application_id == application.id,
                    Deployment.id == application.current_deployment_id,
                )
            )
            if deployment is None:
                raise ApplicationLifecycleConflict(
                    "application deployment binding is incomplete"
                )
        if application.current_revision_id is not None:
            revision = await session.scalar(
                select(AppRevision).where(
                    AppRevision.tenant_id == scope.tenant_id,
                    AppRevision.application_id == application.id,
                    AppRevision.id == application.current_revision_id,
                )
            )
            if revision is None:
                raise ApplicationLifecycleConflict(
                    "application revision binding is incomplete"
                )
        if (deployment is None) != (revision is None):
            raise ApplicationLifecycleConflict(
                "application active bindings are inconsistent"
            )
        if deployment is not None and deployment.revision_id != revision.id:
            raise ApplicationLifecycleConflict(
                "application active deployment does not match its revision"
            )
        return deployment, revision

    async def _create_update_check(
        self,
        session: AsyncSession,
        scope: TenantScope,
        application: Application,
        operation: Operation,
        *,
        current_revision: AppRevision | None,
        now: datetime,
    ) -> tuple[SourceRevision, SourceRevision, Analysis]:
        config = self._update_check
        if config is None:
            raise ApplicationLifecycleSourceUnavailable(
                "update-check analysis binding is unavailable"
            )
        base_source: SourceRevision | None = None
        if current_revision is not None:
            base_source = await session.scalar(
                select(SourceRevision).where(
                    SourceRevision.tenant_id == scope.tenant_id,
                    SourceRevision.application_id == application.id,
                    SourceRevision.id == current_revision.source_revision_id,
                    SourceRevision.deleted_at.is_(None),
                )
            )
        if base_source is None:
            base_source = await session.scalar(
                select(SourceRevision)
                .where(
                    SourceRevision.tenant_id == scope.tenant_id,
                    SourceRevision.application_id == application.id,
                    SourceRevision.kind == "git",
                    SourceRevision.repository.is_not(None),
                    SourceRevision.ref.is_not(None),
                    SourceRevision.deleted_at.is_(None),
                )
                .order_by(SourceRevision.created_at.desc(), SourceRevision.id.desc())
                .limit(1)
            )
        if (
            base_source is None
            or base_source.kind != "git"
            or not base_source.repository
            or not base_source.ref
        ):
            raise ApplicationLifecycleSourceUnavailable(
                "application does not have a reusable Git source"
            )

        connection: SourceConnection | None = None
        if base_source.connection_id is not None:
            if config.connection_key_ring is None:
                raise SourceConnectionUnavailable(
                    "private source connection capability is unavailable"
                )
            connection = await session.scalar(
                select(SourceConnection)
                .where(
                    SourceConnection.tenant_id == scope.tenant_id,
                    SourceConnection.id == base_source.connection_id,
                    SourceConnection.revoked_at.is_(None),
                )
                .with_for_update()
            )
            if connection is None:
                raise ApplicationLifecycleSourceUnavailable(
                    "application source connection is unavailable"
                )
            expected_host = canonical_allowed_host(base_source.repository)
            if not hmac.compare_digest(connection.allowed_host, expected_host):
                raise ApplicationLifecycleSourceUnavailable(
                    "application source connection binding is invalid"
                )

        source = SourceRevision(
            id=new_id("src"),
            tenant_id=scope.tenant_id,
            application_id=application.id,
            kind="git",
            connection_id=base_source.connection_id,
            repository=base_source.repository,
            ref=base_source.ref,
            subdirectory=base_source.subdirectory,
        )
        session.add(source)
        await session.flush()

        lease_id = new_id("lease")
        builder_idempotency = f"lae:{operation.id}:source-analyze:v1"
        task = BuilderTask(
            id=new_id("btask"),
            tenant_id=scope.tenant_id,
            application_id=application.id,
            source_revision_id=source.id,
            operation_id=operation.id,
            luma_cluster_id=config.luma_cluster_id,
            luma_principal_id=config.luma_principal_id,
            action="source.analyze",
            credential_lease_id=lease_id,
            idempotency_key_hash=keyed_secret_hash(
                builder_idempotency,
                config.hash_key,
                domain="lae.builder-idempotency.v1",
            ),
            request_digest=keyed_request_hash(
                {
                    "action": "source.analyze",
                    "tenantRef": scope.tenant_id,
                    "applicationRef": application.id,
                    "sourceRevisionRef": source.id,
                    "repository": source.repository,
                    "ref": source.ref,
                    "subdirectory": source.subdirectory,
                    "credentialLeaseRef": lease_id,
                },
                config.hash_key,
            ),
            hash_key_version=config.hash_key_version,
            event_cursor=0,
            checkpoint_version=0,
        )
        session.add(task)
        await session.flush()

        allowed_host = canonical_allowed_host(source.repository)
        binding_hash: bytes | None = None
        binding_key_version: int | None = None
        if connection is not None:
            binding_key_version = config.connection_key_ring.current_version
            binding_hash = config.connection_key_ring.lease_binding_digest(
                key_version=binding_key_version,
                tenant_id=scope.tenant_id,
                lease_id=lease_id,
                connection_id=connection.id,
                builder_task_id=task.id,
                consumer_id=config.luma_principal_id,
                allowed_host=allowed_host,
            )
        session.add(
            SourceCredentialLease(
                id=lease_id,
                tenant_id=scope.tenant_id,
                source_connection_id=(connection.id if connection is not None else None),
                source_revision_id=source.id,
                operation_id=operation.id,
                builder_task_id=task.id,
                allowed_action="source.fetch",
                allowed_host=allowed_host,
                consumer_id=config.luma_principal_id,
                consumer_binding_hash=binding_hash,
                binding_key_version=binding_key_version,
                status="issued",
                expires_at=now + config.credential_lease_ttl,
            )
        )
        analysis = Analysis(
            id=new_id("ana"),
            tenant_id=scope.tenant_id,
            application_id=application.id,
            source_revision_id=source.id,
            operation_id=operation.id,
            status="queued",
            artifact_state="descriptor-only",
            plan_stored=False,
        )
        session.add(analysis)
        await session.flush()
        return base_source, source, analysis

    @staticmethod
    def _requested_state(action: str) -> str | None:
        return {
            "suspend": "suspended",
            "resume": "running",
            "rollback": "running",
            "delete": "deleted",
        }.get(action)

    @staticmethod
    def _public_body(
        application: Application,
        operation: Operation,
        *,
        analysis: Analysis | None,
        rollback: Deployment | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "application": {
                "id": application.id,
                "desiredState": application.desired_state,
                "observedState": application.observed_state,
            },
            "operation": {
                "id": operation.id,
                "kind": operation.kind,
                "status": operation.status,
                "phase": operation.phase,
                "cursor": operation.last_event_seq,
                "links": {
                    "operation": f"/v1/operations/{operation.id}",
                    "events": f"/v1/operations/{operation.id}/events",
                },
            },
        }
        if analysis is not None:
            body["analysis"] = {
                "id": analysis.id,
                "status": analysis.status,
                "links": {"analysis": f"/v1/analyses/{analysis.id}"},
            }
        if rollback is not None:
            body["rollback"] = {"deploymentId": rollback.id}
        return body

    @staticmethod
    def _validate_idempotency(idempotency: IdempotencyInput) -> None:
        if (
            idempotency.method != "POST"
            or idempotency.route_template != APPLICATION_ACTION_ROUTE
        ):
            raise ValueError("lifecycle idempotency scope is invalid")

    @staticmethod
    async def _database_now(session: AsyncSession) -> datetime:
        value = await session.scalar(select(func.now()))
        return value or datetime.now(timezone.utc)

    @staticmethod
    async def _idempotency_record(
        session: AsyncSession,
        scope: TenantScope,
        principal: Principal,
        idempotency: IdempotencyInput,
        *,
        for_update: bool,
    ) -> IdempotencyRecord | None:
        statement = select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == scope.tenant_id,
            IdempotencyRecord.principal_type == principal.type,
            IdempotencyRecord.principal_id == principal.id,
            IdempotencyRecord.method == idempotency.method,
            IdempotencyRecord.route_template == idempotency.route_template,
            IdempotencyRecord.key == idempotency.key,
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    @staticmethod
    def _replay(
        existing: IdempotencyRecord, idempotency: IdempotencyInput
    ) -> ApplicationActionResult:
        if not hmac.compare_digest(existing.request_hash, idempotency.request_hash):
            raise IdempotencyKeyReused("idempotency key was used for another request")
        body = existing.response_body
        try:
            operation = body["operation"]
            application = body["application"]
            if (
                existing.response_status != 202
                or not isinstance(operation, dict)
                or not isinstance(application, dict)
                or operation.get("id") != existing.operation_id
                or operation.get("status") != "queued"
                or not isinstance(operation.get("kind"), str)
                or not operation["kind"].startswith("application.")
                or not isinstance(application.get("id"), str)
            ):
                raise KeyError("invalid lifecycle replay")
        except (KeyError, TypeError) as exc:
            raise OperationConflict("idempotent lifecycle response is invalid") from exc
        return ApplicationActionResult(dict(body), replayed=True)


__all__ = [
    "APPLICATION_ACTION_ROUTE",
    "APPLICATION_ACTIONS",
    "ApplicationActionResult",
    "PostgresApplicationLifecycleStore",
    "RequestApplicationAction",
    "UpdateCheckBinding",
]
