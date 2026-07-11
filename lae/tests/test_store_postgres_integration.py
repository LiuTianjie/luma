from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, update

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

try:  # Optional outside migration/integration CI jobs.
    from alembic import command
    from alembic.config import Config
except ImportError:  # pragma: no cover - skip condition handles this
    command = None
    Config = None

from lae_store.engine import create_postgres_engine, create_session_factory  # noqa: E402
from lae_store.errors import (  # noqa: E402
    IdempotencyKeyReused,
    LeaseLost,
    OperationConflict,
)
from lae_store.ids import new_id  # noqa: E402
from lae_store.models import (  # noqa: E402
    Application,
    Operation,
    OutboxEvent,
    Tenant,
    TenantMember,
    User,
)
from lae_store.repositories import (  # noqa: E402
    CreateOperation,
    EventInput,
    IdempotencyInput,
    OperationStore,
    OutboxStore,
    Principal,
    TenantRepository,
    TenantScope,
)
from lae_store.state import OperationStatus  # noqa: E402

DSN = os.environ.get("LAE_TEST_POSTGRES_DSN", "")
DDL_ALLOWED = os.environ.get("LAE_TEST_POSTGRES_ALLOW_DDL") == "1"


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
class PostgreSQLStoreIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.store = OperationStore(self.sessions)
        self.outbox = OutboxStore(self.sessions)
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.application_id = new_id("app")
        self.scope = TenantScope(self.tenant_id)
        self.principal = Principal("user", self.user_id)
        async with self.sessions() as session:
            async with session.begin():
                session.add(
                    User(
                        id=self.user_id,
                        email=f"{self.user_id.lower()}@example.test",
                        status="active",
                    )
                )
                await session.flush()
                session.add(
                    Tenant(
                        id=self.tenant_id,
                        type="personal",
                        name="Integration Tenant",
                        slug=self.tenant_id.lower(),
                        status="active",
                        owner_user_id=self.user_id,
                    )
                )
                await session.flush()
                session.add(
                    TenantMember(
                        tenant_id=self.tenant_id,
                        user_id=self.user_id,
                        role="owner",
                    )
                )
                await session.flush()
                session.add(
                    Application(
                        id=self.application_id,
                        tenant_id=self.tenant_id,
                        name="Integration App",
                        slug=self.application_id.lower(),
                        luma_name=f"lae-{self.application_id.lower()}",
                        kind="compose",
                    )
                )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    def _create(self, key: str, request_hash: bytes) -> CreateOperation:
        return CreateOperation(
            scope=self.scope,
            principal=self.principal,
            kind="deployment.create",
            target_type="application",
            target_id=self.application_id,
            phase="deploy.prepare",
            idempotency=IdempotencyInput(
                key=key,
                method="POST",
                route_template="/v1/applications/{applicationId}/deployments",
                request_hash=request_hash,
            ),
        )

    async def test_durable_queue_idempotency_reclaim_events_cancel_and_outbox(
        self,
    ) -> None:
        created = await self.store.create_operation(
            self._create("same-request", b"a" * 32)
        )
        replay = await self.store.create_operation(
            self._create("same-request", b"a" * 32)
        )
        self.assertEqual(replay.id, created.id)
        with self.assertRaises(IdempotencyKeyReused):
            await self.store.create_operation(self._create("same-request", b"b" * 32))

        claims = await asyncio.gather(
            self.store.claim_next(
                worker_id="worker-a", kinds=["deployment.create"], lease_seconds=30
            ),
            self.store.claim_next(
                worker_id="worker-b", kinds=["deployment.create"], lease_seconds=30
            ),
        )
        claimed = next(item for item in claims if item is not None)
        self.assertEqual(sum(item is not None for item in claims), 1)
        owner = claimed.lease_owner
        assert owner is not None

        requested = await self.store.request_cancel(self.scope, created.id)
        self.assertTrue(requested.cancel_requested)
        heartbeat = await self.store.heartbeat(
            self.scope, created.id, worker_id=owner, lease_seconds=30
        )
        self.assertTrue(heartbeat.cancel_requested)
        terminal = await self.store.complete(
            self.scope,
            created.id,
            worker_id=owner,
            status=OperationStatus.SUCCEEDED,
            result={"deploymentId": new_id("dep")},
        )
        self.assertEqual(terminal.status, OperationStatus.CANCELED.value)
        self.assertIsNone(terminal.result)

        second = await self.store.create_operation(self._create("second", b"c" * 32))
        with self.assertRaises(OperationConflict):
            await self.store.create_operation(self._create("third", b"d" * 32))
        second_claim = await self.store.claim_next(
            worker_id="worker-a", kinds=["deployment.create"], lease_seconds=30
        )
        assert second_claim is not None
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(Operation)
                    .where(
                        Operation.id == second.id, Operation.tenant_id == self.tenant_id
                    )
                    .values(lease_expires_at=func.now() - timedelta(seconds=1))
                )
        reclaimed = await self.store.claim_next(
            worker_id="worker-b", kinds=["deployment.create"], lease_seconds=30
        )
        assert reclaimed is not None
        self.assertEqual(reclaimed.id, second.id)
        self.assertEqual(reclaimed.lease_attempt, 2)
        with self.assertRaises(LeaseLost):
            await self.store.append_event(
                self.scope,
                second.id,
                EventInput(
                    type="operation.progress",
                    phase="deploy.prepare",
                    status="running",
                    message="Stale worker event",
                    data={},
                ),
                worker_id="worker-a",
            )

        await asyncio.gather(
            *[
                self.store.append_event(
                    self.scope,
                    second.id,
                    EventInput(
                        type="operation.progress",
                        phase="deploy.prepare",
                        status="running",
                        message=f"Safe progress {index}",
                        data={"step": index},
                    ),
                    worker_id="worker-b",
                )
                for index in range(12)
            ]
        )
        async with self.sessions() as session:
            events = await TenantRepository(session, self.scope).list_operation_events(
                second.id, limit=100
            )
        sequences = [event.seq for event in events]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

        await self.store.complete(
            self.scope,
            second.id,
            worker_id="worker-b",
            status=OperationStatus.SUCCEEDED,
            result={"deploymentId": new_id("dep")},
        )
        third = await self.store.create_operation(
            self._create("recovered-cancel", b"e" * 32)
        )
        third_claim = await self.store.claim_next(
            worker_id="worker-c", kinds=["deployment.create"], lease_seconds=30
        )
        assert third_claim is not None
        await self.store.request_cancel(self.scope, third.id)
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(Operation)
                    .where(
                        Operation.id == third.id, Operation.tenant_id == self.tenant_id
                    )
                    .values(lease_expires_at=func.now() - timedelta(seconds=1))
                )
        self.assertIsNone(
            await self.store.claim_next(
                worker_id="worker-d", kinds=["deployment.create"], lease_seconds=30
            )
        )
        async with self.sessions() as session:
            recovered_cancel = await TenantRepository(
                session, self.scope
            ).get_operation(third.id)
        self.assertEqual(recovered_cancel.status, OperationStatus.CANCELED.value)

        outbox = await self.outbox.claim_next(worker_id="publisher-a")
        self.assertIsNotNone(outbox)
        assert outbox is not None
        await self.outbox.mark_published(outbox.id, worker_id="publisher-a")
        async with self.sessions() as session:
            published = await session.scalar(
                select(OutboxEvent.status).where(OutboxEvent.id == outbox.id)
            )
            operation_count = await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.tenant_id == self.tenant_id)
            )
        self.assertEqual(published, "published")
        self.assertEqual(operation_count, 3)


if __name__ == "__main__":
    unittest.main()
