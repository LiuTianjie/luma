from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from luma.control import server as control_server
from luma.control.server import ControlHandler, create_app
from luma.control.state import init_state, load_state, save_state
from luma.lae_runtime import (
    RUNTIME_AUDIENCE,
    RUNTIME_SECRETS,
    RuntimeBinding,
    canonical_hash,
)
from luma.errors import LumaError
from luma.nomad_api import NomadApi

LAE_ADAPTER_SRC = (
    Path(__file__).resolve().parents[1]
    / "lae/packages/python/lae-luma-adapter/src"
)
sys.path.insert(0, str(LAE_ADAPTER_SRC))

from lae_luma_adapter import (  # noqa: E402
    RuntimeImageBinding as AdapterRuntimeImageBinding,
    RuntimeManifest as AdapterRuntimeManifest,
    RuntimeRouteSpec as AdapterRuntimeRouteSpec,
    RuntimeServiceResources as AdapterRuntimeServiceResources,
    RuntimeServiceSpec as AdapterRuntimeServiceSpec,
    RuntimeVolumeMount as AdapterRuntimeVolumeMount,
    RuntimeVolumeSpec as AdapterRuntimeVolumeSpec,
)


class NomadVariableBoundaryTests(unittest.TestCase):
    def test_variable_values_are_only_in_put_body_and_never_in_public_errors(self) -> None:
        api = NomadApi("http://nomad.internal", token="nomad-token")
        canary = "nomad-variable-secret-canary"
        with patch.object(api, "request", return_value={"Path": "safe"}) as request:
            api.put_variable(
                "nomad/jobs/app/group/task", {"DATABASE_URL": canary}
            )
        self.assertEqual(request.call_args.args[0:2], (
            "PUT",
            "/v1/var/nomad/jobs/app/group/task",
        ))
        self.assertEqual(
            request.call_args.args[2], {"Items": {"DATABASE_URL": canary}}
        )
        with patch.object(
            api,
            "request",
            side_effect=LumaError("upstream echoed " + canary),
        ):
            with self.assertRaises(LumaError) as caught:
                api.put_variable(
                    "nomad/jobs/app/group/task", {"DATABASE_URL": canary}
                )
        self.assertNotIn(canary, str(caught.exception))

    def test_variable_delete_is_idempotent_on_nomad_not_found(self) -> None:
        api = NomadApi("http://nomad.internal", token="nomad-token")
        with patch.object(
            api,
            "request",
            side_effect=LumaError("Nomad API error 404: missing"),
        ):
            api.delete_variable("nomad/jobs/app/group/task")

    def test_variable_read_returns_items_but_redacts_upstream_errors(self) -> None:
        api = NomadApi("http://nomad.internal", token="nomad-token")
        canary = "variable-read-secret-canary"
        with patch.object(
            api,
            "request",
            return_value={"Items": {"PASSWORD": canary}},
        ):
            self.assertEqual(
                api.get_variable("nomad/jobs/app/group/task"),
                {"PASSWORD": canary},
            )
        with patch.object(
            api,
            "request",
            side_effect=LumaError("upstream echoed " + canary),
        ):
            with self.assertRaises(LumaError) as caught:
                api.get_variable("nomad/jobs/app/group/task")
        self.assertNotIn(canary, str(caught.exception))


class RuntimeWireCompatibilityTests(unittest.TestCase):
    def test_adapter_to_wire_digest_is_accepted_without_translation(self) -> None:
        binding = RuntimeBinding(
            "tenant-wire",
            "application-wire",
            "operation-wire",
            "revision-wire",
            "deployment-wire",
        )
        services = (
            AdapterRuntimeServiceSpec(
                "web",
                "http",
                AdapterRuntimeImageBinding(
                    "builder-task-wire", "web", "sha256:" + "a" * 64
                ),
                None,
                (),
                AdapterRuntimeServiceResources("0.25", 256),
                (),
                port=8080,
            ),
        )
        routes = (
            AdapterRuntimeRouteSpec(
                "web", "a" * 32 + ".itool.tech", 8080, "cn-edge"
            ),
        )
        volumes = (
            AdapterRuntimeVolumeSpec(
                "data",
                1024,
                "ReadWriteOnce",
                (AdapterRuntimeVolumeMount("web", "/data"),),
                existing_ref="lv_12345678",
            ),
        )
        digest = canonical_hash(
            {
                "schemaVersion": "lae.runtime-manifest/v1",
                "applicationId": binding.application_ref,
                "revisionId": binding.revision_ref,
                "name": "wire-app",
                "kind": "service",
                "region": "cn",
                "services": [item.to_wire() for item in services],
                "routes": [item.to_wire() for item in routes],
                "volumes": [item.to_wire() for item in volumes],
                "environment": [],
            }
        )
        manifest = AdapterRuntimeManifest(
            "wire-app",
            "service",
            "cn",
            services,
            routes,
            volumes,
            (),
            digest,
        )
        validated = control_server._validate_lae_runtime_deploy_body(
            {
                "schemaVersion": "luma.lae-runtime/v1",
                "manifest": manifest.to_wire(),
            },
            binding,
        )
        self.assertEqual(validated["services"], [services[0].to_wire()])
        self.assertEqual(validated["volumes"], [volumes[0].to_wire()])


class LaeRuntimeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "luma.yaml"
        self.config_path.write_text(
            "defaults:\n"
            f"  stackRoot: {self.root / 'stacks'}\n"
            f"  routesRoot: {self.root / 'routes'}\n"
            "providers: {}\n",
            encoding="utf-8",
        )
        self.builder_token = "builder-principal-token-only"
        self.runtime_token = "runtime-principal-token-only"
        self.env = patch.dict(
            os.environ,
            {
                "LUMA_CONTROL_STATE_DIR": str(self.state_dir),
                "LUMA_CONTROL_CONFIG": str(self.config_path),
                "LUMA_LAE_SERVICE_PRINCIPALS_JSON": json.dumps(
                    {
                        "lae-builder": {
                            "token": self.builder_token,
                            "tenantRefs": ["tenant-runtime"],
                            "applicationRefs": ["app-runtime"],
                        }
                    }
                ),
                "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_JSON": json.dumps(
                    {
                        "lae-runtime": {
                            "token": self.runtime_token,
                            "tenantRefs": ["tenant-runtime"],
                            "applicationRefs": ["app-runtime"],
                            "builderPrincipalRefs": ["lae-builder"],
                            "scopes": [
                                "runtime:volumes:prepare",
                                "runtime:deployments:write",
                                "runtime:deployments:read",
                                "runtime:logs",
                                "runtime:metrics",
                                "runtime:secrets:issue",
                            ],
                        }
                    }
                ),
                "LUMA_LAE_RUNTIME_STORAGE_CLASS": "lae-runtime-nfs",
                "LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON": '["runtime-node"]',
            },
            clear=False,
        )
        self.env.start()
        state = init_state(
            domain="luma.test",
            cluster_id="luma-runtime-test",
            overwrite=True,
        )
        self.management_token = state["deployToken"]
        state["nomadToken"] = "nomad-management-token"
        state["storageClasses"] = {
            "lae-runtime-nfs": {
                "provider": "nfs",
                "mode": "managed",
                "node": "storage-node",
                "path": "/srv/luma",
                "regions": ["cn"],
            }
        }
        state["nodes"] = {
            "storage-node": {
                "region": "cn",
                "hostname": "nfs.internal.test",
            }
        }
        state["builderTasks"] = {
            "builder-runtime": {
                "id": "builder-runtime",
                "kind": "build-plan",
                "status": "succeeded",
                "externalOperationId": "op-runtime",
                "tenantRef": "tenant-runtime",
                "applicationRef": "app-runtime",
                "principalRef": "lae-builder",
                "result": {
                    "images": {
                        "web": "registry.internal/app/web@sha256:" + "a" * 64,
                        "admin": "registry.internal/app/admin@sha256:" + "b" * 64,
                        "postgres": "docker.io/library/postgres@sha256:" + "c" * 64,
                    },
                    "imageDigests": {
                        "web": "sha256:" + "a" * 64,
                        "admin": "sha256:" + "b" * 64,
                        "postgres": "sha256:" + "c" * 64,
                    },
                },
            }
        }
        save_state(state)
        RUNTIME_SECRETS.clear()
        self.binding = RuntimeBinding(
            tenant_ref="tenant-runtime",
            application_ref="app-runtime",
            operation_ref="op-runtime",
            revision_ref="rev-runtime",
            deployment_ref="dep-runtime",
        )

    def tearDown(self) -> None:
        RUNTIME_SECRETS.clear()
        self.env.stop()
        self.tmp.cleanup()

    def headers(
        self,
        *,
        token: str | None = None,
        idempotency_key: str | None = None,
        binding: RuntimeBinding | None = None,
    ) -> dict[str, str]:
        selected = binding or self.binding
        value = {
            "Authorization": f"Bearer {token or self.runtime_token}",
            "X-Luma-Principal-Audience": RUNTIME_AUDIENCE,
            "X-LAE-Tenant-Id": selected.tenant_ref,
            "X-LAE-Application-Id": selected.application_ref,
            "X-LAE-Operation-Id": selected.operation_ref,
            "X-LAE-Revision-Id": selected.revision_ref,
            "X-LAE-Deployment-Id": selected.deployment_ref,
        }
        if idempotency_key is not None:
            value["Idempotency-Key"] = idempotency_key
        return value

    @staticmethod
    def volume_body(*, existing_ref: str | None = None) -> dict[str, object]:
        volume: dict[str, object] = {
            "key": "pg-data",
            "requestedBytes": 1024 * 1024 * 1024,
            "storagePolicy": "managed",
            "accessMode": "ReadWriteOnce",
            "mounts": [
                {
                    "serviceKey": "postgres",
                    "mountPath": "/var/lib/postgresql/data",
                    "readOnly": False,
                }
            ],
        }
        if existing_ref is not None:
            volume["existingRef"] = existing_ref
        return {"schemaVersion": "luma.lae-runtime/v1", "volumes": [volume]}

    def issue_secret(self, service: str, name: str, value: str) -> str:
        result = control_server.handle_lae_runtime_secret_issue(
            self.runtime_token,
            RUNTIME_AUDIENCE,
            self.binding,
            {
                "schemaVersion": "luma.lae-runtime/v1",
                "serviceKey": service,
                "name": name,
                "plaintext": value,
                "environmentVersion": 3,
                "ttlSeconds": 300,
            },
            idempotency_key=f"secret-{service}-{name}",
        )
        return str(result["secret"]["secretRef"])

    def prepare_volume(self) -> str:
        result = control_server.handle_lae_runtime_volume_prepare(
            self.runtime_token,
            RUNTIME_AUDIENCE,
            self.binding,
            self.volume_body(),
            idempotency_key="volume-prepare",
        )
        return str(result["volumes"][0]["volumeRef"])

    def deployment_body(
        self,
        *,
        volume_ref: str,
        secret_refs: dict[tuple[str, str], str] | None = None,
        binding: RuntimeBinding | None = None,
    ) -> dict[str, object]:
        secret_refs = secret_refs or {}
        selected_binding = binding or self.binding
        services = [
            {
                "key": "web",
                "role": "http",
                "required": True,
                "exposure": "none",
                "image": {
                    "builderTaskRef": "builder-runtime",
                    "buildKey": "web",
                    "imageDigest": "sha256:" + "a" * 64,
                },
                "command": None,
                "dependencies": ["postgres"],
                "resources": {"cpu": "0.50", "memoryMiB": 512},
                "environmentNames": [
                    name for service, name in secret_refs if service == "web"
                ],
                "port": 8080,
                "healthcheck": {
                    "type": "http",
                    "path": "/healthz",
                    "intervalSeconds": 10,
                },
            },
            {
                "key": "admin",
                "role": "http",
                "required": True,
                "exposure": "none",
                "image": {
                    "builderTaskRef": "builder-runtime",
                    "buildKey": "admin",
                    "imageDigest": "sha256:" + "b" * 64,
                },
                "command": None,
                "dependencies": [],
                "resources": {"cpu": "0.25", "memoryMiB": 256},
                "environmentNames": [
                    name for service, name in secret_refs if service == "admin"
                ],
                "port": 9090,
            },
            {
                "key": "postgres",
                "role": "datastore",
                "required": True,
                "exposure": "none",
                "image": {
                    "builderTaskRef": "builder-runtime",
                    "buildKey": "postgres",
                    "imageDigest": "sha256:" + "c" * 64,
                },
                "command": None,
                "dependencies": [],
                "resources": {"cpu": "0.50", "memoryMiB": 1024},
                "environmentNames": [
                    name
                    for service, name in secret_refs
                    if service == "postgres"
                ],
            },
        ]
        routes = [
            {
                "serviceKey": "web",
                "kind": "http",
                "hostname": "a" * 32 + ".itool.tech",
                "containerPort": 8080,
                "exposure": "cn-edge",
                "healthPath": "/healthz",
            },
            {
                "serviceKey": "admin",
                "kind": "http",
                "hostname": "b" * 32 + ".itool.tech",
                "containerPort": 9090,
                "exposure": "cn-edge",
                "healthPath": "/",
            },
        ]
        volumes = list(self.volume_body(existing_ref=volume_ref)["volumes"])
        secrets = [
            {
                "serviceKey": service,
                "name": name,
                "secretRef": ref,
                "environmentVersion": 3,
            }
            for (service, name), ref in sorted(secret_refs.items())
        ]
        digest = canonical_hash(
            {
                "schemaVersion": "lae.runtime-manifest/v1",
                "applicationId": selected_binding.application_ref,
                "revisionId": selected_binding.revision_ref,
                "name": "lae-runtime-app",
                "kind": "compose",
                "region": "cn",
                "services": services,
                "routes": routes,
                "volumes": volumes,
                "environment": [
                    {
                        "serviceKey": item["serviceKey"],
                        "name": item["name"],
                        "environmentVersion": item["environmentVersion"],
                    }
                    for item in secrets
                ],
            }
        )
        return {
            "schemaVersion": "luma.lae-runtime/v1",
            "manifest": {
                "schemaVersion": "luma.lae-runtime/v1",
                "name": "lae-runtime-app",
                "kind": "compose",
                "region": "cn",
                "services": services,
                "routes": routes,
                "volumes": volumes,
                "secretRefs": secrets,
                "manifestDigest": digest,
                "normalizedComposeDigest": "sha256:" + "d" * 64,
            },
        }

    @staticmethod
    def fake_execution(**kwargs: object) -> dict[str, object]:
        binding = kwargs["binding"]
        assert isinstance(binding, RuntimeBinding)
        return {
            "jobSlug": control_server._lae_runtime_job_slug(binding),
            "taskNames": {
                key: control_server._lae_runtime_task_name(
                    key, binding.revision_ref
                )
                for key in ("web", "admin", "postgres")
            },
            "variablePaths": [],
            "previousNomadVersion": None,
            "nomadVersion": 1,
            "composeManifest": "name: test\n",
            "composeContent": "services: {}\n",
        }

    def test_scoped_handlers_are_idempotent_and_reject_management_builder_and_tenant_crossing(self) -> None:
        volume_ref = self.prepare_volume()
        replay = control_server.handle_lae_runtime_volume_prepare(
            self.runtime_token,
            RUNTIME_AUDIENCE,
            self.binding,
            self.volume_body(),
            idempotency_key="volume-prepare",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["volumes"][0]["volumeRef"], volume_ref)
        for token in (self.management_token, self.builder_token):
            with self.subTest(token=token), self.assertRaisesRegex(
                Exception, "unauthorized"
            ):
                control_server.handle_lae_runtime_volume_prepare(
                    token,
                    RUNTIME_AUDIENCE,
                    self.binding,
                    self.volume_body(),
                    idempotency_key="wrong-token",
                )
        foreign = RuntimeBinding(
            "tenant-foreign",
            self.binding.application_ref,
            self.binding.operation_ref,
            self.binding.revision_ref,
            self.binding.deployment_ref,
        )
        with self.assertRaisesRegex(Exception, "forbidden"):
            control_server.handle_lae_runtime_volume_prepare(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                foreign,
                self.volume_body(),
                idempotency_key="foreign",
            )

        body = self.deployment_body(volume_ref=volume_ref)
        with patch.object(
            control_server,
            "_execute_lae_runtime_deployment",
            side_effect=self.fake_execution,
        ):
            first = control_server.handle_lae_runtime_deployment_create(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                body,
                idempotency_key="deploy-idem",
            )
            second = control_server.handle_lae_runtime_deployment_create(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                body,
                idempotency_key="deploy-idem",
            )
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(
            first["deployment"]["deploymentRef"],
            second["deployment"]["deploymentRef"],
        )

    def test_terminal_nomad_rollout_returns_non_retryable_runtime_failure(self) -> None:
        volume_ref = self.prepare_volume()
        with patch.object(
            control_server,
            "_execute_lae_runtime_deployment",
            side_effect=control_server.NomadRolloutError(
                "internal allocation and node details must stay private"
            ),
        ), TestClient(create_app()) as client:
            failed = client.post(
                "/v1/lae/runtime/deployments",
                json=self.deployment_body(volume_ref=volume_ref),
                headers=self.headers(idempotency_key="deploy-terminal-failure"),
            )

        self.assertEqual(failed.status_code, 422, failed.text)
        payload = failed.json()
        self.assertEqual(
            payload["errorInfo"]["code"], "runtime_deployment_failed"
        )
        self.assertNotIn("allocation", failed.text)
        state = load_state()
        records = list(state["laeRuntime"]["deployments"].values())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "failed")
        self.assertFalse(records[0]["retryable"])

    def test_closed_schema_rejects_tcp_custom_domain_unknown_fields_and_mount_drift(self) -> None:
        volume_ref = self.prepare_volume()
        body = self.deployment_body(volume_ref=volume_ref)
        body["manifest"]["unknown"] = True
        with self.assertRaises(Exception):
            control_server.handle_lae_runtime_deployment_create(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                body,
                idempotency_key="unknown",
            )
        body = self.deployment_body(volume_ref=volume_ref)
        body["manifest"]["routes"][0]["exposure"] = "tcp-relay"
        with self.assertRaises(Exception):
            control_server.handle_lae_runtime_deployment_create(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                body,
                idempotency_key="tcp",
            )
        body = self.deployment_body(volume_ref=volume_ref)
        body["manifest"]["routes"][0]["hostname"] = "custom.example.com"
        with self.assertRaises(Exception):
            control_server.handle_lae_runtime_deployment_create(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                body,
                idempotency_key="domain",
            )

    def test_render_uses_nomad_variables_and_managed_nfs_without_secret_in_state_or_job(self) -> None:
        canary = "secret-canary-quoted-#-value"
        secret_ref = self.issue_secret("postgres", "POSTGRES_PASSWORD", canary)
        volume_ref = self.prepare_volume()
        body = self.deployment_body(
            volume_ref=volume_ref,
            secret_refs={("postgres", "POSTGRES_PASSWORD"): secret_ref},
        )
        manifest = control_server._validate_lae_runtime_deploy_body(
            body, self.binding
        )
        state = load_state()
        principal = control_server._require_lae_runtime_principal(
            state,
            self.runtime_token,
            audience=RUNTIME_AUDIENCE,
            scope="runtime:deployments:write",
            binding=self.binding,
        )
        images = control_server._lae_runtime_resolve_images(
            state, {"lae-builder"}, self.binding, manifest
        )
        volumes = control_server._lae_runtime_validate_volumes(
            state, str(principal["id"]), self.binding, manifest
        )
        spec = control_server._lae_runtime_compose_spec(
            state,
            self.binding,
            manifest,
            images,
            volumes,
            job_slug=control_server._lae_runtime_job_slug(self.binding),
        )
        config = control_server.load_config(self.config_path)
        values = RUNTIME_SECRETS.resolve_manifest(
            principal_ref=str(principal["id"]),
            binding=self.binding,
            secret_refs=list(manifest["secretRefs"]),
        )
        job_json, variable_items, paths = control_server._lae_runtime_render_job(
            state,
            config,
            self.binding,
            manifest,
            spec,
            values,
            runtime_deployment_ref="lae-run-test",
        )
        self.assertNotIn(canary, job_json)
        self.assertNotIn(canary, self.state_dir.joinpath("control.json").read_text())
        self.assertEqual([items["POSTGRES_PASSWORD"] for items in variable_items.values()], [canary])
        self.assertEqual(len(paths), 1)
        parsed = json.loads(job_json)["Job"]
        tasks = parsed["TaskGroups"][0]["Tasks"]
        postgres = next(task for task in tasks if task["Name"].startswith("postgres-r"))
        self.assertNotIn("Env", postgres)
        self.assertIn("nomadVar", postgres["Templates"][0]["EmbeddedTmpl"])
        mount = postgres["Config"]["mount"][0]
        self.assertEqual(mount["target"], "/var/lib/postgresql/data")
        self.assertEqual(
            mount["volume_options"]["driver_config"]["options"]["type"],
            "nfs",
        )
        serialized = json.dumps(parsed).lower()
        for forbidden in ("tcp-relay", "host_path", "privileged", "network_mode"):
            self.assertNotIn(forbidden, serialized)

    def test_asgi_full_runtime_flow_and_management_token_rejection(self) -> None:
        with TestClient(create_app()) as client:
            management = client.post(
                "/v1/lae/runtime/volumes:prepare",
                json=self.volume_body(),
                headers=self.headers(
                    token=self.management_token,
                    idempotency_key="management-volume",
                ),
            )
            self.assertEqual(management.status_code, 401)
            prepared = client.post(
                "/v1/lae/runtime/volumes:prepare",
                json=self.volume_body(),
                headers=self.headers(idempotency_key="asgi-volume"),
            )
            self.assertEqual(prepared.status_code, 200)
            volume_ref = prepared.json()["volumes"][0]["volumeRef"]
            body = self.deployment_body(volume_ref=volume_ref)
            with patch.object(
                control_server,
                "_execute_lae_runtime_deployment",
                side_effect=self.fake_execution,
            ):
                deployed = client.post(
                    "/v1/lae/runtime/deployments",
                    json=body,
                    headers=self.headers(idempotency_key="asgi-deploy"),
                )
            self.assertEqual(deployed.status_code, 202, deployed.text)
            runtime_ref = deployed.json()["deployment"]["deploymentRef"]

            def healthy(_state: object, record: dict[str, object]) -> dict[str, object]:
                return {
                    **record,
                    "status": "running",
                    "serviceStatuses": {
                        "web": "healthy",
                        "admin": "healthy",
                        "postgres": "healthy",
                    },
                    "routeStatuses": {
                        "a" * 32 + ".itool.tech": "ready",
                        "b" * 32 + ".itool.tech": "ready",
                    },
                }

            with patch.object(
                control_server,
                "_observe_lae_runtime_deployment",
                side_effect=healthy,
            ):
                fetched = client.get(
                    f"/v1/lae/runtime/deployments/{runtime_ref}",
                    headers=self.headers(),
                )
            self.assertEqual(fetched.status_code, 200, fetched.text)
            self.assertEqual(fetched.json()["deployment"]["status"], "running")
            with patch.object(
                control_server, "_execute_lae_runtime_cancel", return_value=None
            ):
                canceled = client.post(
                    f"/v1/lae/runtime/deployments/{runtime_ref}/cancel",
                    json={},
                    headers=self.headers(),
                )
            self.assertEqual(canceled.status_code, 200, canceled.text)
            self.assertEqual(canceled.json()["deployment"]["status"], "canceled")

    def test_lifecycle_routes_are_bound_idempotent_and_delete_policy_is_explicit(self) -> None:
        volume_ref = self.prepare_volume()
        body = self.deployment_body(volume_ref=volume_ref)
        with patch.object(
            control_server,
            "_execute_lae_runtime_deployment",
            side_effect=self.fake_execution,
        ):
            deployed = control_server.handle_lae_runtime_deployment_create(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                body,
                idempotency_key="deploy-lifecycle",
            )
        runtime_ref = str(deployed["deployment"]["deploymentRef"])
        state = load_state()
        state["laeRuntime"]["deployments"][runtime_ref]["status"] = "running"
        save_state(state)

        lifecycle_body = {"schemaVersion": "luma.lae-runtime/v1"}
        with patch.object(
            control_server,
            "_execute_lae_runtime_lifecycle",
            return_value={"suspendedNomadVersion": 7},
        ) as execute:
            with TestClient(create_app()) as client:
                suspended = client.post(
                    f"/v1/lae/runtime/deployments/{runtime_ref}/suspend",
                    json=lifecycle_body,
                    headers=self.headers(idempotency_key="suspend-1"),
                )
                replay = client.post(
                    f"/v1/lae/runtime/deployments/{runtime_ref}/suspend",
                    json=lifecycle_body,
                    headers=self.headers(idempotency_key="suspend-1"),
                )
                management = client.post(
                    f"/v1/lae/runtime/deployments/{runtime_ref}/suspend",
                    json=lifecycle_body,
                    headers=self.headers(
                        token=self.management_token,
                        idempotency_key="management-cannot-suspend",
                    ),
                )
            self.assertEqual(suspended.status_code, 200, suspended.text)
            self.assertEqual(suspended.json()["deployment"]["status"], "suspended")
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(management.status_code, 401)

        foreign = RuntimeBinding(
            self.binding.tenant_ref,
            self.binding.application_ref,
            "op-other",
            self.binding.revision_ref,
            self.binding.deployment_ref,
        )
        with self.assertRaisesRegex(Exception, "not found"):
            control_server.handle_lae_runtime_deployment_lifecycle(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                foreign,
                runtime_ref,
                "resume",
                lifecycle_body,
                idempotency_key="foreign-resume",
            )

        with patch.object(
            control_server,
            "_execute_lae_runtime_lifecycle",
            return_value={"nomadVersion": 8},
        ):
            resumed = control_server.handle_lae_runtime_deployment_lifecycle(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                runtime_ref,
                "resume",
                lifecycle_body,
                idempotency_key="resume-1",
            )
        self.assertEqual(resumed["deployment"]["status"], "deploying")

        state = load_state()
        state["laeRuntime"]["deployments"][runtime_ref]["status"] = "running"
        save_state(state)
        with patch.object(
            control_server,
            "_execute_lae_runtime_lifecycle",
            return_value={"restartedAllocations": 1},
        ):
            restarted = control_server.handle_lae_runtime_deployment_lifecycle(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                runtime_ref,
                "restart",
                lifecycle_body,
                idempotency_key="restart-1",
            )
        self.assertEqual(restarted["deployment"]["status"], "deploying")

        with self.assertRaisesRegex(Exception, "volumePolicy"):
            control_server.handle_lae_runtime_deployment_lifecycle(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                runtime_ref,
                "delete",
                lifecycle_body,
                idempotency_key="delete-invalid",
            )
        state = load_state()
        state["laeRuntime"]["deployments"][runtime_ref]["status"] = "running"
        save_state(state)
        with patch.object(
            control_server,
            "_execute_lae_runtime_lifecycle",
            return_value={"deletedVolumeRefs": [volume_ref]},
        ):
            deleted = control_server.handle_lae_runtime_deployment_lifecycle(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                runtime_ref,
                "delete",
                {
                    "schemaVersion": "luma.lae-runtime/v1",
                    "volumePolicy": "delete",
                },
                idempotency_key="delete-1",
            )
        self.assertEqual(deleted["deployment"]["status"], "deleted")
        self.assertNotIn(volume_ref, load_state()["laeRuntime"]["volumes"])

    def test_restart_waits_for_replacement_allocation_before_reporting_success(self) -> None:
        volume_ref = self.prepare_volume()
        with patch.object(
            control_server,
            "_execute_lae_runtime_deployment",
            side_effect=self.fake_execution,
        ):
            deployed = control_server.handle_lae_runtime_deployment_create(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                self.deployment_body(volume_ref=volume_ref),
                idempotency_key="deploy-restart-replacement",
            )
        runtime_ref = str(deployed["deployment"]["deploymentRef"])
        state = load_state()
        state["laeRuntime"]["deployments"][runtime_ref]["status"] = "restarting"
        save_state(state)
        record = dict(state["laeRuntime"]["deployments"][runtime_ref])
        api = MagicMock()
        api.request.side_effect = [
            [{"ID": "alloc-old", "ClientStatus": "running"}],
            {},
        ]
        bound_spec = object()
        with (
            patch.object(control_server, "NomadApi", return_value=api),
            patch.object(
                control_server,
                "_lae_runtime_verified_job",
                return_value={"ID": record["jobSlug"]},
            ),
            patch.object(
                control_server,
                "_lae_runtime_bound_compose_spec",
                return_value=bound_spec,
            ),
            patch.object(
                control_server,
                "_wait_for_nomad_job_replacement",
                return_value=["alloc-new"],
            ) as wait_for_replacement,
            patch.object(control_server, "_lae_runtime_restore_routes") as restore_routes,
        ):
            result = control_server._execute_lae_runtime_lifecycle(
                "restart",
                record,
                token=self.runtime_token,
                audience=RUNTIME_AUDIENCE,
                binding=self.binding,
                principal_ref=str(record["principalRef"]),
            )

        self.assertEqual(result["restartedAllocations"], 1)
        self.assertEqual(result["replacementAllocationIds"], ["alloc-new"])
        api.request.assert_any_call("POST", "/v1/allocation/alloc-old/stop", None)
        wait_for_replacement.assert_called_once_with(
            api,
            str(record["jobSlug"]),
            {"alloc-old"},
            min_running=1,
        )
        restore_routes.assert_called_once()
        self.assertIs(restore_routes.call_args.args[1], bound_spec)

        checkpointed = dict(
            load_state()["laeRuntime"]["deployments"][runtime_ref]
        )
        self.assertEqual(checkpointed["restartAllocationIds"], ["alloc-old"])
        retry_api = MagicMock()
        retry_api.request.return_value = [
            {"ID": "alloc-old", "ClientStatus": "complete"},
            {"ID": "alloc-new", "ClientStatus": "running"},
        ]
        with (
            patch.object(control_server, "NomadApi", return_value=retry_api),
            patch.object(
                control_server,
                "_lae_runtime_verified_job",
                return_value={"ID": record["jobSlug"]},
            ),
            patch.object(
                control_server,
                "_lae_runtime_bound_compose_spec",
                return_value=bound_spec,
            ),
            patch.object(
                control_server,
                "_wait_for_nomad_job_replacement",
                return_value=["alloc-new"],
            ) as retry_wait,
            patch.object(control_server, "_lae_runtime_restore_routes"),
        ):
            retried = control_server._execute_lae_runtime_lifecycle(
                "restart",
                checkpointed,
                token=self.runtime_token,
                audience=RUNTIME_AUDIENCE,
                binding=self.binding,
                principal_ref=str(record["principalRef"]),
            )
        self.assertEqual(retried["replacementAllocationIds"], ["alloc-new"])
        self.assertFalse(
            any(call.args[0] == "POST" for call in retry_api.request.call_args_list)
        )
        retry_wait.assert_called_once_with(
            retry_api,
            str(record["jobSlug"]),
            {"alloc-old"},
            min_running=1,
        )

    def test_rollback_reverts_to_exact_saved_runtime_and_switches_active_binding(self) -> None:
        target_binding = RuntimeBinding(
            tenant_ref=self.binding.tenant_ref,
            application_ref=self.binding.application_ref,
            operation_ref=self.binding.operation_ref,
            revision_ref="rev-runtime-target",
            deployment_ref="dep-runtime-target",
        )
        target_volume = control_server.handle_lae_runtime_volume_prepare(
            self.runtime_token,
            RUNTIME_AUDIENCE,
            target_binding,
            self.volume_body(),
            idempotency_key="rollback-volume-target",
        )["volumes"][0]["volumeRef"]
        with patch.object(
            control_server,
            "_execute_lae_runtime_deployment",
            side_effect=self.fake_execution,
        ):
            target = control_server.handle_lae_runtime_deployment_create(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                target_binding,
                self.deployment_body(
                    volume_ref=str(target_volume), binding=target_binding
                ),
                idempotency_key="rollback-deploy-target",
            )
        target_ref = str(target["deployment"]["deploymentRef"])
        state = load_state()
        state["laeRuntime"]["deployments"][target_ref].update(
            {"status": "running", "nomadVersion": 3}
        )
        save_state(state)

        current_volume = control_server.handle_lae_runtime_volume_prepare(
            self.runtime_token,
            RUNTIME_AUDIENCE,
            self.binding,
            self.volume_body(existing_ref=str(target_volume)),
            idempotency_key="rollback-volume-current",
        )["volumes"][0]["volumeRef"]
        with patch.object(
            control_server,
            "_execute_lae_runtime_deployment",
            side_effect=self.fake_execution,
        ):
            current = control_server.handle_lae_runtime_deployment_create(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                self.deployment_body(volume_ref=str(current_volume)),
                idempotency_key="rollback-deploy-current",
            )
        current_ref = str(current["deployment"]["deploymentRef"])
        state = load_state()
        state["laeRuntime"]["deployments"][current_ref].update(
            {"status": "running", "nomadVersion": 4}
        )
        foreign_ref = "lae-run-foreign-target"
        state["laeRuntime"]["deployments"][foreign_ref] = {
            **state["laeRuntime"]["deployments"][target_ref],
            "runtimeDeploymentRef": foreign_ref,
            "tenantRef": "tenant-foreign",
        }
        save_state(state)

        target_body = {
            "schemaVersion": "luma.lae-runtime/v1",
            "target": {
                "runtimeDeploymentRef": target_ref,
                "operationRef": target_binding.operation_ref,
                "revisionRef": target_binding.revision_ref,
                "deploymentRef": target_binding.deployment_ref,
            },
        }
        foreign_body = {
            **target_body,
            "target": {
                **target_body["target"],
                "runtimeDeploymentRef": foreign_ref,
            },
        }
        with patch.object(
            control_server,
            "_execute_lae_runtime_rollback",
            return_value={"nomadVersion": 5},
        ) as execute:
            with TestClient(create_app()) as client:
                foreign = client.post(
                    f"/v1/lae/runtime/deployments/{current_ref}/rollback",
                    json=foreign_body,
                    headers=self.headers(idempotency_key="rollback-foreign"),
                )
                rolled_back = client.post(
                    f"/v1/lae/runtime/deployments/{current_ref}/rollback",
                    json=target_body,
                    headers=self.headers(idempotency_key="rollback-1"),
                )
                replay = client.post(
                    f"/v1/lae/runtime/deployments/{current_ref}/rollback",
                    json=target_body,
                    headers=self.headers(idempotency_key="rollback-1"),
                )

        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.assertEqual(rolled_back.status_code, 200, rolled_back.text)
        self.assertEqual(
            rolled_back.json()["deployment"]["deploymentRef"], target_ref
        )
        self.assertEqual(rolled_back.json()["deployment"]["status"], "deploying")
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(execute.call_count, 1)
        state = load_state()
        runtime = state["laeRuntime"]
        self.assertEqual(runtime["deployments"][current_ref]["status"], "superseded")
        self.assertEqual(runtime["deployments"][target_ref]["nomadVersion"], 5)
        application = next(iter(runtime["applicationBindings"].values()))
        self.assertEqual(application["currentRuntimeDeploymentRef"], target_ref)
        rebound_volume = runtime["volumes"][str(target_volume)]
        self.assertEqual(rebound_volume["revisionRef"], target_binding.revision_ref)
        self.assertEqual(
            rebound_volume["deploymentRef"], target_binding.deployment_ref
        )
        rebound_spec = control_server._lae_runtime_bound_compose_spec(
            state, runtime["deployments"][target_ref]
        )
        self.assertEqual(rebound_spec.slug, application["jobSlug"])
        self.assertTrue(
            all(
                owner["runtimeDeploymentRef"] == target_ref
                for owner in runtime["hostnameBindings"].values()
            )
        )

    def test_runtime_rollback_reverts_saved_nomad_version_and_repairs_routes_on_retry(self) -> None:
        current = {
            "tenantRef": self.binding.tenant_ref,
            "applicationRef": self.binding.application_ref,
            "operationRef": self.binding.operation_ref,
            "revisionRef": self.binding.revision_ref,
            "deploymentRef": self.binding.deployment_ref,
            "runtimeDeploymentRef": "lae-run-current",
            "manifestDigest": "sha256:" + "a" * 64,
            "jobSlug": control_server._lae_runtime_job_slug(self.binding),
        }
        target = {
            **current,
            "operationRef": "op-runtime-target",
            "revisionRef": "rev-runtime-target",
            "deploymentRef": "dep-runtime-target",
            "runtimeDeploymentRef": "lae-run-target",
            "manifestDigest": "sha256:" + "b" * 64,
            "nomadVersion": 3,
        }
        current_spec = object()
        target_spec = object()
        with (
            patch.object(
                control_server.NomadApi,
                "request",
                return_value={
                    "Meta": control_server._lae_runtime_expected_job_meta(current)
                },
            ),
            patch.object(
                control_server,
                "_lae_runtime_bound_compose_spec",
                side_effect=[current_spec, target_spec],
            ),
            patch.object(control_server, "_lae_runtime_delete_routes") as delete,
            patch.object(control_server, "revert_job") as revert,
            patch.object(control_server, "_lae_runtime_restore_routes") as restore,
            patch.object(
                control_server,
                "_lae_runtime_nomad_job_version",
                return_value=5,
            ),
        ):
            result = control_server._execute_lae_runtime_rollback(current, target)

        self.assertEqual(result, {"nomadVersion": 5})
        self.assertIs(delete.call_args.args[1], current_spec)
        self.assertEqual(revert.call_args.kwargs["slug"], current["jobSlug"])
        self.assertEqual(revert.call_args.kwargs["version"], 3)
        self.assertIs(restore.call_args.args[1], target_spec)

        # Nomad may already expose the target metadata after a crash. The
        # retry must skip a second revert but still restore route publication.
        with (
            patch.object(
                control_server.NomadApi,
                "request",
                return_value={
                    "Meta": control_server._lae_runtime_expected_job_meta(target)
                },
            ),
            patch.object(
                control_server,
                "_lae_runtime_bound_compose_spec",
                return_value=target_spec,
            ),
            patch.object(control_server, "_lae_runtime_delete_routes") as delete,
            patch.object(control_server, "revert_job") as revert,
            patch.object(control_server, "_lae_runtime_restore_routes") as restore,
            patch.object(
                control_server,
                "_lae_runtime_nomad_job_version",
                return_value=5,
            ),
        ):
            replay = control_server._execute_lae_runtime_rollback(current, target)

        self.assertEqual(replay, {"nomadVersion": 5})
        delete.assert_not_called()
        revert.assert_not_called()
        self.assertIs(restore.call_args.args[1], target_spec)

    def test_observability_is_service_bound_bounded_and_redacts_runtime_secrets(self) -> None:
        canary = "runtime-log-secret-canary"
        secret_ref = self.issue_secret(
            "postgres", "POSTGRES_PASSWORD", canary
        )
        volume_ref = self.prepare_volume()
        body = self.deployment_body(
            volume_ref=volume_ref,
            secret_refs={("postgres", "POSTGRES_PASSWORD"): secret_ref},
        )
        with patch.object(
            control_server,
            "_execute_lae_runtime_deployment",
            side_effect=self.fake_execution,
        ):
            deployed = control_server.handle_lae_runtime_deployment_create(
                self.runtime_token,
                RUNTIME_AUDIENCE,
                self.binding,
                body,
                idempotency_key="deploy-observability",
            )
        runtime_ref = str(deployed["deployment"]["deploymentRef"])
        state = load_state()
        record = state["laeRuntime"]["deployments"][runtime_ref]
        task_name = record["taskNames"]["postgres"]
        record["status"] = "running"
        record["variablePaths"] = [
            f"nomad/jobs/{record['jobSlug']}/group/{task_name}"
        ]
        save_state(state)

        metric_series = {
            "cpuPercent": [[100, 1.25]],
            "memoryUsageBytes": [[100, 4096]],
            "unexpected": [[100, 1]],
        }
        with (
            patch.object(
                control_server,
                "_lae_runtime_verified_job",
                return_value={"Meta": {}},
            ),
            patch.object(
                control_server.NomadApi,
                "get_variable",
                return_value={"POSTGRES_PASSWORD": canary},
            ),
            patch.object(
                control_server,
                "_nomad_log_lines",
                return_value=[
                    "plain line",
                    canary,
                    "Authorization: Bearer bearer-canary",
                    "API_TOKEN=generic-canary",
                    "lsec_12345678",
                ],
            ) as log_lines,
            patch.object(
                control_server,
                "load_history",
                return_value=metric_series,
            ) as history,
            TestClient(create_app()) as client,
        ):
            logs = client.get(
                f"/v1/lae/runtime/deployments/{runtime_ref}/services/postgres/logs?tail=5",
                headers=self.headers(),
            )
            metrics = client.get(
                f"/v1/lae/runtime/deployments/{runtime_ref}/services/postgres/metrics?window=604800",
                headers=self.headers(),
            )
            too_many = client.get(
                f"/v1/lae/runtime/deployments/{runtime_ref}/services/postgres/logs?tail=501",
                headers=self.headers(),
            )
            unknown = client.get(
                f"/v1/lae/runtime/deployments/{runtime_ref}/services/arbitrary/logs?tail=5",
                headers=self.headers(),
            )
            management = client.get(
                f"/v1/lae/runtime/deployments/{runtime_ref}/services/postgres/metrics?window=60",
                headers=self.headers(token=self.management_token),
            )
        self.assertEqual(logs.status_code, 200, logs.text)
        self.assertEqual(logs.headers["cache-control"], "no-store")
        serialized_logs = json.dumps(logs.json())
        for forbidden in (
            canary,
            "bearer-canary",
            "generic-canary",
            "lsec_12345678",
            str(record["jobSlug"]),
            task_name,
        ):
            self.assertNotIn(forbidden, serialized_logs)
        self.assertEqual(logs.json()["lumaName"], "lae-runtime-app")
        self.assertEqual(logs.json()["serviceKey"], "postgres")
        self.assertEqual(log_lines.call_args.kwargs["bound_job_id"], record["jobSlug"])
        self.assertEqual(log_lines.call_args.kwargs["bound_task_name"], task_name)
        self.assertEqual(metrics.status_code, 200, metrics.text)
        self.assertEqual(
            set(metrics.json()["series"]),
            {"cpuPercent", "memoryUsageBytes"},
        )
        self.assertEqual(metrics.json()["windowSeconds"], 604800)
        history.assert_called_once_with(
            "service",
            f"{record['jobSlug']}_{task_name}",
            window=604800,
        )
        self.assertEqual(too_many.status_code, 422)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(management.status_code, 401)

    def test_sync_http_route_issues_secret_and_never_accepts_management_token(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), ControlHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1/lae/runtime/secrets:issue"
            payload = json.dumps(
                {
                    "schemaVersion": "luma.lae-runtime/v1",
                    "serviceKey": "web",
                    "name": "APP_SECRET",
                    "plaintext": "sync-secret-canary",
                    "environmentVersion": 3,
                    "ttlSeconds": 60,
                }
            ).encode()
            request = urllib.request.Request(
                url,
                data=payload,
                method="POST",
                headers={
                    **self.headers(idempotency_key="sync-secret"),
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                value = json.loads(response.read())
                self.assertEqual(response.status, 201)
                self.assertTrue(value["secret"]["secretRef"].startswith("lsec_"))
            request = urllib.request.Request(
                url,
                data=payload,
                method="POST",
                headers={
                    **self.headers(
                        token=self.management_token,
                        idempotency_key="sync-management",
                    ),
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(caught.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
