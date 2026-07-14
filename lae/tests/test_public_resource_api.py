from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "apps/api/src",
    "packages/python/lae-core/src",
    "packages/python/lae-store/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_api.app import CSRF_COOKIE, SESSION_COOKIE, create_app  # noqa: E402
from lae_store import (  # noqa: E402
    PublicAnalysisRecord,
    PublicOperationEventPage,
    PublicOperationEventRecord,
    PublicOperationListRecord,
    PublicOperationPage,
    PublicOperationRecord,
    ResourceNotFound,
    TenantScope,
    UpdateCheckResult,
    new_id,
    require_opaque_id,
    required_scope_for_operation,
)
from lae_store.auth import (  # noqa: E402
    AuthRejected,
    DeployTokenPrincipal,
    SessionPrincipal,
)


class ResourceAuthService:
    def __init__(self) -> None:
        self.session_token = "lae_ss_v1_" + "S" * 43
        self.csrf_token = "lae_cs_" + "C" * 43
        self.deploy_token = "lae_dt_0123456789_" + "D" * 43
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.deploy_tenant_id = self.tenant_id
        self.session_id = new_id("ses")
        self.deploy_token_id = new_id("dtk")
        self.deploy_scopes = frozenset(
            {"analyses:write", "deployments:write", "apps:write"}
        )

    async def authenticate(self, token: str) -> SessionPrincipal:
        if token != self.session_token:
            raise AuthRejected("invalid")
        return SessionPrincipal(
            session_id=self.session_id,
            user_id=self.user_id,
            email="person@example.test",
            tenant_id=self.tenant_id,
            entitlement_code="lite",
            key_version=1,
            csrf_digest=b"c" * 32,
        )

    async def authenticate_deploy_token(
        self, token: str, *, request_ip: str | None
    ) -> DeployTokenPrincipal:
        del request_ip
        if token != self.deploy_token:
            raise AuthRejected("invalid")
        return DeployTokenPrincipal(
            token_id=self.deploy_token_id,
            token_prefix="0123456789",
            user_id=self.user_id,
            email="person@example.test",
            tenant_id=self.deploy_tenant_id,
            entitlement_code="lite",
            member_role="owner",
            scopes=self.deploy_scopes,
        )

    def csrf_valid(
        self,
        _principal: SessionPrincipal,
        *,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> bool:
        return csrf_cookie == self.csrf_token and csrf_header == self.csrf_token


class RecordingPublicResourceStore:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.application_id = new_id("app")
        self.analysis_id = new_id("ana")
        self.source_operation_id = new_id("op")
        self.deployment_operation_id = new_id("op")
        self.application_operation_id = new_id("op")
        self.update_operation_id = new_id("op")
        self.analysis = PublicAnalysisRecord(
            id=self.analysis_id,
            operation_id=self.source_operation_id,
            status="deployable",
            source_tree_digest="sha256:" + "a" * 64,
            source_snapshot_digest="sha256:" + "b" * 64,
            deployment_plan_digest="sha256:" + "c" * 64,
            build_plan_digest="sha256:" + "d" * 64,
            evidence_digest="sha256:" + "e" * 64,
            plan_stored=True,
        )
        self.operations = {
            self.source_operation_id: PublicOperationRecord(
                id=self.source_operation_id,
                kind="source.analyze",
                status="running",
                phase="source.analyze",
                error_code=None,
                cancel_requested=False,
                last_event_seq=2,
            ),
            self.deployment_operation_id: PublicOperationRecord(
                id=self.deployment_operation_id,
                kind="deployment.create",
                status="queued",
                phase="deploy.prepare",
                error_code=None,
                cancel_requested=False,
                last_event_seq=1,
            ),
            self.application_operation_id: PublicOperationRecord(
                id=self.application_operation_id,
                kind="application.restart",
                status="queued",
                phase="application.lifecycle",
                error_code=None,
                cancel_requested=False,
                last_event_seq=1,
            ),
            self.update_operation_id: PublicOperationRecord(
                id=self.update_operation_id,
                kind="application.check-update",
                status="succeeded",
                phase="source.analyze",
                error_code=None,
                cancel_requested=False,
                last_event_seq=3,
                update_check=UpdateCheckResult(
                    baseline_available=True,
                    source_changed=False,
                    deployment_plan_changed=True,
                    changed=True,
                    baseline_source_tree_digest="sha256:" + "a" * 64,
                    baseline_deployment_plan_digest="sha256:" + "b" * 64,
                    candidate_source_tree_digest="sha256:" + "a" * 64,
                    candidate_deployment_plan_digest="sha256:" + "c" * 64,
                ),
            ),
        }
        self.operation_applications = {
            operation_id: self.application_id for operation_id in self.operations
        }
        now = datetime(2026, 7, 11, tzinfo=timezone.utc)
        self.operation_created_at = {
            operation_id: now for operation_id in self.operations
        }
        self.events = {
            self.source_operation_id: [
                PublicOperationEventRecord(
                    event_id=new_id("evt"),
                    operation_id=self.source_operation_id,
                    cursor=1,
                    type="builder.analyze.progress",
                    phase="source.analyze",
                    status="running",
                    level="info",
                    data={
                        "replayed": True,
                        "credentialLeaseId": "lease_must-not-escape",
                        "lumaTaskId": "internal-task",
                        "imageRef": "registry.internal/image@sha256:" + "f" * 64,
                        "stdout": "raw-output",
                    },
                    created_at=now,
                ),
                PublicOperationEventRecord(
                    event_id=new_id("evt"),
                    operation_id=self.source_operation_id,
                    cursor=2,
                    type="untrusted.internal.stdout",
                    phase="internal.registry.push",
                    status="running",
                    level="warning",
                    data={"url": "https://internal.example.test/signed?token=secret"},
                    created_at=now,
                ),
            ]
        }
        self.cancel_events = 0

    def _owned(self, scope: TenantScope, resource_id: str, *, prefix: str) -> None:
        require_opaque_id(resource_id, prefix=prefix)
        if scope.tenant_id != self.tenant_id:
            raise ResourceNotFound("not found")

    async def get_analysis(
        self, scope: TenantScope, analysis_id: str
    ) -> PublicAnalysisRecord:
        self._owned(scope, analysis_id, prefix="ana")
        if analysis_id != self.analysis_id:
            raise ResourceNotFound("not found")
        return self.analysis

    async def get_operation(
        self, scope: TenantScope, operation_id: str
    ) -> PublicOperationRecord:
        self._owned(scope, operation_id, prefix="op")
        operation = self.operations.get(operation_id)
        if operation is None:
            raise ResourceNotFound("not found")
        return operation

    async def list_operations(
        self,
        scope: TenantScope,
        *,
        allowed_scopes: frozenset[str],
        application_id: str | None,
        kind: str | None,
        before: str | None,
        limit: int,
    ) -> PublicOperationPage:
        if application_id is not None:
            require_opaque_id(application_id, prefix="app")
        if before is not None:
            require_opaque_id(before, prefix="op")
        if not 1 <= limit <= 100:
            raise ValueError("invalid limit")
        if scope.tenant_id != self.tenant_id:
            return PublicOperationPage((), False)
        visible = []
        for operation in self.operations.values():
            try:
                required_scope = required_scope_for_operation(operation.kind)
            except ResourceNotFound:
                continue
            if required_scope not in allowed_scopes:
                continue
            if kind is not None and operation.kind != kind:
                continue
            operation_application_id = self.operation_applications.get(operation.id)
            if (
                application_id is not None
                and operation_application_id != application_id
            ):
                continue
            if before is not None and operation.id >= before:
                continue
            visible.append(operation)
        visible.sort(key=lambda operation: operation.id, reverse=True)
        has_more = len(visible) > limit
        records = tuple(
            PublicOperationListRecord(
                operation=operation,
                application_id=self.operation_applications.get(operation.id),
                created_at=self.operation_created_at[operation.id],
                started_at=None,
                finished_at=None,
            )
            for operation in visible[:limit]
        )
        return PublicOperationPage(records, has_more)

    async def list_operation_events(
        self,
        scope: TenantScope,
        operation_id: str,
        *,
        after: int,
        limit: int,
    ) -> PublicOperationEventPage:
        operation = await self.get_operation(scope, operation_id)
        if not 0 <= after <= (1 << 63) - 1 or not 1 <= limit <= 500:
            raise ValueError("invalid cursor")
        retained = tuple(
            event
            for event in self.events.get(operation_id, [])
            if event.cursor > after
        )[:limit]
        cursor = retained[-1].cursor if retained else after
        return PublicOperationEventPage(
            operation=operation,
            events=retained,
            cursor=cursor,
            has_more=cursor < operation.last_event_seq,
        )

    async def request_cancel(
        self, scope: TenantScope, operation_id: str
    ) -> PublicOperationRecord:
        operation = await self.get_operation(scope, operation_id)
        if operation.status == "queued":
            self.cancel_events += 1
            operation = replace(
                operation,
                status="canceled",
                cancel_requested=True,
                last_event_seq=operation.last_event_seq + 1,
            )
        elif operation.status == "running" and not operation.cancel_requested:
            self.cancel_events += 1
            operation = replace(
                operation,
                cancel_requested=True,
                last_event_seq=operation.last_event_seq + 1,
            )
        self.operations[operation_id] = operation
        return operation


class PublicResourceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = ResourceAuthService()
        self.store = RecordingPublicResourceStore(self.auth.tenant_id)
        self.client_context = TestClient(
            create_app(self.auth, None, self.store),
            base_url="https://lae.example.test",
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    @property
    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.deploy_token}"}

    def test_analysis_read_returns_only_public_status_digests_and_links(self) -> None:
        response = self.client.get(
            f"/v1/analyses/{self.store.analysis_id}", headers=self.bearer
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()),
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
        self.assertTrue(response.json()["planStored"])
        serialized = response.text.lower()
        for forbidden in (
            "artifact",
            "repository",
            "credential",
            "lease",
            "luma",
            "image",
            "snapshotid",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")

    def test_operation_kind_uses_minimum_scope_and_unknown_kind_fails_closed(
        self,
    ) -> None:
        cases = (
            (self.store.source_operation_id, "analyses:write"),
            (self.store.deployment_operation_id, "deployments:write"),
            (self.store.application_operation_id, "apps:write"),
            (self.store.update_operation_id, "apps:write"),
        )
        for operation_id, scope in cases:
            with self.subTest(scope=scope):
                self.auth.deploy_scopes = frozenset({scope})
                allowed = self.client.get(
                    f"/v1/operations/{operation_id}", headers=self.bearer
                )
                self.assertEqual(allowed.status_code, 200, allowed.text)
                self.auth.deploy_scopes = frozenset({"apps:read"})
                denied = self.client.get(
                    f"/v1/operations/{operation_id}", headers=self.bearer
                )
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(denied.json()["error"]["code"], "LAE_FORBIDDEN")

        unknown_id = new_id("op")
        self.store.operations[unknown_id] = PublicOperationRecord(
            id=unknown_id,
            kind="internal.reconcile",
            status="running",
            phase=None,
            error_code=None,
            cancel_requested=False,
            last_event_seq=0,
        )
        self.auth.deploy_scopes = frozenset(
            {"analyses:write", "deployments:write", "apps:write"}
        )
        hidden = self.client.get(f"/v1/operations/{unknown_id}", headers=self.bearer)
        self.assertEqual(hidden.status_code, 404)

    def test_operation_history_is_scope_filtered_and_cursor_resumable(self) -> None:
        extra_id = new_id("op")
        self.store.operations[extra_id] = PublicOperationRecord(
            id=extra_id,
            kind="deployment.create",
            status="running",
            phase="deploy.apply",
            error_code=None,
            cancel_requested=False,
            last_event_seq=4,
        )
        self.store.operation_applications[extra_id] = self.store.application_id
        self.store.operation_created_at[extra_id] = datetime(
            2026, 7, 11, 0, 1, tzinfo=timezone.utc
        )
        self.auth.deploy_scopes = frozenset({"deployments:write"})
        path = "/v1/operations"
        first = self.client.get(
            path,
            headers=self.bearer,
            params={"applicationId": self.store.application_id, "limit": 1},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["hasMore"])
        self.assertIsNotNone(first.json()["nextCursor"])
        first_item = first.json()["operations"][0]
        self.assertEqual(first_item["kind"], "deployment.create")
        self.assertEqual(first_item["applicationId"], self.store.application_id)
        self.assertIn("createdAt", first_item)
        self.assertEqual(
            first_item["links"]["events"],
            f"/v1/operations/{first_item['id']}/events",
        )

        second = self.client.get(
            path,
            headers=self.bearer,
            params={
                "applicationId": self.store.application_id,
                "limit": 1,
                "before": first.json()["nextCursor"],
            },
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(second.json()["hasMore"])
        self.assertIsNone(second.json()["nextCursor"])
        ids = {first_item["id"], second.json()["operations"][0]["id"]}
        self.assertEqual(
            ids,
            {self.store.deployment_operation_id, extra_id},
        )
        serialized = first.text.lower() + second.text.lower()
        for forbidden in ("credential", "luma", "image", "stdout", "repository"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(first.headers["cache-control"], "no-store, max-age=0")

        denied_kind = self.client.get(
            path,
            headers=self.bearer,
            params={"kind": "application.restart"},
        )
        self.assertEqual(denied_kind.status_code, 403)
        invalid_kind = self.client.get(
            path,
            headers=self.bearer,
            params={"kind": "internal.reconcile"},
        )
        self.assertEqual(invalid_kind.status_code, 400)

    def test_operation_history_hides_foreign_tenant_and_requires_a_history_scope(
        self,
    ) -> None:
        self.auth.deploy_scopes = frozenset({"deployments:write"})
        self.auth.deploy_tenant_id = new_id("ten")
        foreign = self.client.get("/v1/operations", headers=self.bearer)
        self.assertEqual(foreign.status_code, 200, foreign.text)
        self.assertEqual(foreign.json()["operations"], [])

        self.auth.deploy_tenant_id = self.auth.tenant_id
        self.auth.deploy_scopes = frozenset({"apps:read"})
        denied = self.client.get("/v1/operations", headers=self.bearer)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "LAE_FORBIDDEN")

    def test_update_check_operation_exposes_only_closed_comparison(self) -> None:
        self.auth.deploy_scopes = frozenset({"apps:write"})
        response = self.client.get(
            f"/v1/operations/{self.store.update_operation_id}",
            headers=self.bearer,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            body["updateCheck"],
            self.store.operations[
                self.store.update_operation_id
            ].update_check.to_body(),
        )
        self.assertEqual(
            set(body["updateCheck"]),
            {
                "baselineAvailable",
                "sourceChanged",
                "deploymentPlanChanged",
                "changed",
                "changes",
                "candidateAnalysis",
                "digests",
            },
        )
        serialized = response.text.lower()
        for forbidden in (
            "repository",
            "credential",
            "secret",
            "topology",
            "node",
            "luma",
            "artifact",
            "sourcerevisionid",
        ):
            self.assertNotIn(forbidden, serialized)
        self.auth.deploy_scopes = frozenset({"analyses:write"})
        ordinary = self.client.get(
            f"/v1/operations/{self.store.source_operation_id}",
            headers={"Authorization": f"Bearer {self.auth.deploy_token}"},
        )
        self.assertNotIn("updateCheck", ordinary.json())

    def test_events_replay_monotonic_cursor_and_redact_internal_payloads(self) -> None:
        first = self.client.get(
            f"/v1/operations/{self.store.source_operation_id}/events",
            headers=self.bearer,
            params={"after": 0, "limit": 1},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["cursor"], 1)
        self.assertTrue(first.json()["hasMore"])
        self.assertFalse(first.json()["terminal"])
        self.assertEqual(first.json()["events"][0]["data"], {"replayed": True})

        second = self.client.get(
            f"/v1/operations/{self.store.source_operation_id}/events",
            headers=self.bearer,
            params={"after": first.json()["cursor"], "limit": 10},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["cursor"], 2)
        event = second.json()["events"][0]
        self.assertEqual(event["type"], "operation.progress")
        self.assertEqual(event["message"], "Operation progress updated")
        self.assertIsNone(event["phase"])
        self.assertEqual(event["data"], {})
        serialized = first.text.lower() + second.text.lower()
        for forbidden in (
            "lease_must-not-escape",
            "internal-task",
            "registry.internal",
            "raw-output",
            "signed?token",
            "untrusted.internal.stdout",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_cancel_is_state_idempotent_bearer_needs_no_csrf_cookie_needs_csrf(
        self,
    ) -> None:
        operation_id = self.store.deployment_operation_id
        first = self.client.post(
            f"/v1/operations/{operation_id}/cancel", headers=self.bearer
        )
        second = self.client.post(
            f"/v1/operations/{operation_id}/cancel", headers=self.bearer
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["status"], "canceled")
        self.assertEqual(self.store.cancel_events, 1)
        self.assertEqual(first.headers["idempotency-policy"], "state-transition")

        session_operation = self.store.application_operation_id
        self.client.cookies.set(SESSION_COOKIE, self.auth.session_token)
        self.client.cookies.set(CSRF_COOKIE, self.auth.csrf_token)
        without_csrf = self.client.post(
            f"/v1/operations/{session_operation}/cancel"
        )
        self.assertEqual(without_csrf.status_code, 403)
        self.assertEqual(without_csrf.json()["error"]["code"], "LAE_CSRF_FAILED")
        with_csrf = self.client.post(
            f"/v1/operations/{session_operation}/cancel",
            headers={"X-CSRF-Token": self.auth.csrf_token},
        )
        self.assertEqual(with_csrf.status_code, 202)

    def test_cross_tenant_nonexistent_malformed_and_confused_deputy_are_hidden(
        self,
    ) -> None:
        missing = self.client.get(
            f"/v1/operations/{new_id('op')}", headers=self.bearer
        )
        malformed = self.client.get("/v1/operations/not-an-id", headers=self.bearer)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(malformed.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "LAE_NOT_FOUND")
        self.assertEqual(malformed.json()["error"]["code"], "LAE_NOT_FOUND")

        self.auth.deploy_tenant_id = new_id("ten")
        cross_tenant = self.client.get(
            f"/v1/operations/{self.store.source_operation_id}", headers=self.bearer
        )
        self.assertEqual(cross_tenant.status_code, 404)
        self.assertEqual(cross_tenant.json()["error"]["code"], "LAE_NOT_FOUND")

        self.client.cookies.set(SESSION_COOKIE, self.auth.session_token)
        self.client.cookies.set(CSRF_COOKIE, self.auth.csrf_token)
        confused = self.client.post(
            f"/v1/operations/{self.store.application_operation_id}/cancel",
            headers={
                **self.bearer,
                "X-CSRF-Token": self.auth.csrf_token,
            },
        )
        self.assertEqual(confused.status_code, 401)
        self.assertEqual(confused.json()["error"]["code"], "LAE_UNAUTHENTICATED")

    def test_invalid_event_cursor_and_limit_use_stable_public_error(self) -> None:
        for params in (
            {"after": -1},
            {"after": 1 << 63},
            {"limit": 0},
            {"limit": 501},
        ):
            with self.subTest(params=params):
                response = self.client.get(
                    f"/v1/operations/{self.store.source_operation_id}/events",
                    headers=self.bearer,
                    params=params,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()["error"]["code"], "LAE_INVALID_ARGUMENT"
                )


class PublicResourceContractTests(unittest.TestCase):
    def test_kind_scope_mapping_is_explicit(self) -> None:
        self.assertEqual(required_scope_for_operation("source.analyze"), "analyses:write")
        self.assertEqual(
            required_scope_for_operation("deployment.create"), "deployments:write"
        )
        self.assertEqual(
            required_scope_for_operation("application.restart"), "apps:write"
        )
        with self.assertRaises(ResourceNotFound):
            required_scope_for_operation("source.fetch-internal")

    def test_terminal_page_is_true_only_after_all_events_are_replayed(self) -> None:
        operation = PublicOperationRecord(
            id=new_id("op"),
            kind="source.analyze",
            status="succeeded",
            phase="source.analyze",
            error_code=None,
            cancel_requested=False,
            last_event_seq=2,
        )
        page = PublicOperationEventPage(operation, (), 1, True)
        self.assertFalse(page.public_body()["terminal"])
        self.assertEqual(page.public_body()["status"], "succeeded")
        drained = PublicOperationEventPage(operation, (), 2, False)
        self.assertTrue(drained.public_body()["terminal"])

    def test_operation_error_never_copies_free_form_or_invalid_internal_code(self) -> None:
        operation = PublicOperationRecord(
            id=new_id("op"),
            kind="source.analyze",
            status="failed",
            phase="source.analyze",
            error_code="internal-token-like-code-must-not-escape",
            cancel_requested=False,
            last_event_seq=1,
        )
        self.assertNotIn("error", operation.public_body())
        stable = replace(operation, error_code="LAE_ANALYSIS_FAILED")
        self.assertEqual(
            stable.public_body()["error"],
            {"code": "LAE_ANALYSIS_FAILED", "message": "Operation failed"},
        )


if __name__ == "__main__":
    unittest.main()
