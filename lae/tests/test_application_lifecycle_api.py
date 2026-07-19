from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in ("apps/api/src", "packages/python/lae-store/src"):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_api.application_lifecycle_api import (  # noqa: E402
    register_application_lifecycle_routes,
)
from lae_api.app import create_app  # noqa: E402
from lae_store import (  # noqa: E402
    ApplicationActionResult,
    ApplicationLifecycleConflict,
    IdempotencyKeyReused,
    ResourceNotFound,
    TenantScope,
    new_id,
)
from lae_store.auth import DeployTokenPrincipal  # noqa: E402


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


@dataclass
class Principal:
    tenant_id: str
    credential_type: str = "deploy_token"
    credential_id: str = ""


class FakeLifecycleService:
    def __init__(self) -> None:
        self.application_id = new_id("app")
        self.operation_id = new_id("op")
        self.analysis_id = new_id("ana")
        self.calls: list[dict[str, Any]] = []
        self.failure: Exception | None = None

    async def request(
        self,
        scope: TenantScope,
        principal: Principal,
        application_id: str,
        action: str,
        *,
        rollback_deployment_id: str | None,
        idempotency_key: str,
    ) -> ApplicationActionResult:
        self.calls.append(
            {
                "scope": scope,
                "principal": principal,
                "application_id": application_id,
                "action": action,
                "rollback_deployment_id": rollback_deployment_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.failure is not None:
            raise self.failure
        body: dict[str, Any] = {
            "application": {
                "id": application_id,
                "desiredState": "suspended" if action == "suspend" else "running",
                "observedState": "running",
            },
            "operation": {
                "id": self.operation_id,
                "kind": f"application.{action}",
                "status": "queued",
                "phase": "source.analyze"
                if action == "check-update"
                else "application.lifecycle",
                "cursor": 1,
                "links": {
                    "operation": f"/v1/operations/{self.operation_id}",
                    "events": f"/v1/operations/{self.operation_id}/events",
                },
            },
        }
        if action == "check-update":
            body["analysis"] = {
                "id": self.analysis_id,
                "status": "queued",
                "links": {"analysis": f"/v1/analyses/{self.analysis_id}"},
            }
        return ApplicationActionResult(body, replayed=False)


class ApplicationLifecycleRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tenant_id = new_id("ten")
        self.principal = Principal(
            tenant_id=self.tenant_id, credential_id=new_id("dtk")
        )
        self.auth_calls: list[dict[str, Any]] = []
        self.service = FakeLifecycleService()
        app = FastAPI()

        @app.exception_handler(ApiError)
        async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
            return JSONResponse(
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "retryable": exc.retryable,
                        "details": exc.details,
                    }
                },
                status_code=exc.status,
            )

        async def require_scoped_principal(
            _request: Request,
            scope: str,
            *,
            csrf_header: str | None = None,
            mutation: bool,
        ) -> Principal:
            self.auth_calls.append(
                {"scope": scope, "csrf": csrf_header, "mutation": mutation}
            )
            return self.principal

        app.state.require_scoped_principal = require_scoped_principal
        register_application_lifecycle_routes(app, lambda: self.service, ApiError)
        self.context = TestClient(app, base_url="https://lae.example.test")
        self.client = self.context.__enter__()

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    def path(self, action: str) -> str:
        return f"/v1/applications/{self.service.application_id}/actions/{action}"

    def test_actions_require_apps_scope_csrf_and_idempotency(self) -> None:
        for action in ("suspend", "resume", "restart", "delete", "check-update"):
            response = self.client.post(
                self.path(action),
                headers={
                    "Idempotency-Key": f"action-{action}",
                    "X-CSRF-Token": "csrf-value",
                },
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.headers["idempotency-replayed"], "false")
            self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
            self.assertEqual(
                self.auth_calls[-1],
                {"scope": "apps:write", "csrf": "csrf-value", "mutation": True},
            )
        missing = self.client.post(self.path("restart"))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["error"]["code"], "LAE_IDEMPOTENCY_REQUIRED")

    def test_rollback_uses_deployment_scope_and_bounded_server_validated_target(
        self,
    ) -> None:
        deployment_id = new_id("dep")
        response = self.client.post(
            self.path("rollback"),
            headers={
                "Idempotency-Key": "rollback-1",
                "X-CSRF-Token": "csrf-value",
            },
            json={"deploymentId": deployment_id},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            self.auth_calls[-1],
            {
                "scope": "deployments:write",
                "csrf": "csrf-value",
                "mutation": True,
            },
        )
        self.assertEqual(
            self.service.calls[-1]["rollback_deployment_id"], deployment_id
        )

    def test_check_update_rejects_every_caller_supplied_source_fact(self) -> None:
        for field, value in (
            ("repository", "https://attacker.example/repo.git"),
            ("ref", "attacker-branch"),
            ("connectionId", new_id("conn")),
            ("subdirectory", "other"),
            ("source", {"repository": "https://attacker.example/x"}),
        ):
            response = self.client.post(
                self.path("check-update"),
                headers={"Idempotency-Key": f"source-injection-{field}"},
                json={field: value},
            )
            self.assertEqual(response.status_code, 400, (field, response.text))
            self.assertEqual(response.json()["error"]["code"], "LAE_INVALID_ARGUMENT")
        self.assertEqual(self.service.calls, [])

    def test_unknown_action_and_foreign_resources_do_not_disclose_details(self) -> None:
        unknown = self.client.post(
            self.path("shell"), headers={"Idempotency-Key": "unknown"}
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(self.auth_calls, [])

        self.service.failure = ResourceNotFound("foreign tenant secret detail")
        hidden = self.client.post(
            self.path("restart"), headers={"Idempotency-Key": "hidden"}
        )
        self.assertEqual(hidden.status_code, 404)
        self.assertNotIn("secret detail", hidden.text)

    def test_conflict_and_idempotency_errors_are_stable(self) -> None:
        self.service.failure = ApplicationLifecycleConflict("internal operation id")
        conflict = self.client.post(
            self.path("restart"), headers={"Idempotency-Key": "conflict"}
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.json()["error"]["code"], "LAE_APPLICATION_MUTATION_CONFLICT"
        )

        self.service.failure = IdempotencyKeyReused("different secret request")
        reused = self.client.post(
            self.path("restart"), headers={"Idempotency-Key": "reused"}
        )
        self.assertEqual(reused.status_code, 409)
        self.assertEqual(
            reused.json()["error"]["code"], "LAE_IDEMPOTENCY_KEY_REUSED"
        )
        self.assertNotIn("secret", reused.text)


class ApplicationLifecycleAppWiringTests(unittest.TestCase):
    def test_create_app_wires_lifecycle_as_independent_capability(self) -> None:
        token = "lae_dt_0123456789_" + "A" * 43
        tenant_id = new_id("ten")
        user_id = new_id("usr")

        class Auth:
            async def authenticate_deploy_token(self, value, *, request_ip):
                del request_ip
                if value != token:
                    raise AssertionError("unexpected token")
                return DeployTokenPrincipal(
                    token_id=new_id("dtk"),
                    token_prefix="0123456789",
                    user_id=user_id,
                    email="lifecycle@example.test",
                    tenant_id=tenant_id,
                    entitlement_code="lite",
                    member_role="owner",
                    scopes=frozenset({"apps:write", "deployments:write"}),
                )

        service = FakeLifecycleService()
        with TestClient(
            create_app(
                auth_service=Auth(),  # type: ignore[arg-type]
                application_lifecycle=service,
            ),
            base_url="https://lae.example.test",
        ) as client:
            response = client.post(
                f"/v1/applications/{service.application_id}/actions/restart",
                headers={
                    "Authorization": "Bearer " + token,
                    "Idempotency-Key": "wired-lifecycle-1",
                },
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(service.calls[-1]["action"], "restart")
        self.assertEqual(service.calls[-1]["scope"].tenant_id, tenant_id)

        with TestClient(
            create_app(auth_service=Auth()),  # type: ignore[arg-type]
            base_url="https://lae.example.test",
        ) as client:
            unavailable = client.post(
                f"/v1/applications/{service.application_id}/actions/restart",
                headers={
                    "Authorization": "Bearer " + token,
                    "Idempotency-Key": "wired-lifecycle-unavailable",
                },
            )
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(
            unavailable.json()["error"]["code"],
            "LAE_APPLICATION_LIFECYCLE_UNAVAILABLE",
        )


if __name__ == "__main__":
    unittest.main()
