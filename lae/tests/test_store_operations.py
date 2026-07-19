from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.dialects import postgresql

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

from lae_store.errors import InvalidOperationTransition  # noqa: E402
from lae_store.ids import new_id  # noqa: E402
from lae_store.repositories import (  # noqa: E402
    CreateOperation,
    EventInput,
    IdempotencyInput,
    Principal,
    TenantScope,
    application_mutation_lock_statement,
    event_sequence_statement,
    operation_claim_statement,
    outbox_claim_statement,
    tenant_application_statement,
)
from lae_store.security import ensure_persistable_payload  # noqa: E402
from lae_store.state import (  # noqa: E402
    OperationStatus,
    cancellation_result,
    require_transition,
)


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


@dataclass(frozen=True)
class FakeLeaseState:
    status: OperationStatus = OperationStatus.QUEUED
    owner: str | None = None
    expires_at: datetime | None = None
    attempt: int = 1
    cancel_requested: bool = False

    def claim(self, worker: str, now: datetime) -> "FakeLeaseState":
        if self.status is OperationStatus.QUEUED:
            require_transition(self.status, OperationStatus.RUNNING)
            return replace(
                self,
                status=OperationStatus.RUNNING,
                owner=worker,
                expires_at=now + timedelta(seconds=30),
            )
        if (
            self.status is OperationStatus.RUNNING
            and self.expires_at is not None
            and self.expires_at < now
        ):
            return replace(
                self,
                owner=worker,
                expires_at=now + timedelta(seconds=30),
                attempt=self.attempt + 1,
            )
        raise RuntimeError("not claimable")

    def request_cancel(self) -> "FakeLeaseState":
        target, notify = cancellation_result(self.status)
        if notify:
            return replace(self, cancel_requested=True)
        return replace(self, status=target)

    def complete(self, desired: OperationStatus) -> "FakeLeaseState":
        effective = OperationStatus.CANCELED if self.cancel_requested else desired
        require_transition(self.status, effective)
        return replace(self, status=effective, owner=None, expires_at=None)


class OperationStateMachineTests(unittest.TestCase):
    def test_only_declared_transitions_are_allowed(self) -> None:
        require_transition(OperationStatus.QUEUED, OperationStatus.RUNNING)
        require_transition(OperationStatus.RUNNING, OperationStatus.SUCCEEDED)
        with self.assertRaises(InvalidOperationTransition):
            require_transition(OperationStatus.SUCCEEDED, OperationStatus.RUNNING)
        with self.assertRaises(InvalidOperationTransition):
            require_transition(OperationStatus.QUEUED, OperationStatus.SUCCEEDED)

    def test_queued_cancel_is_terminal_and_running_cancel_is_cooperative(self) -> None:
        self.assertEqual(
            cancellation_result(OperationStatus.QUEUED),
            (OperationStatus.CANCELED, False),
        )
        self.assertEqual(
            cancellation_result(OperationStatus.RUNNING),
            (OperationStatus.RUNNING, True),
        )

    def test_fake_reclaims_expired_lease_and_cancel_wins_late_success(self) -> None:
        now = datetime(2026, 7, 11, tzinfo=timezone.utc)
        running = FakeLeaseState().claim("worker-a", now)
        with self.assertRaises(RuntimeError):
            running.claim("worker-b", now + timedelta(seconds=10))
        reclaimed = running.claim("worker-b", now + timedelta(seconds=31))
        self.assertEqual(reclaimed.owner, "worker-b")
        self.assertEqual(reclaimed.attempt, 2)
        canceled = reclaimed.request_cancel().complete(OperationStatus.SUCCEEDED)
        self.assertEqual(canceled.status, OperationStatus.CANCELED)
        self.assertIsNone(canceled.owner)


class PostgreSQLQueueStatementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = TenantScope(new_id("ten"))

    def test_operation_claim_is_skip_locked_and_reclaims_expired_running_work(
        self,
    ) -> None:
        sql = _sql(operation_claim_statement(["source.analyze", "deployment.create"]))
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("operations.status = 'queued'", sql)
        self.assertIn("operations.status = 'running'", sql)
        self.assertIn("operations.lease_expires_at < now()", sql)
        self.assertIn("ORDER BY operations.created_at, operations.id", sql)

    def test_outbox_claim_is_skip_locked_and_reclaims_publisher_crash(self) -> None:
        sql = _sql(outbox_claim_statement())
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("outbox_events.status = 'pending'", sql)
        self.assertIn("outbox_events.status = 'publishing'", sql)
        self.assertIn("outbox_events.lease_expires_at < now()", sql)

    def test_event_sequence_is_atomic_monotonic_update_scoped_by_tenant(self) -> None:
        operation_id = new_id("op")
        sql = _sql(event_sequence_statement(self.scope, operation_id))
        self.assertIn("last_event_seq=(operations.last_event_seq + 1)", sql)
        self.assertIn("RETURNING operations.last_event_seq", sql)
        self.assertIn(f"operations.tenant_id = '{self.scope.tenant_id}'", sql)
        self.assertIn(f"operations.id = '{operation_id}'", sql)

    def test_tenant_repository_statement_cannot_omit_scope(self) -> None:
        application_id = new_id("app")
        sql = _sql(tenant_application_statement(self.scope, application_id))
        self.assertIn(f"applications.tenant_id = '{self.scope.tenant_id}'", sql)
        self.assertIn(f"applications.id = '{application_id}'", sql)
        self.assertIn("applications.deleted_at IS NULL", sql)

    def test_application_mutation_advisory_lock_is_parameterized(self) -> None:
        compiled = application_mutation_lock_statement().compile(
            dialect=postgresql.dialect()
        )
        sql = str(compiled)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("hashtextextended", sql)
        self.assertIn("application_lock_key", compiled.params)
        self.assertNotIn("tenant@example.com", sql)


class OperationInputSecurityTests(unittest.TestCase):
    def test_application_mutation_contract_is_closed(self) -> None:
        scope = TenantScope(new_id("ten"))
        principal = Principal("user", new_id("usr"))
        valid = CreateOperation(
            scope=scope,
            principal=principal,
            kind="deployment.create",
            target_type="application",
            target_id=new_id("app"),
            idempotency=IdempotencyInput(
                key="deploy-01",
                method="POST",
                route_template="/v1/applications/{applicationId}/deployments",
                request_hash=b"h" * 32,
            ),
        )
        self.assertEqual(valid.kind, "deployment.create")
        with self.assertRaises(ValueError):
            CreateOperation(
                scope=scope,
                principal=principal,
                kind="deployment.create",
                target_type="tenant",
                target_id=scope.tenant_id,
            )

    def test_secret_like_event_payloads_and_messages_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "secret-bearing"):
            EventInput(
                type="operation.progress",
                phase="source.fetch",
                status="running",
                message="Fetching source",
                data={"password": "do-not-store"},
            )
        with self.assertRaisesRegex(ValueError, "credential-like"):
            ensure_persistable_payload(
                {"log": "authorization: Bearer abcdefghijklmnopqrstuvwxyz"}
            )
        with self.assertRaisesRegex(ValueError, "secret-bearing"):
            ensure_persistable_payload({"accessToken": "not-allowed"})
        # Identifiers and public lookup metadata are allowed, never values.
        ensure_persistable_payload(
            {"credentialLeaseId": new_id("lease"), "deployTokenPrefix": "ABC123"}
        )


if __name__ == "__main__":
    unittest.main()
