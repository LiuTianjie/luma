from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import func, select

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "apps/api/src",
    "packages/python/lae-core/src",
    "packages/python/lae-store/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

try:  # Optional outside migration/integration CI jobs.
    from alembic import command
    from alembic.config import Config
except ImportError:  # pragma: no cover - skip condition handles this
    command = None
    Config = None

from lae_store import (  # noqa: E402
    CreateOperation,
    EventInput,
    OperationStatus,
    OperationStore,
    PostgresPublicResourceStore,
    Principal,
    ResourceNotFound,
    TenantScope,
    create_postgres_engine,
    create_session_factory,
    new_id,
)
from lae_store.models import (  # noqa: E402
    Analysis,
    Application,
    OperationEvent,
    SourceRevision,
    Tenant,
    TenantMember,
    User,
)
from lae_api.app import create_app  # noqa: E402
from lae_store.auth import (  # noqa: E402
    AuthRejected,
    DeployTokenPrincipal,
    SessionPrincipal,
)

DSN = os.environ.get("LAE_TEST_POSTGRES_DSN", "")
DDL_ALLOWED = os.environ.get("LAE_TEST_POSTGRES_ALLOW_DDL") == "1"


class PostgreSQLResourceAuth:
    def __init__(self, *, user_id: str, tenant_id: str) -> None:
        self.token = "lae_dt_0123456789_" + "P" * 43
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.token_id = new_id("dtk")

    async def authenticate(self, _token: str) -> SessionPrincipal:
        raise AuthRejected("invalid")

    async def authenticate_deploy_token(
        self, token: str, *, request_ip: str | None
    ) -> DeployTokenPrincipal:
        del request_ip
        if token != self.token:
            raise AuthRejected("invalid")
        return DeployTokenPrincipal(
            token_id=self.token_id,
            token_prefix="0123456789",
            user_id=self.user_id,
            email="integration@example.test",
            tenant_id=self.tenant_id,
            entitlement_code="lite",
            member_role="owner",
            scopes=frozenset({"analyses:write"}),
        )

    def csrf_valid(
        self,
        _principal: SessionPrincipal,
        *,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> bool:
        del csrf_cookie, csrf_header
        return False


def _alembic_config() -> Config:
    assert Config is not None
    config = Config(str(LAE_ROOT / "migrations" / "alembic.ini"))
    config.set_main_option("script_location", str(LAE_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", DSN)
    return config


def _upgrade() -> None:
    assert command is not None
    command.upgrade(_alembic_config(), "head")


def _downgrade() -> None:
    assert command is not None
    command.downgrade(_alembic_config(), "base")


@unittest.skipUnless(
    DSN and DDL_ALLOWED and command is not None,
    "set LAE_TEST_POSTGRES_DSN and LAE_TEST_POSTGRES_ALLOW_DDL=1 for real PostgreSQL",
)
class PostgreSQLPublicResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.operations = OperationStore(self.sessions)
        self.public = PostgresPublicResourceStore(self.sessions)
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.other_user_id = new_id("usr")
        self.other_tenant_id = new_id("ten")
        self.application_id = new_id("app")
        self.source_id = new_id("src")
        self.analysis_id = new_id("ana")
        async with self.sessions() as session:
            async with session.begin():
                session.add_all(
                    [
                        User(
                            id=self.user_id,
                            email=f"{self.user_id.lower()}@example.test",
                            status="active",
                        ),
                        User(
                            id=self.other_user_id,
                            email=f"{self.other_user_id.lower()}@example.test",
                            status="active",
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        Tenant(
                            id=self.tenant_id,
                            type="personal",
                            name="Public resource tenant",
                            slug=self.tenant_id.lower(),
                            status="active",
                            owner_user_id=self.user_id,
                        ),
                        Tenant(
                            id=self.other_tenant_id,
                            type="personal",
                            name="Foreign tenant",
                            slug=self.other_tenant_id.lower(),
                            status="active",
                            owner_user_id=self.other_user_id,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        TenantMember(
                            tenant_id=self.tenant_id,
                            user_id=self.user_id,
                            role="owner",
                        ),
                        TenantMember(
                            tenant_id=self.other_tenant_id,
                            user_id=self.other_user_id,
                            role="owner",
                        ),
                    ]
                )
                session.add(
                    Application(
                        id=self.application_id,
                        tenant_id=self.tenant_id,
                        name="Public operation app",
                        slug=self.application_id.lower(),
                        luma_name=f"lae-{self.application_id.lower()}",
                        kind="compose",
                    )
                )
                await session.flush()
                session.add(
                    SourceRevision(
                        id=self.source_id,
                        tenant_id=self.tenant_id,
                        application_id=self.application_id,
                        kind="git",
                        repository="https://github.com/acme/example.git",
                        ref="main",
                        subdirectory="",
                    )
                )

        operation = await self.operations.create_operation(
            CreateOperation(
                scope=TenantScope(self.tenant_id),
                principal=Principal("user", self.user_id),
                kind="source.analyze",
                target_type="source-revision",
                target_id=self.source_id,
                phase="source.analyze",
            )
        )
        self.operation_id = operation.id
        deployment_operation = await self.operations.create_operation(
            CreateOperation(
                scope=TenantScope(self.tenant_id),
                principal=Principal("user", self.user_id),
                kind="deployment.create",
                target_type="application",
                target_id=self.application_id,
                phase="deploy.prepare",
            )
        )
        self.deployment_operation_id = deployment_operation.id
        internal_operation = await self.operations.create_operation(
            CreateOperation(
                scope=TenantScope(self.tenant_id),
                principal=Principal("user", self.user_id),
                kind="internal.reconcile",
                target_type="application",
                target_id=self.application_id,
                phase=None,
            )
        )
        self.internal_operation_id = internal_operation.id
        async with self.sessions() as session:
            async with session.begin():
                session.add(
                    Analysis(
                        id=self.analysis_id,
                        tenant_id=self.tenant_id,
                        application_id=self.application_id,
                        source_revision_id=self.source_id,
                        operation_id=self.operation_id,
                        status="queued",
                    )
                )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    async def test_tenant_fenced_replay_redaction_and_cancel_late_success_fence(
        self,
    ) -> None:
        scope = TenantScope(self.tenant_id)
        foreign = TenantScope(self.other_tenant_id)
        operation = await self.public.get_operation(scope, self.operation_id)
        self.assertEqual(operation.kind, "source.analyze")
        analysis = await self.public.get_analysis(scope, self.analysis_id)
        self.assertEqual(analysis.status, "queued")
        self.assertEqual(
            set(analysis.public_body()),
            {
                "id",
                "status",
                "verdict",
                "diagnostic",
                "blockers",
                "digests",
                "planStored",
                "links",
            },
        )
        with self.assertRaises(ResourceNotFound):
            await self.public.get_operation(foreign, self.operation_id)
        with self.assertRaises(ResourceNotFound):
            await self.public.get_analysis(foreign, self.analysis_id)

        first_history = await self.public.list_operations(
            scope,
            allowed_scopes=frozenset({"analyses:write", "deployments:write"}),
            application_id=self.application_id,
            limit=1,
        )
        self.assertTrue(first_history.has_more)
        self.assertEqual(len(first_history.operations), 1)
        history_cursor = first_history.public_body()["nextCursor"]
        self.assertIsNotNone(history_cursor)
        second_history = await self.public.list_operations(
            scope,
            allowed_scopes=frozenset({"analyses:write", "deployments:write"}),
            application_id=self.application_id,
            before=history_cursor,
            limit=1,
        )
        self.assertFalse(second_history.has_more)
        self.assertEqual(
            {
                first_history.operations[0].operation.id,
                second_history.operations[0].operation.id,
            },
            {self.operation_id, self.deployment_operation_id},
        )
        self.assertNotIn(
            self.internal_operation_id,
            {
                first_history.operations[0].operation.id,
                second_history.operations[0].operation.id,
            },
        )
        for item in first_history.operations + second_history.operations:
            self.assertEqual(item.application_id, self.application_id)
        foreign_history = await self.public.list_operations(
            foreign,
            allowed_scopes=frozenset({"analyses:write", "deployments:write"}),
            limit=100,
        )
        self.assertEqual(foreign_history.operations, ())

        claimed = await self.operations.claim_next(
            worker_id="public-resource-worker",
            kinds=["source.analyze"],
            lease_seconds=30,
        )
        assert claimed is not None
        await self.operations.append_event(
            scope,
            self.operation_id,
            EventInput(
                type="builder.analyze.progress",
                phase="source.analyze",
                status="running",
                message="Internal image registry output must not be public",
                data={
                    "replayed": True,
                    "credentialLeaseId": new_id("lease"),
                    "imageRef": "registry.internal/image@sha256:" + "f" * 64,
                    "stdout": "raw output must not escape",
                },
            ),
            worker_id="public-resource-worker",
        )

        cursors: list[int] = []
        after = 0
        while True:
            page = await self.public.list_operation_events(
                scope, self.operation_id, after=after, limit=1
            )
            if page.events:
                cursors.append(page.events[0].cursor)
                serialized = str(page.public_body()).lower()
                self.assertNotIn("credential", serialized)
                self.assertNotIn("registry.internal", serialized)
                self.assertNotIn("raw output", serialized)
            after = page.cursor
            if not page.has_more:
                break
        self.assertEqual(cursors, list(range(1, claimed.last_event_seq + 2)))
        self.assertEqual(cursors, sorted(set(cursors)))

        auth = PostgreSQLResourceAuth(user_id=self.user_id, tenant_id=self.tenant_id)
        api = create_app(auth, None, self.public)
        async with AsyncClient(
            transport=ASGITransport(app=api),
            base_url="https://lae.example.test",
            headers={"Authorization": f"Bearer {auth.token}"},
        ) as client:
            analysis_response = await client.get(f"/v1/analyses/{self.analysis_id}")
            self.assertEqual(analysis_response.status_code, 200)
            self.assertEqual(
                set(analysis_response.json()),
                {
                    "id",
                    "status",
                    "verdict",
                    "diagnostic",
                    "blockers",
                    "digests",
                    "planStored",
                    "links",
                },
            )
            history_response = await client.get(
                "/v1/operations",
                params={"applicationId": self.application_id, "limit": 100},
            )
            self.assertEqual(history_response.status_code, 200)
            self.assertEqual(
                [item["id"] for item in history_response.json()["operations"]],
                [self.operation_id],
            )
            self.assertEqual(
                history_response.json()["operations"][0]["applicationId"],
                self.application_id,
            )
            event_response = await client.get(
                f"/v1/operations/{self.operation_id}/events",
                params={"after": 0, "limit": 100},
            )
            self.assertEqual(event_response.status_code, 200)
            serialized = event_response.text.lower()
            self.assertNotIn("credential", serialized)
            self.assertNotIn("registry.internal", serialized)
            self.assertNotIn("raw output", serialized)
            first_cancel_response = await client.post(
                f"/v1/operations/{self.operation_id}/cancel"
            )
            second_cancel_response = await client.post(
                f"/v1/operations/{self.operation_id}/cancel"
            )
            self.assertEqual(first_cancel_response.status_code, 202)
            self.assertEqual(
                first_cancel_response.json(), second_cancel_response.json()
            )
            self.assertEqual(
                first_cancel_response.headers["idempotency-policy"],
                "state-transition",
            )

        first_cancel = await self.public.get_operation(scope, self.operation_id)
        second_cancel = await self.public.request_cancel(scope, self.operation_id)
        self.assertTrue(first_cancel.cancel_requested)
        self.assertEqual(first_cancel.last_event_seq, second_cancel.last_event_seq)
        terminal = await self.operations.complete(
            scope,
            self.operation_id,
            worker_id="public-resource-worker",
            status=OperationStatus.SUCCEEDED,
            result={"analysisId": self.analysis_id},
        )
        self.assertEqual(terminal.status, "canceled")
        final = await self.public.get_operation(scope, self.operation_id)
        self.assertEqual(final.status, "canceled")
        self.assertNotIn("result", final.public_body())

        # A terminal status does not tell a resumable client to stop until it
        # has replayed through last_event_seq.
        first_terminal_page = await self.public.list_operation_events(
            scope, self.operation_id, after=0, limit=1
        )
        self.assertEqual(first_terminal_page.operation.status, "canceled")
        self.assertFalse(first_terminal_page.public_body()["terminal"])
        drained = await self.public.list_operation_events(
            scope,
            self.operation_id,
            after=final.last_event_seq,
            limit=100,
        )
        self.assertTrue(drained.public_body()["terminal"])
        self.assertFalse(drained.has_more)

        async with self.sessions() as session:
            cancel_requested_events = int(
                await session.scalar(
                    select(func.count())
                    .select_from(OperationEvent)
                    .where(
                        OperationEvent.operation_id == self.operation_id,
                        OperationEvent.type == "operation.cancel-requested",
                    )
                )
                or 0
            )
            canceled_events = int(
                await session.scalar(
                    select(func.count())
                    .select_from(OperationEvent)
                    .where(
                        OperationEvent.operation_id == self.operation_id,
                        OperationEvent.type == "operation.canceled",
                    )
                )
                or 0
            )
        self.assertEqual(cancel_requested_events, 1)
        self.assertEqual(canceled_events, 1)


if __name__ == "__main__":
    unittest.main()
