from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "apps/api/src",
    "packages/python/lae-store/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_api.deployment_api import register_deployment_routes  # noqa: E402
from lae_store import (  # noqa: E402
    DeploymentChangeConfirmationRequired,
    DeploymentEnvironmentScopeInvalid,
    IdempotentCatalogResult,
    ResourceNotFound,
    TenantScope,
    new_id,
)
from lae_store.deployment_admission import (  # noqa: E402
    DeploymentAdmissionResult,
    PublicDeploymentRecord,
)


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


class FakeDeploymentService:
    def __init__(self) -> None:
        self.application_id = new_id("app")
        self.deployment_id = new_id("dep")
        self.operation_id = new_id("op")
        self.revision_id = new_id("rev")
        self.calls: list[tuple[str, object]] = []
        self.not_found = False
        self.create_error: Exception | None = None

    def record(self) -> PublicDeploymentRecord:
        return PublicDeploymentRecord(
            id=self.deployment_id,
            application_id=self.application_id,
            revision_id=self.revision_id,
            operation_id=self.operation_id,
            status="queued",
            previous_deployment_id=None,
            started_at=None,
            finished_at=None,
            error_code=None,
            created_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        )

    async def create(
        self,
        scope: TenantScope,
        principal: Principal,
        application_id: str,
        payload: object,
        idempotency_key: str,
    ) -> DeploymentAdmissionResult:
        self.calls.append(
            ("create", (scope, principal, application_id, payload, idempotency_key))
        )
        if self.create_error is not None:
            raise self.create_error
        if self.not_found:
            raise ResourceNotFound("foreign analysis secret detail")
        record = self.record()
        return DeploymentAdmissionResult(
            deployment=record,
            operation_id=record.operation_id,
            operation_status="queued",
            operation_phase="deploy.prepare",
            operation_cursor=1,
            replayed=False,
        )

    async def list(
        self, scope: TenantScope, application_id: str, *, limit: int
    ) -> dict[str, object]:
        self.calls.append(("list", (scope, application_id, limit)))
        if self.not_found:
            raise ResourceNotFound("foreign application secret detail")
        return {"deployments": [self.record().public_body()]}

    async def get(
        self, scope: TenantScope, application_id: str, deployment_id: str
    ) -> dict[str, object]:
        self.calls.append(("get", (scope, application_id, deployment_id)))
        if self.not_found:
            raise ResourceNotFound("foreign deployment secret detail")
        return {"deployment": self.record().public_body()}

    async def configuration(
        self, scope: TenantScope, application_id: str, analysis_id: str
    ) -> dict[str, object]:
        self.calls.append(("configuration", (scope, application_id, analysis_id)))
        if self.not_found:
            raise ResourceNotFound("foreign analysis secret detail")
        return {
            "configuration": {
                "sourceRevisionId": new_id("src"),
                "kind": "compose",
                "serviceKeys": ["web", "worker"],
                "environmentSchemaDigest": "sha256:" + "a" * 64,
                "environmentScopeMode": "service",
                "environment": [
                    {
                        "name": "DATABASE_URL",
                        "serviceKeys": ["web", "worker"],
                        "references": [
                            "web:DATABASE_URL",
                            "worker:DATABASE_URL",
                        ],
                        "required": True,
                        "sensitive": True,
                    }
                ],
            }
        }

    async def patch_environment(
        self,
        scope: TenantScope,
        principal: Principal,
        application_id: str,
        analysis_id: str,
        payload: object,
        idempotency_key: str,
    ) -> IdempotentCatalogResult:
        self.calls.append(
            (
                "patch_environment",
                (
                    scope,
                    principal,
                    application_id,
                    analysis_id,
                    payload,
                    idempotency_key,
                ),
            )
        )
        if any(reference.startswith("*:") for reference in payload.set):
            raise DeploymentEnvironmentScopeInvalid("wildcard is unsafe")
        return IdempotentCatalogResult(
            {
                "environment": {
                    "version": payload.expectedVersion + 1,
                    "variables": [
                        {
                            "serviceScope": reference.split(":", 1)[0],
                            "name": reference.split(":", 1)[1],
                            "configured": True,
                            "sensitive": True,
                            "required": True,
                            "source": "user",
                            "updatedAt": "2026-07-11T00:00:00Z",
                        }
                        for reference in payload.set
                    ],
                }
            },
            replayed=False,
        )


class DeploymentRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tenant_id = new_id("ten")
        self.principal = Principal(
            tenant_id=self.tenant_id,
            credential_id=new_id("dtk"),
        )
        self.auth_calls: list[dict[str, object]] = []
        self.service = FakeDeploymentService()
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
                {
                    "scope": scope,
                    "csrf": csrf_header,
                    "mutation": mutation,
                }
            )
            return self.principal

        app.state.require_scoped_principal = require_scoped_principal
        register_deployment_routes(app, lambda: self.service, ApiError)
        self.context = TestClient(app, base_url="https://lae.example.test")
        self.client = self.context.__enter__()

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    def payload(self) -> dict[str, object]:
        return {"analysisId": new_id("ana"), "environmentVersion": 0}

    def test_create_contract_is_minimal_scoped_csrf_aware_and_secret_free(self) -> None:
        response = self.client.post(
            f"/v1/applications/{self.service.application_id}/deployments",
            headers={
                "Idempotency-Key": "deploy-api-1",
                "X-CSRF-Token": "csrf-value",
            },
            json=self.payload(),
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.headers["idempotency-replayed"], "false")
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(
            self.auth_calls[-1],
            {
                "scope": "deployments:write",
                "csrf": "csrf-value",
                "mutation": True,
            },
        )
        body = response.json()
        self.assertEqual(body["deployment"]["id"], self.service.deployment_id)
        self.assertEqual(body["operation"]["cursor"], 1)
        serialized = response.text.lower()
        for forbidden in (
            "storage_key",
            "storagekey",
            "manifest",
            "image",
            "environmentvalue",
            "luma",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_create_accepts_only_sorted_unique_update_confirmations(self) -> None:
        path = f"/v1/applications/{self.service.application_id}/deployments"
        response = self.client.post(
            path,
            headers={"Idempotency-Key": "deploy-update-api-1"},
            json={
                **self.payload(),
                "confirmedChanges": [
                    "PUBLIC_ROUTE_CHANGE",
                    "SERVICE_REMOVAL",
                ],
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        payload = self.service.calls[-1][1][3]
        self.assertEqual(
            payload.confirmedChanges,
            ["PUBLIC_ROUTE_CHANGE", "SERVICE_REMOVAL"],
        )

        for confirmed in (
            ["SERVICE_REMOVAL", "PUBLIC_ROUTE_CHANGE"],
            ["SERVICE_REMOVAL", "SERVICE_REMOVAL"],
            ["UNKNOWN_CHANGE"],
        ):
            rejected = self.client.post(
                path,
                headers={"Idempotency-Key": f"reject-{len(self.service.calls)}"},
                json={**self.payload(), "confirmedChanges": confirmed},
            )
            self.assertEqual(rejected.status_code, 422, rejected.text)

    def test_create_projects_required_update_confirmation_without_plan_details(self) -> None:
        path = f"/v1/applications/{self.service.application_id}/deployments"
        self.service.create_error = DeploymentChangeConfirmationRequired(
            ("PUBLIC_ROUTE_CHANGE", "SERVICE_REMOVAL")
        )
        required = self.client.post(
            path,
            headers={"Idempotency-Key": "deploy-confirmation-required"},
            json=self.payload(),
        )
        self.assertEqual(required.status_code, 409, required.text)
        self.assertEqual(
            required.json()["error"],
            {
                "code": "LAE_DEPLOYMENT_CONFIRMATION_REQUIRED",
                "message": (
                    "Destructive deployment changes require explicit confirmation"
                ),
                "retryable": False,
                "details": {
                    "requiredConfirmations": [
                        "PUBLIC_ROUTE_CHANGE",
                        "SERVICE_REMOVAL",
                    ]
                },
            },
        )

        self.service.create_error = DeploymentChangeConfirmationRequired(())
        legacy = self.client.post(
            path,
            headers={"Idempotency-Key": "deploy-update-details-required"},
            json=self.payload(),
        )
        self.assertEqual(legacy.status_code, 409, legacy.text)
        self.assertEqual(
            legacy.json()["error"]["code"],
            "LAE_UPDATE_CHECK_DETAILS_REQUIRED",
        )

    def test_request_rejects_missing_idempotency_and_all_user_plan_fields(self) -> None:
        path = f"/v1/applications/{self.service.application_id}/deployments"
        missing = self.client.post(path, json=self.payload())
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["error"]["code"], "LAE_IDEMPOTENCY_REQUIRED")

        for field, value in (
            ("services", [{"key": "web"}]),
            ("routes", [{"protocol": "tcp"}]),
            ("image", "registry/private:latest"),
            ("manifest", "secret manifest"),
            ("volumeConfirmations", ["database"]),
            ("primaryService", "web"),
        ):
            response = self.client.post(
                path,
                headers={"Idempotency-Key": f"reject-{field}"},
                json={**self.payload(), field: value},
            )
            self.assertEqual(response.status_code, 422, (field, response.text))
        self.assertEqual(len(self.service.calls), 0)

    def test_list_show_are_tenant_scoped_and_foreign_ids_are_not_disclosed(
        self,
    ) -> None:
        listed = self.client.get(
            f"/v1/applications/{self.service.application_id}/deployments"
        )
        shown = self.client.get(
            f"/v1/applications/{self.service.application_id}/deployments/"
            f"{self.service.deployment_id}"
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(shown.status_code, 200)
        self.assertTrue(
            all(call["scope"] == "deployments:write" for call in self.auth_calls)
        )
        self.assertTrue(all(call["mutation"] is False for call in self.auth_calls[-2:]))

        self.service.not_found = True
        foreign_id = new_id("dep")
        hidden = self.client.get(
            f"/v1/applications/{new_id('app')}/deployments/{foreign_id}"
        )
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.json()["error"]["code"], "LAE_NOT_FOUND")
        self.assertNotIn(foreign_id, hidden.text)
        self.assertNotIn("secret detail", hidden.text)

    def test_configuration_is_secret_free_tenant_scoped_and_no_store(self) -> None:
        analysis_id = new_id("ana")
        path = (
            f"/v1/applications/{self.service.application_id}/analyses/"
            f"{analysis_id}/configuration"
        )
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(
            self.auth_calls[-1],
            {"scope": "deployments:write", "csrf": None, "mutation": False},
        )
        body = response.json()["configuration"]
        self.assertEqual(body["serviceKeys"], ["web", "worker"])
        self.assertEqual(body["environment"][0]["name"], "DATABASE_URL")
        serialized = response.text.lower()
        for forbidden in ("storagekey", "artifact", "value", "token", "image"):
            self.assertNotIn(forbidden, serialized)

        self.service.not_found = True
        hidden = self.client.get(path)
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.json()["error"]["code"], "LAE_NOT_FOUND")
        self.assertNotIn("secret detail", hidden.text)

    def test_plan_environment_patch_requires_explicit_service_scope(self) -> None:
        analysis_id = new_id("ana")
        path = (
            f"/v1/applications/{self.service.application_id}/analyses/"
            f"{analysis_id}/environment"
        )
        secret = "never-return-this-value"
        payload = {
            "expectedVersion": 0,
            "environmentSchemaDigest": "sha256:" + "a" * 64,
            "set": {
                "web:DATABASE_URL": {"value": secret},
                "worker:DATABASE_URL": {"value": secret},
            },
            "unset": [],
        }
        response = self.client.patch(
            path,
            headers={
                "Idempotency-Key": "plan-environment-1",
                "X-CSRF-Token": "csrf-value",
            },
            json=payload,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["idempotency-replayed"], "false")
        self.assertEqual(
            self.auth_calls[-1],
            {"scope": "apps:write", "csrf": "csrf-value", "mutation": True},
        )
        self.assertNotIn(secret, response.text)
        call = self.service.calls[-1][1]
        self.assertEqual(set(call[4].set), {"web:DATABASE_URL", "worker:DATABASE_URL"})

        wildcard = self.client.patch(
            path,
            headers={"Idempotency-Key": "plan-environment-wildcard"},
            json={
                **payload,
                "set": {"*:DATABASE_URL": {"value": secret}},
            },
        )
        self.assertEqual(wildcard.status_code, 409)
        self.assertEqual(
            wildcard.json()["error"]["code"], "LAE_ENVIRONMENT_SCOPE_INVALID"
        )
        self.assertNotIn(secret, wildcard.text)

        caller_flags = self.client.patch(
            path,
            headers={"Idempotency-Key": "plan-environment-flags"},
            json={
                **payload,
                "set": {
                    "web:DATABASE_URL": {
                        "value": secret,
                        "sensitive": False,
                        "required": False,
                    }
                },
            },
        )
        self.assertEqual(caller_flags.status_code, 422)


if __name__ == "__main__":
    unittest.main()
