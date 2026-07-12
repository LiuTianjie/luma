from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from lae_api.app import create_app
from lae_api.observability_api import (
    ApplicationObservabilityService,
    RuntimeReadBinding,
)
from lae_luma_adapter import RuntimeLogTail, RuntimeMetricsHistory
from lae_store import TenantScope, new_id


class _Bindings:
    def __init__(self, binding: RuntimeReadBinding) -> None:
        self.binding = binding
        self.calls = []

    async def resolve(self, scope, application_id, service_key):
        self.calls.append((scope, application_id, service_key))
        return self.binding


class _Runtime:
    def __init__(self) -> None:
        self.log_calls = []
        self.metric_calls = []

    def tail_runtime_logs(self, context, deployment_ref, service_key, *, tail):
        self.log_calls.append((context, deployment_ref, service_key, tail))
        return RuntimeLogTail(
            luma_name="lae-app-test",
            service_key=service_key,
            tail=tail,
            logs=("ready", "request complete"),
            truncated=False,
            updated_at=1720000000,
        )

    def get_runtime_metrics_history(
        self, context, deployment_ref, service_key, *, window_seconds
    ):
        self.metric_calls.append(
            (context, deployment_ref, service_key, window_seconds)
        )
        return RuntimeMetricsHistory(
            luma_name="lae-app-test",
            service_key=service_key,
            window_seconds=window_seconds,
            series={
                "cpuPercent": ((1720000000, 12.5),),
                "memoryUsageBytes": ((1720000000, 1048576),),
            },
            updated_at=1720000001,
        )


class _Auth:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    async def authenticate(self, _token):
        return None

    async def authenticate_deploy_token(self, _token, *, request_ip):
        del request_ip
        return SimpleNamespace(
            tenant_id=self.tenant_id,
            user_id=new_id("usr"),
            email="observer@example.test",
            entitlement_code="lite",
            credential_type="deploy_token",
            credential_id=new_id("dtk"),
            scopes=frozenset({"logs:read"}),
        )

class ObservabilityApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tenant_id = new_id("ten")
        self.application_id = new_id("app")
        self.operation_id = new_id("op")
        self.revision_id = new_id("rev")
        self.deployment_id = new_id("dep")
        self.runtime_deployment_id = "lae-run-test"
        self.binding = RuntimeReadBinding(
            tenant_ref=self.tenant_id,
            application_ref=self.application_id,
            operation_ref=self.operation_id,
            revision_ref=self.revision_id,
            deployment_ref=self.deployment_id,
            runtime_deployment_ref=self.runtime_deployment_id,
            service_key="web",
        )
        self.bindings = _Bindings(self.binding)
        self.runtime = _Runtime()
        self.service = ApplicationObservabilityService(
            self.bindings, self.runtime
        )

    async def test_logs_use_database_binding_and_return_no_luma_name(self) -> None:
        body = await self.service.logs(
            TenantScope(self.tenant_id),
            self.application_id,
            service_key="web",
            tail=50,
            request_id="req_test",
        )
        self.assertEqual(body["logs"], ["ready", "request complete"])
        self.assertNotIn("lumaName", body)
        context, deployment_ref, service_key, tail = self.runtime.log_calls[0]
        self.assertEqual(deployment_ref, self.runtime_deployment_id)
        self.assertEqual(service_key, "web")
        self.assertEqual(tail, 50)
        self.assertEqual(context.headers()["X-LAE-Tenant-Id"], self.tenant_id)
        self.assertEqual(context.headers()["X-LAE-Revision-Id"], self.revision_id)
        self.assertEqual(
            context.headers()["X-LAE-Deployment-Id"], self.deployment_id
        )

    async def test_metrics_are_bounded_to_same_runtime_binding(self) -> None:
        body = await self.service.metrics(
            TenantScope(self.tenant_id),
            self.application_id,
            service_key=None,
            window_seconds=3600,
            request_id=None,
        )
        self.assertEqual(body["deploymentId"], self.deployment_id)
        self.assertEqual(
            body["series"]["cpuPercent"], [[1720000000, 12.5]]
        )
        self.assertEqual(self.bindings.calls[0][2], None)
        self.assertEqual(self.runtime.metric_calls[0][3], 3600)

    async def test_create_app_routes_require_logs_scope_and_hide_runtime_name(self) -> None:
        app = create_app(
            auth_service=_Auth(self.tenant_id),  # type: ignore[arg-type]
            observability=self.service,
        )
        with TestClient(app, base_url="https://lae.example.test") as client:
            logs = client.get(
                f"/v1/applications/{self.application_id}/logs?tail=25",
                headers={"Authorization": "Bearer deploy-token"},
            )
            metrics = client.get(
                f"/v1/applications/{self.application_id}/metrics?window=600",
                headers={"Authorization": "Bearer deploy-token"},
            )
        self.assertEqual(logs.status_code, 200, logs.text)
        self.assertEqual(metrics.status_code, 200, metrics.text)
        self.assertEqual(logs.headers["cache-control"], "no-store, max-age=0")
        self.assertNotIn("lumaName", logs.text)
        self.assertNotIn("lumaName", metrics.text)


if __name__ == "__main__":
    unittest.main()
