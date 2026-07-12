from __future__ import annotations

import dataclasses
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages/python/lae-luma-adapter/src"))

from lae_luma_adapter import (  # noqa: E402
    AdapterErrorCode,
    FakeLumaRuntime,
    HttpLumaRuntimeAdapter,
    LumaAdapterError,
    RuntimeCallContext,
    RuntimeImageBinding,
    RuntimeManifest,
    RuntimeRouteSpec,
    RuntimeServiceHealthcheck,
    RuntimeServicePrincipal,
    RuntimeServiceResources,
    RuntimeServiceSpec,
    RuntimeVolumeMount,
    RuntimeVolumeSpec,
)


SHA = "sha256:" + "a" * 64


def context(*, tenant: str = "ten_01J00000000000000000000000") -> RuntimeCallContext:
    return RuntimeCallContext(
        tenant_ref=tenant,
        application_ref="app_01J00000000000000000000000",
        operation_ref="op_01J000000000000000000000000",
        revision_ref="rev_01J00000000000000000000000",
        deployment_ref="dep_01J00000000000000000000000",
    )


def manifest(*, routes: int = 2) -> RuntimeManifest:
    services = (
        RuntimeServiceSpec(
            "web",
            "http",
            RuntimeImageBinding("builder-task-1", "web", SHA),
            None,
            ("postgres",),
            RuntimeServiceResources("0.50", 512),
            (),
            port=8080,
            healthcheck=RuntimeServiceHealthcheck("/healthz", 10),
        ),
        RuntimeServiceSpec(
            "admin",
            "http",
            RuntimeImageBinding("builder-task-1", "admin", "sha256:" + "b" * 64),
            None,
            (),
            RuntimeServiceResources("0.25", 256),
            (),
            port=9090,
        ),
        RuntimeServiceSpec(
            "postgres",
            "datastore",
            RuntimeImageBinding("builder-task-1", "postgres", "sha256:" + "c" * 64),
            None,
            (),
            RuntimeServiceResources("0.50", 1024),
            (),
        ),
    )
    route_specs = (
        RuntimeRouteSpec(
            "web",
            "a" * 32 + ".itool.tech",
            8080,
            "cn-edge",
            health_path="/healthz",
        ),
        RuntimeRouteSpec("admin", "b" * 32 + ".itool.tech", 9090, "cn-edge"),
    )[:routes]
    return RuntimeManifest(
        name="lae-application",
        kind="compose",
        region="cn",
        services=services,
        routes=route_specs,
        volumes=(
            RuntimeVolumeSpec(
                "pg-data",
                1024,
                "ReadWriteOnce",
                (RuntimeVolumeMount("postgres", "/var/lib/postgresql/data"),),
            ),
        ),
        secrets=(),
        manifest_digest="sha256:" + "d" * 64,
        normalized_compose_digest="sha256:" + "e" * 64,
    )


class RuntimeModelTests(unittest.TestCase):
    def test_shape_has_no_tcp_udp_host_port_bind_mount_or_custom_domain(self) -> None:
        fields = {
            item.name
            for value in (
                RuntimeManifest,
                RuntimeServiceSpec,
                RuntimeRouteSpec,
                RuntimeVolumeSpec,
            )
            for item in dataclasses.fields(value)
        }
        for forbidden in (
            "protocol",
            "publish_port",
            "host_port",
            "host_path",
            "network_mode",
            "privileged",
            "custom_domain",
        ):
            self.assertNotIn(forbidden, fields)
        with self.assertRaises(LumaAdapterError):
            RuntimeServiceSpec(
                "mysql",
                "tcp",
                RuntimeImageBinding("task", "mysql", SHA),
                None,
                (),
                RuntimeServiceResources("0.25", 256),
                (),
            )
        with self.assertRaises(LumaAdapterError):
            RuntimeRouteSpec("web", "custom.example.com", 80, "cn-edge")
        with self.assertRaises(LumaAdapterError):
            RuntimeRouteSpec(
                "web", "a" * 32 + ".itool.tech", 80, "tcp-relay"
            )

    def test_credentials_and_secret_like_refs_are_redacted_from_repr(self) -> None:
        principal = RuntimeServicePrincipal("lae-runtime", "runtime-secret-value")
        self.assertNotIn("runtime-secret-value", repr(principal))


class FakeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeLumaRuntime()
        self.adapter = self.backend.bind(
            RuntimeServicePrincipal("lae-runtime", "runtime-secret-token")
        )

    def test_idempotent_multi_http_deploy_and_tenant_fence(self) -> None:
        ctx = context()
        volumes = self.adapter.prepare_volumes(
            ctx,
            manifest().volumes,
            idempotency_key="volumes-1",
        )
        self.assertEqual(len(volumes), 1)
        first = self.adapter.deploy_revision(
            ctx, manifest(), idempotency_key="deploy-1"
        )
        replay = self.adapter.deploy_revision(
            ctx, manifest(), idempotency_key="deploy-1"
        )
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.deployment.deployment_ref, replay.deployment.deployment_ref)
        healthy = self.backend.set_health(first.deployment.deployment_ref)
        self.assertEqual(healthy.route_statuses, {
            "a" * 32 + ".itool.tech": "ready",
            "b" * 32 + ".itool.tech": "ready",
        })
        logs = self.adapter.tail_runtime_logs(
            ctx, first.deployment.deployment_ref, "web", tail=50
        )
        metrics = self.adapter.get_runtime_metrics_history(
            ctx,
            first.deployment.deployment_ref,
            "web",
            window_seconds=604800,
        )
        self.assertEqual(logs.service_key, "web")
        self.assertEqual(logs.tail, 50)
        self.assertEqual(metrics.window_seconds, 604800)
        with self.assertRaisesRegex(LumaAdapterError, "not found"):
            self.adapter.tail_runtime_logs(
                ctx, first.deployment.deployment_ref, "arbitrary"
            )
        with self.assertRaisesRegex(LumaAdapterError, "not found"):
            self.adapter.get_runtime_deployment(
                context(tenant="ten_01J11111111111111111111111"),
                first.deployment.deployment_ref,
            )

    def test_idempotency_key_cannot_change_manifest(self) -> None:
        ctx = context()
        self.adapter.deploy_revision(ctx, manifest(), idempotency_key="deploy-1")
        with self.assertRaises(LumaAdapterError) as caught:
            self.adapter.deploy_revision(
                ctx, manifest(routes=1), idempotency_key="deploy-1"
            )
        self.assertEqual(caught.exception.code, AdapterErrorCode.IDEMPOTENCY_CONFLICT)

    def test_lifecycle_mutations_are_idempotent_and_require_explicit_volume_policy(self) -> None:
        ctx = context()
        created = self.adapter.deploy_revision(
            ctx, manifest(), idempotency_key="deploy-lifecycle"
        )
        runtime_ref = created.deployment.deployment_ref
        self.backend.set_health(runtime_ref)
        suspended = self.adapter.suspend_runtime_deployment(
            ctx, runtime_ref, idempotency_key="suspend-1"
        )
        replay = self.adapter.suspend_runtime_deployment(
            ctx, runtime_ref, idempotency_key="suspend-1"
        )
        self.assertEqual(suspended.deployment.status, "suspended")
        self.assertTrue(replay.replayed)
        resumed = self.adapter.resume_runtime_deployment(
            ctx, runtime_ref, idempotency_key="resume-1"
        )
        self.assertEqual(resumed.deployment.status, "deploying")
        self.backend.set_health(runtime_ref)
        restarted = self.adapter.restart_runtime_deployment(
            ctx, runtime_ref, idempotency_key="restart-1"
        )
        self.assertEqual(restarted.deployment.status, "deploying")
        self.backend.set_health(runtime_ref)
        with self.assertRaises(LumaAdapterError):
            self.adapter.delete_runtime_deployment(
                ctx,
                runtime_ref,
                volume_policy="default",
                idempotency_key="delete-invalid",
            )
        deleted = self.adapter.delete_runtime_deployment(
            ctx,
            runtime_ref,
            volume_policy="retain",
            idempotency_key="delete-1",
        )
        self.assertEqual(deleted.deployment.status, "deleted")

    def test_rollback_switches_runtime_to_exact_target_and_is_idempotent(self) -> None:
        current_context = context()
        target_context = dataclasses.replace(
            current_context,
            operation_ref=current_context.operation_ref[:-1] + "1",
            revision_ref=current_context.revision_ref[:-1] + "1",
            deployment_ref=current_context.deployment_ref[:-1] + "1",
        )
        target = self.adapter.deploy_revision(
            target_context, manifest(), idempotency_key="deploy-target"
        )
        self.backend.set_health(target.deployment.deployment_ref)
        current = self.adapter.deploy_revision(
            current_context, manifest(), idempotency_key="deploy-current"
        )
        self.backend.set_health(current.deployment.deployment_ref)

        mutation = self.adapter.rollback_runtime_deployment(
            current_context,
            current.deployment.deployment_ref,
            target_context=target_context,
            target_deployment_ref=target.deployment.deployment_ref,
            idempotency_key="rollback-1",
        )
        replay = self.adapter.rollback_runtime_deployment(
            current_context,
            current.deployment.deployment_ref,
            target_context=target_context,
            target_deployment_ref=target.deployment.deployment_ref,
            idempotency_key="rollback-1",
        )

        self.assertEqual(
            mutation.deployment.deployment_ref, target.deployment.deployment_ref
        )
        self.assertEqual(mutation.deployment.status, "deploying")
        self.assertTrue(replay.replayed)
        self.assertEqual(
            self.adapter.get_runtime_deployment(
                current_context, current.deployment.deployment_ref
            ).status,
            "superseded",
        )
        with self.assertRaises(LumaAdapterError):
            self.adapter.rollback_runtime_deployment(
                current_context,
                current.deployment.deployment_ref,
                target_context=dataclasses.replace(
                    target_context,
                    tenant_ref=target_context.tenant_ref[:-1] + "1",
                ),
                target_deployment_ref=target.deployment.deployment_ref,
                idempotency_key="rollback-cross-tenant",
            )


class _Response:
    def __init__(self, body: dict[str, object]) -> None:
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit: int) -> bytes:
        return self._body


class _Opener:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout):
        del timeout
        self.requests.append(request)
        return _Response(self.responses.pop(0))


class _RedirectOpener:
    def open(self, request, *, timeout):
        del timeout
        raise urllib.error.HTTPError(
            request.full_url,
            307,
            "redirect",
            {"Location": "https://attacker.invalid/steal"},
            io.BytesIO(b""),
        )


class _ErrorOpener:
    def __init__(self, status: int, body: dict[str, object]) -> None:
        self.status = status
        self.body = body

    def open(self, request, *, timeout):
        del timeout
        raise urllib.error.HTTPError(
            request.full_url,
            self.status,
            "upstream error",
            {},
            io.BytesIO(json.dumps(self.body).encode()),
        )


class HttpRuntimeTests(unittest.TestCase):
    def test_terminal_runtime_deployment_failure_is_non_retryable(self) -> None:
        adapter = HttpLumaRuntimeAdapter(
            "https://luma.internal.example",
            RuntimeServicePrincipal("lae-runtime", "runtime-only-token"),
            opener=_ErrorOpener(
                422,
                {
                    "requestId": "req-0123456789ab",
                    "errorInfo": {
                        "code": "runtime_deployment_failed",
                        "message": "internal allocation detail",
                        "requestId": "req-0123456789ab",
                    },
                },
            ),  # type: ignore[arg-type]
        )

        with self.assertRaises(LumaAdapterError) as caught:
            adapter.deploy_revision(
                context(), manifest(), idempotency_key="deploy-terminal-error"
            )

        self.assertEqual(
            caught.exception.code, AdapterErrorCode.RUNTIME_DEPLOY_FAILED
        )
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("allocation", str(caught.exception))

    def test_409_uses_closed_upstream_code_instead_of_assuming_idempotency(self) -> None:
        cases = (
            (
                "volume_placement_incompatible",
                AdapterErrorCode.CAPACITY_UNAVAILABLE,
                False,
            ),
            ("conflict", AdapterErrorCode.IDEMPOTENCY_CONFLICT, False),
            ("future_conflict", AdapterErrorCode.PROTOCOL_ERROR, False),
        )
        for upstream_code, expected, retryable in cases:
            with self.subTest(upstream_code=upstream_code):
                adapter = HttpLumaRuntimeAdapter(
                    "https://luma.internal.example",
                    RuntimeServicePrincipal("lae-runtime", "runtime-only-token"),
                    opener=_ErrorOpener(
                        409,
                        {
                            "requestId": "req-0123456789ab",
                            "errorInfo": {
                                "code": upstream_code,
                                "message": "internal node 10.0.0.1 secret-canary",
                                "requestId": "req-0123456789ab",
                            },
                        },
                    ),  # type: ignore[arg-type]
                )
                with self.assertRaises(LumaAdapterError) as caught:
                    adapter.deploy_revision(
                        context(), manifest(), idempotency_key="deploy-error"
                    )
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertEqual(
                    caught.exception.request_id, "req-0123456789ab"
                )
                self.assertNotIn("10.0.0.1", str(caught.exception))
                self.assertNotIn("secret-canary", repr(caught.exception))

    def test_uses_only_scoped_endpoint_and_binding_headers(self) -> None:
        ctx = context()
        response = {
            "schemaVersion": "luma.lae-runtime/v1",
            "replayed": False,
            "deployment": {
                "deploymentRef": "lae-run-1",
                "status": "deploying",
                "manifestDigest": manifest().manifest_digest,
                "serviceStatuses": {"web": "pending"},
                "routeStatuses": {"a" * 32 + ".itool.tech": "pending"},
                "volumeBindings": [{"key": "pg-data", "volumeRef": "lv_12345678"}],
            },
        }
        opener = _Opener([response])
        adapter = HttpLumaRuntimeAdapter(
            "https://luma.internal.example",
            RuntimeServicePrincipal("lae-runtime", "runtime-only-token"),
            opener=opener,  # type: ignore[arg-type]
        )
        adapter.deploy_revision(ctx, manifest(), idempotency_key="deploy-1")
        request = opener.requests[0]
        self.assertTrue(request.full_url.endswith("/v1/lae/runtime/deployments"))
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["x-luma-principal-audience"], "luma-lae-runtime")
        self.assertEqual(headers["x-lae-tenant-id"], ctx.tenant_ref)
        self.assertEqual(headers["x-lae-revision-id"], ctx.revision_ref)
        self.assertEqual(headers["x-lae-deployment-id"], ctx.deployment_ref)
        body = json.loads(request.data)
        serialized = json.dumps(body).lower()
        for forbidden in ("tcp-relay", "publishport", "hostport", "customdomain"):
            self.assertNotIn(forbidden, serialized)

    def test_redirect_is_failure_and_bearer_is_never_forwarded(self) -> None:
        adapter = HttpLumaRuntimeAdapter(
            "https://luma.internal.example",
            RuntimeServicePrincipal("lae-runtime", "runtime-only-token"),
            opener=_RedirectOpener(),  # type: ignore[arg-type]
        )
        with self.assertRaises(LumaAdapterError) as caught:
            adapter.deploy_revision(context(), manifest(), idempotency_key="deploy-1")
        self.assertEqual(caught.exception.code, AdapterErrorCode.UPSTREAM_UNAVAILABLE)

    def test_lifecycle_uses_scoped_endpoint_binding_and_idempotency(self) -> None:
        ctx = context()
        response = {
            "schemaVersion": "luma.lae-runtime/v1",
            "replayed": False,
            "deployment": {
                "deploymentRef": "lae-run-1",
                "status": "suspended",
                "manifestDigest": manifest().manifest_digest,
                "serviceStatuses": {"web": "suspended"},
                "routeStatuses": {"a" * 32 + ".itool.tech": "suspended"},
                "volumeBindings": [],
            },
        }
        opener = _Opener([response])
        adapter = HttpLumaRuntimeAdapter(
            "https://luma.internal.example",
            RuntimeServicePrincipal("lae-runtime", "runtime-only-token"),
            opener=opener,  # type: ignore[arg-type]
        )
        adapter.suspend_runtime_deployment(
            ctx, "lae-run-1", idempotency_key="suspend-1"
        )
        request = opener.requests[0]
        self.assertTrue(
            request.full_url.endswith(
                "/v1/lae/runtime/deployments/lae-run-1/suspend"
            )
        )
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["idempotency-key"], "suspend-1")
        self.assertEqual(headers["x-lae-operation-id"], ctx.operation_ref)
        self.assertEqual(
            json.loads(request.data),
            {"schemaVersion": "luma.lae-runtime/v1"},
        )

    def test_rollback_uses_current_binding_and_closed_target_binding(self) -> None:
        current_context = context()
        target_context = dataclasses.replace(
            current_context,
            operation_ref=current_context.operation_ref[:-1] + "1",
            revision_ref=current_context.revision_ref[:-1] + "1",
            deployment_ref=current_context.deployment_ref[:-1] + "1",
        )
        response = {
            "schemaVersion": "luma.lae-runtime/v1",
            "replayed": False,
            "deployment": {
                "deploymentRef": "lae-run-target",
                "status": "deploying",
                "manifestDigest": manifest().manifest_digest,
                "serviceStatuses": {"web": "pending"},
                "routeStatuses": {"a" * 32 + ".itool.tech": "pending"},
                "volumeBindings": [],
            },
        }
        opener = _Opener([response])
        adapter = HttpLumaRuntimeAdapter(
            "https://luma.internal.example",
            RuntimeServicePrincipal("lae-runtime", "runtime-only-token"),
            opener=opener,  # type: ignore[arg-type]
        )

        adapter.rollback_runtime_deployment(
            current_context,
            "lae-run-current",
            target_context=target_context,
            target_deployment_ref="lae-run-target",
            idempotency_key="rollback-1",
        )

        request = opener.requests[0]
        self.assertTrue(
            request.full_url.endswith(
                "/v1/lae/runtime/deployments/lae-run-current/rollback"
            )
        )
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["idempotency-key"], "rollback-1")
        self.assertEqual(
            headers["x-lae-deployment-id"], current_context.deployment_ref
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "schemaVersion": "luma.lae-runtime/v1",
                "target": {
                    "runtimeDeploymentRef": "lae-run-target",
                    "operationRef": target_context.operation_ref,
                    "revisionRef": target_context.revision_ref,
                    "deploymentRef": target_context.deployment_ref,
                },
            },
        )

    def test_observability_parses_only_bounded_closed_responses(self) -> None:
        ctx = context()
        log_response = {
            "schemaVersion": "luma.lae-runtime/v1",
            "lumaName": "lae-application",
            "serviceKey": "web",
            "tail": 25,
            "logs": ["safe line"],
            "truncated": False,
            "updatedAt": 100,
        }
        metric_response = {
            "schemaVersion": "luma.lae-runtime/v1",
            "lumaName": "lae-application",
            "serviceKey": "web",
            "windowSeconds": 3600,
            "series": {
                "cpuPercent": [[100, 1.25]],
                "memoryUsageBytes": [[100, 4096]],
            },
            "updatedAt": 101,
        }
        opener = _Opener([log_response, metric_response])
        adapter = HttpLumaRuntimeAdapter(
            "https://luma.internal.example",
            RuntimeServicePrincipal("lae-runtime", "runtime-only-token"),
            opener=opener,  # type: ignore[arg-type]
        )
        logs = adapter.tail_runtime_logs(
            ctx, "lae-run-1", "web", tail=25
        )
        metrics = adapter.get_runtime_metrics_history(
            ctx, "lae-run-1", "web", window_seconds=3600
        )
        self.assertEqual(logs.logs, ("safe line",))
        self.assertEqual(metrics.series["cpuPercent"], ((100, 1.25),))
        self.assertTrue(
            opener.requests[0].full_url.endswith(
                "/services/web/logs?tail=25"
            )
        )
        self.assertTrue(
            opener.requests[1].full_url.endswith(
                "/services/web/metrics?window=3600"
            )
        )
        with self.assertRaises(LumaAdapterError):
            adapter.tail_runtime_logs(ctx, "lae-run-1", "web", tail=501)


if __name__ == "__main__":
    unittest.main()
