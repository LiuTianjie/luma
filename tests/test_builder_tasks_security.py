import copy
import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.builder_tasks import builder_plan_content_digest, builder_plan_signature_payload
from luma.control import server as control_server
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError


SNAPSHOT_DIGEST_A = "sha256:" + ("a" * 64)
SNAPSHOT_DIGEST_B = "sha256:" + ("b" * 64)
RUNNER_IMAGE = "registry.internal/infra/lae-agent-runner@sha256:" + ("c" * 64)
SECRET_SENTINEL = "lae-secret-sentinel-must-never-be-persisted"


class BuilderTaskSecurityTests(unittest.TestCase):
    """Security and state-machine contract for the scoped LAE Builder Task API.

    These tests deliberately exercise handlers instead of the eventual HTTP
    transport so the durable task invariants remain covered independently of
    Starlette routing. The public wire body is strict; builder selection and
    idempotency are trusted server/header inputs, never body fields.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "luma.yaml"
        self.config_path.write_text("providers: {}\n", encoding="utf-8")
        self.service_token = "lae-service-token-for-tests"
        self.plan_signing_secret = b"lae-builder-test-plan-signing-secret-32-bytes"
        self.env = patch.dict(
            os.environ,
            {
                "LUMA_CONTROL_STATE_DIR": str(self.state_dir),
                "LUMA_CONTROL_CONFIG": str(self.config_path),
                "LUMA_LAE_SERVICE_TOKEN": self.service_token,
                "LUMA_LAE_PLAN_SIGNING_KEYS_JSON": json.dumps(
                    {"lae-plan-test": self.plan_signing_secret.decode("ascii")}
                ),
                "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": RUNNER_IMAGE,
                "LUMA_LAE_BUILDER_ALLOW_ANONYMOUS_REGISTRY": "1",
                "LUMA_LAE_BUILDER_REGISTRY_INSECURE": "1",
                "LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON": json.dumps(["docker.io"]),
            },
            clear=False,
        )
        self.env.start()

        state = init_state(domain="luma.example.com", cluster_id="luma-builder-test", overwrite=True)
        self.management_token = state["deployToken"]
        state["nodes"] = {
            "builder": {
                "name": "builder",
                "region": "home",
                "tailscaleIP": "100.66.177.70",
                "swarmNodeId": "builder-node-id",
                "agent": {
                    "status": "ready",
                    "os": "linux",
                    "capabilities": ["docker-build", "builder-task-v1"],
                },
            }
        }
        state["build"] = {
            "nodes": ["builder"],
            "defaultNode": "builder",
            "registryHost": "100.66.177.70:5000",
            "pushHost": "localhost:5000",
        }
        fixture_build_request = self._build_body()
        fixture_snapshot_scope = control_server._builder_source_snapshot_scope(
            "lae-service",
            "tenant-builder-test",
            "application-builder-test",
            "snapshot-builder-test",
        )
        state["builderSourceSnapshots"] = {
            fixture_snapshot_scope: {
                "id": "snapshot-builder-test",
                "digest": SNAPSHOT_DIGEST_A,
                "sourceTreeDigest": SNAPSHOT_DIGEST_A,
                "resolvedCommit": "d" * 40,
                "buildPlanDigest": builder_plan_content_digest(fixture_build_request),
                "deploymentPlanDigest": "sha256:" + ("e" * 64),
                "evidenceDigest": "sha256:" + ("f" * 64),
                "policyVersion": "2026-07-11",
                "agentImageDigest": RUNNER_IMAGE,
                "tenantRef": "tenant-builder-test",
                "applicationRef": "application-builder-test",
                "principalRef": "lae-service",
                "builderTaskId": "builder-analysis-fixture",
                "createdAt": 1,
            }
        }
        save_state(state)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def _task(result):
        if not isinstance(result, dict):
            raise AssertionError(f"builder handler returned a non-object: {result!r}")
        task = result.get("task", result)
        if not isinstance(task, dict):
            raise AssertionError(f"builder handler did not return a task object: {result!r}")
        return task

    @staticmethod
    def _limits():
        return {
            "cpu": 2,
            "memoryMiB": 2048,
            "diskMiB": 4096,
            "timeoutSeconds": 300,
        }

    def _analyze_body(self, *, operation_id="op-analyze-1", ref="main"):
        return {
            "schemaVersion": "luma.builder-task/v1",
            "kind": "analyze-source",
            "externalOperationId": operation_id,
            "tenantRef": "tenant-builder-test",
            "applicationRef": "application-builder-test",
            "payload": {
                "sourceRef": {
                    "repository": "https://github.com/acme/app.git",
                    "ref": ref,
                    "subdirectory": "",
                },
                "credentialLeaseId": "credential-lease-analyze-1",
                "agentImageDigest": RUNNER_IMAGE,
                "policyVersion": "2026-07-11",
                "limits": self._limits(),
            },
        }

    def _build_body(self, *, operation_id="op-build-1", snapshot_digest=SNAPSHOT_DIGEST_A):
        body = {
            "schemaVersion": "luma.builder-task/v1",
            "kind": "build-plan",
            "externalOperationId": operation_id,
            "tenantRef": "tenant-builder-test",
            "applicationRef": "application-builder-test",
            "payload": {
                "sourceSnapshotId": "snapshot-builder-test",
                "sourceSnapshotDigest": snapshot_digest,
                "signedBuildPlan": {
                    "schemaVersion": "lae.build-plan/v1",
                    "sourceSnapshotDigest": snapshot_digest,
                    "resolvedCommit": "d" * 40,
                    "policyVersion": "2026-07-11",
                    "builds": [
                        {
                            "key": "web",
                            "context": ".",
                            "dockerfile": "Dockerfile",
                            "target": "runtime",
                            "platform": "linux/amd64",
                            "buildArgNames": [],
                            "secretMountNames": [],
                            "dependsOnBuilds": [],
                        }
                    ],
                    "externalImages": [],
                    "signature": {
                        "keyId": "lae-plan-test",
                        "value": "unsigned_placeholder",
                    },
                },
                "credentialLeaseId": "credential-lease-build-1",
                "limits": self._limits(),
            },
        }
        signature = hmac.new(
            self.plan_signing_secret,
            builder_plan_signature_payload(body),
            hashlib.sha256,
        ).digest()
        body["payload"]["signedBuildPlan"]["signature"]["value"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        return body

    def _create(self, body, *, idempotency_key):
        return control_server.handle_builder_task_create(
            self.service_token,
            body,
            idempotency_key=idempotency_key,
        )

    def _get(self, task_id):
        return self._task(control_server.handle_builder_task_get(self.service_token, task_id))

    def _state_text(self):
        return (self.state_dir / "control.json").read_text(encoding="utf-8")

    def test_scoped_service_token_is_required_and_management_token_is_rejected(self):
        body = self._analyze_body()

        with self.assertRaisesRegex(LumaError, "unauthorized"):
            control_server.handle_builder_task_create(
                self.management_token,
                body,
                idempotency_key="idem-management-must-fail",
            )

        created = self._task(self._create(body, idempotency_key="idem-service-works"))
        self.assertEqual(created["kind"], "analyze-source")
        self.assertEqual(created["status"], "queued")

        for operation in (
            lambda: control_server.handle_builder_task_get(self.management_token, created["id"]),
            lambda: control_server.handle_builder_task_events(self.management_token, created["id"]),
            lambda: control_server.handle_builder_task_cancel(self.management_token, created["id"]),
        ):
            with self.assertRaisesRegex(LumaError, "unauthorized"):
                operation()

    def test_only_analyze_source_and_build_plan_are_accepted(self):
        analyze = self._task(self._create(self._analyze_body(), idempotency_key="idem-kind-analyze"))
        build = self._task(self._create(self._build_body(), idempotency_key="idem-kind-build"))

        self.assertEqual(analyze["kind"], "analyze-source")
        self.assertEqual(build["kind"], "build-plan")

        for kind in ("build-image", "shell", "command", "", "ANALYZE-SOURCE"):
            body = self._analyze_body(operation_id=f"op-invalid-{kind or 'empty'}")
            body["kind"] = kind
            with self.subTest(kind=kind), self.assertRaises(LumaError):
                self._create(body, idempotency_key=f"idem-invalid-kind-{kind or 'empty'}")

    def test_uploaded_object_source_is_accepted_without_url_or_storage_key(self):
        body = self._analyze_body(operation_id="op-object-source")
        body["payload"]["sourceRef"] = {
            "kind": "object",
            "digest": "sha256:" + "9" * 64,
            "mediaType": "application/zip",
            "sizeBytes": 4096,
        }
        created = self._task(
            self._create(body, idempotency_key="idem-object-source")
        )
        self.assertEqual(created["kind"], "analyze-source")
        state_text = self._state_text()
        self.assertNotIn("objectUrl", state_text)
        self.assertNotIn("objectKey", state_text)

        for field, value in (
            ("url", "https://objects.example.test/secret"),
            ("objectKey", "tenants/private/source.zip"),
            ("repository", "https://attacker.invalid/source.git"),
        ):
            invalid = self._analyze_body(operation_id=f"op-object-{field}")
            invalid["payload"]["sourceRef"] = dict(body["payload"]["sourceRef"])
            invalid["payload"]["sourceRef"][field] = value
            with self.subTest(field=field), self.assertRaises(LumaError):
                self._create(invalid, idempotency_key=f"idem-object-{field}")

    def test_analyzer_image_must_match_the_control_allowlist(self):
        body = self._analyze_body(operation_id="op-untrusted-agent-image")
        body["payload"]["agentImageDigest"] = "attacker.example/runner@sha256:" + ("9" * 64)
        with self.assertRaisesRegex(LumaError, "not allowlisted"):
            self._create(body, idempotency_key="idem-untrusted-agent-image")
        self.assertFalse(load_state().get("builderTasks"))

    def test_build_registry_scope_is_derived_only_for_the_ephemeral_node_lease(self):
        created = self._task(self._create(self._build_body(), idempotency_key="idem-build-registry-lease"))
        state = load_state()
        child = state["agentTasks"][state["builderTasks"][created["id"]]["agentTaskId"]]
        self.assertNotIn("registry", child["payload"])
        self.assertNotIn("principalRef", child["payload"])

        issued = control_server.handle_node_agent_token(
            self.management_token,
            {"nodeName": "builder", "nodeId": "builder-node-id"},
        )
        leased = control_server.handle_node_agent_lease(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "os": "linux",
                "capabilities": ["docker-build", "builder-build-v1"],
                "waitSeconds": 0,
            },
        )["task"]
        self.assertIsNotNone(leased)
        self.assertEqual(leased["payload"]["principalRef"], "lae-service")
        self.assertEqual(leased["payload"]["registry"]["authMode"], "anonymous")
        self.assertEqual(set(leased["payload"]["registry"]["repositories"]), {"web"})
        self.assertEqual(leased["payload"]["registry"]["externalRegistries"], ["docker.io"])
        persisted = load_state()["agentTasks"][leased["id"]]["payload"]
        self.assertNotIn("registry", persisted)
        self.assertNotIn("principalRef", persisted)

    def test_external_image_is_plan_bound_and_control_allowlisted(self):
        body = self._build_body(operation_id="op-external-image")
        plan = body["payload"]["signedBuildPlan"]
        plan["builds"] = []
        plan["externalImages"] = [
            {
                "key": "database",
                "ref": "postgres:17",
                "resolvedDigest": "sha256:" + ("7" * 64),
                "platform": "linux/amd64",
            }
        ]
        signature = hmac.new(
            self.plan_signing_secret,
            builder_plan_signature_payload(body),
            hashlib.sha256,
        ).digest()
        plan["signature"]["value"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

        state = load_state()
        snapshot_scope = control_server._builder_source_snapshot_scope(
            "lae-service",
            body["tenantRef"],
            body["applicationRef"],
            body["payload"]["sourceSnapshotId"],
        )
        state["builderSourceSnapshots"][snapshot_scope]["buildPlanDigest"] = (
            builder_plan_content_digest(body)
        )
        save_state(state)
        created = self._task(
            self._create(body, idempotency_key="idem-external-image")
        )

        issued = control_server.handle_node_agent_token(
            self.management_token,
            {"nodeName": "builder", "nodeId": "builder-node-id"},
        )
        leased = control_server.handle_node_agent_lease(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "os": "linux",
                "capabilities": ["docker-build", "builder-build-v1"],
                "waitSeconds": 0,
            },
        )["task"]
        self.assertEqual(leased["payload"]["registry"]["repositories"], {})
        self.assertEqual(
            leased["payload"]["registry"]["externalRegistries"], ["docker.io"]
        )
        self.assertEqual(created["status"], "queued")

        changed = copy.deepcopy(body)
        changed["payload"]["signedBuildPlan"]["externalImages"][0]["ref"] = (
            "postgres:17-alpine"
        )
        with self.assertRaisesRegex(LumaError, "signature verification failed"):
            control_server._verify_lae_plan_signature(changed)

        denied = copy.deepcopy(body)
        denied["payload"]["signedBuildPlan"]["externalImages"][0]["ref"] = (
            "ghcr.io/acme/postgres:17"
        )
        signature = hmac.new(
            self.plan_signing_secret,
            builder_plan_signature_payload(denied),
            hashlib.sha256,
        ).digest()
        denied["payload"]["signedBuildPlan"]["signature"]["value"] = (
            base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )
        with self.assertRaisesRegex(LumaError, "not allowlisted"):
            control_server._builder_build_registry_lease(
                load_state(), denied, {"id": "lae-service"}
            )

    def test_analyze_only_builder_cannot_receive_build_plan(self):
        state = load_state()
        state["nodes"]["builder"]["agent"]["capabilities"] = ["docker-build", "builder-analyze-v1"]
        save_state(state)

        analyze = self._task(self._create(self._analyze_body(), idempotency_key="idem-specific-analyze"))
        self.assertEqual(analyze["kind"], "analyze-source")
        with self.assertRaisesRegex(LumaError, "builder-build-v1"):
            self._create(self._build_body(), idempotency_key="idem-specific-build")

    def test_unknown_or_privileged_top_level_fields_are_rejected(self):
        forbidden = {
            "action": "update-luma",
            "command": "sh -c id",
            "script": "rm -rf /",
            "buildNode": "arbitrary-node",
            "idempotencyKey": "must-come-from-header",
            "unexpected": True,
        }

        for field, value in forbidden.items():
            body = self._analyze_body(operation_id=f"op-forbidden-{field}")
            body[field] = value
            with self.subTest(field=field), self.assertRaises(LumaError):
                self._create(body, idempotency_key=f"idem-forbidden-{field}")

        state = load_state()
        self.assertFalse(state.get("builderTasks"))
        self.assertFalse(state.get("agentTasks"))

    def test_recursive_inline_credentials_are_rejected_without_persisting_sentinel(self):
        credential_shapes = [
            {"token": SECRET_SENTINEL},
            {"nested": {"password": SECRET_SENTINEL}},
            {"nested": [{"secret": SECRET_SENTINEL}]},
            {"registryAuth": {"username": "user", "password": SECRET_SENTINEL}},
        ]

        for index, injected in enumerate(credential_shapes):
            body = self._analyze_body(operation_id=f"op-secret-{index}")
            body["payload"]["sourceRef"]["inlineAuth"] = injected
            with self.subTest(shape=injected), self.assertRaises(LumaError):
                self._create(body, idempotency_key=f"idem-secret-{index}")
            self.assertNotIn(SECRET_SENTINEL, self._state_text())

        state = load_state()
        self.assertFalse(state.get("builderTasks"))
        self.assertFalse(state.get("agentTasks"))

    def test_same_idempotency_key_and_body_reuses_one_parent_and_child_task(self):
        body = self._analyze_body()
        first = self._task(self._create(body, idempotency_key="idem-reuse"))
        second = self._task(self._create(copy.deepcopy(body), idempotency_key="idem-reuse"))

        self.assertEqual(first["id"], second["id"])
        state = load_state()
        self.assertEqual(len(state.get("builderTasks") or {}), 1)
        self.assertEqual(len(state.get("agentTasks") or {}), 1)
        self.assertEqual(len(state.get("builderTaskIdempotency") or {}), 1)

    def test_same_idempotency_key_with_different_body_is_rejected(self):
        first_body = self._analyze_body(ref="main")
        changed_body = self._analyze_body(ref="release")
        first = self._task(self._create(first_body, idempotency_key="idem-conflict"))

        with self.assertRaisesRegex(LumaError, "(?i)idempotency"):
            self._create(changed_body, idempotency_key="idem-conflict")

        state = load_state()
        self.assertEqual(len(state.get("builderTasks") or {}), 1)
        self.assertEqual(len(state.get("agentTasks") or {}), 1)
        self.assertEqual(self._get(first["id"])["id"], first["id"])

    def test_service_principal_scopes_ownership_tenant_and_idempotency(self):
        principals = {
            "lae-a": {
                "token": "lae-principal-a-token",
                "tenantRefs": ["tenant-builder-test"],
                "applicationRefs": ["application-builder-test"],
            },
            "lae-b": {
                "token": "lae-principal-b-token",
                "tenantRefs": ["tenant-builder-test"],
                "applicationRefs": ["application-builder-test"],
            },
        }
        with patch.dict(
            os.environ,
            {"LUMA_LAE_SERVICE_PRINCIPALS_JSON": json.dumps(principals)},
            clear=False,
        ):
            body = self._analyze_body(operation_id="op-principal-scope")
            first = self._task(
                control_server.handle_builder_task_create(
                    "lae-principal-a-token",
                    body,
                    idempotency_key="same-key-across-principals",
                )
            )
            second = self._task(
                control_server.handle_builder_task_create(
                    "lae-principal-b-token",
                    body,
                    idempotency_key="same-key-across-principals",
                )
            )
            self.assertNotEqual(first["id"], second["id"])

            for operation in (
                lambda: control_server.handle_builder_task_get("lae-principal-b-token", first["id"]),
                lambda: control_server.handle_builder_task_events("lae-principal-b-token", first["id"]),
                lambda: control_server.handle_builder_task_cancel("lae-principal-b-token", first["id"]),
            ):
                with self.assertRaisesRegex(LumaError, "not found"):
                    operation()

            outside_scope = self._analyze_body(operation_id="op-outside-principal-scope")
            outside_scope["tenantRef"] = "tenant-other"
            with self.assertRaisesRegex(LumaError, "unauthorized"):
                control_server.handle_builder_task_create(
                    "lae-principal-a-token",
                    outside_scope,
                    idempotency_key="outside-principal-scope",
                )

        state = load_state()
        self.assertEqual(len(state.get("builderTasks") or {}), 2)
        self.assertEqual(len(state.get("builderTaskIdempotency") or {}), 2)
        self.assertTrue(all(str(key).startswith("sha256:") for key in state["builderTaskIdempotency"]))

    def test_concurrent_identical_creates_are_atomic_and_reuse_one_task(self):
        body = self._analyze_body()
        barrier = threading.Barrier(8)
        task_ids = []
        errors = []

        def create_once():
            try:
                barrier.wait(timeout=3)
                created = self._task(self._create(copy.deepcopy(body), idempotency_key="idem-race"))
                task_ids.append(created["id"])
            except Exception as exc:  # collected and asserted in the main thread
                errors.append(exc)

        threads = [threading.Thread(target=create_once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertEqual(len(task_ids), 8)
        self.assertEqual(len(set(task_ids)), 1)
        state = load_state()
        self.assertEqual(len(state.get("builderTasks") or {}), 1)
        self.assertEqual(len(state.get("agentTasks") or {}), 1)

    def test_queued_cancel_is_idempotent_and_prevents_agent_lease(self):
        created = self._task(self._create(self._analyze_body(), idempotency_key="idem-cancel-queued"))

        first_cancel = self._task(control_server.handle_builder_task_cancel(self.service_token, created["id"]))
        second_cancel = self._task(control_server.handle_builder_task_cancel(self.service_token, created["id"]))
        self.assertEqual(first_cancel["id"], created["id"])
        self.assertEqual(first_cancel["status"], "canceled")
        self.assertEqual(second_cancel["status"], "canceled")

        issued = control_server.handle_node_agent_token(
            self.management_token,
            {"nodeName": "builder", "nodeId": "builder-node-id"},
        )
        leased = control_server.handle_node_agent_lease(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "os": "linux",
                "capabilities": ["docker-build", "builder-task-v1"],
                "waitSeconds": 0,
            },
        )["task"]
        self.assertIsNone(leased)
        self.assertTrue(all(task.get("status") == "canceled" for task in (load_state().get("agentTasks") or {}).values()))

    def test_late_success_cannot_revive_a_cancel_requested_task(self):
        created = self._task(self._create(self._analyze_body(), idempotency_key="idem-cancel-running"))
        issued = control_server.handle_node_agent_token(
            self.management_token,
            {"nodeName": "builder", "nodeId": "builder-node-id"},
        )
        leased = control_server.handle_node_agent_lease(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "os": "linux",
                "capabilities": ["docker-build", "builder-task-v1"],
                "waitSeconds": 0,
            },
        )["task"]
        self.assertIsNotNone(leased)

        canceled = self._task(control_server.handle_builder_task_cancel(self.service_token, created["id"]))
        self.assertEqual(canceled["status"], "cancel_requested")
        heartbeat = control_server.handle_node_agent_heartbeat(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "os": "linux",
                "capabilities": ["docker-build", "builder-task-v1"],
                "activeTaskId": leased["id"],
            },
        )
        self.assertTrue(heartbeat["cancelRequested"])

        result = {
            "resolvedCommit": "e" * 40,
            "sourceTreeDigest": SNAPSHOT_DIGEST_A,
            "sourceSnapshotId": "snapshot-1",
            "sourceSnapshotDigest": SNAPSHOT_DIGEST_A,
            "deploymentPlanDigest": "sha256:" + ("e" * 64),
            "buildPlanDigest": "sha256:" + ("f" * 64),
            "evidenceDigest": "sha256:" + ("1" * 64),
        }
        try:
            control_server.handle_node_agent_complete(
                issued["agentToken"],
                {
                    "nodeName": "builder",
                    "nodeId": "builder-node-id",
                    "taskId": leased["id"],
                    "status": "succeeded",
                    "message": "late success",
                    "result": result,
                },
            )
        except LumaError:
            # Rejecting a stale terminal callback is also safe. The invariant is
            # that it can never overwrite the cancellation fence.
            pass

        final_task = self._get(created["id"])
        self.assertIn(final_task["status"], {"cancel_requested", "canceled"})
        self.assertNotEqual(final_task["status"], "succeeded")

    def test_events_have_monotonic_seq_and_support_after_and_limit(self):
        created = self._task(self._create(self._analyze_body(), idempotency_key="idem-events"))
        control_server.handle_builder_task_cancel(self.service_token, created["id"])

        all_events = control_server.handle_builder_task_events(
            self.service_token,
            created["id"],
            after=0,
            limit=200,
        )["events"]
        self.assertGreaterEqual(len(all_events), 2)
        seqs = [event["seq"] for event in all_events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertTrue(all(isinstance(seq, int) and seq > 0 for seq in seqs))

        first_page = control_server.handle_builder_task_events(
            self.service_token,
            created["id"],
            after=0,
            limit=1,
        )["events"]
        self.assertEqual(len(first_page), 1)
        remaining = control_server.handle_builder_task_events(
            self.service_token,
            created["id"],
            after=first_page[0]["seq"],
            limit=200,
        )["events"]
        self.assertTrue(remaining)
        self.assertTrue(all(event["seq"] > first_page[0]["seq"] for event in remaining))

    def test_builder_progress_and_completion_never_persist_free_form_agent_output(self):
        created = self._task(self._create(self._analyze_body(), idempotency_key="idem-redacted-output"))
        issued = control_server.handle_node_agent_token(
            self.management_token,
            {"nodeName": "builder", "nodeId": "builder-node-id"},
        )
        leased = control_server.handle_node_agent_lease(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "os": "linux",
                "capabilities": ["docker-build", "builder-task-v1"],
                "waitSeconds": 0,
            },
        )["task"]
        self.assertIsNotNone(leased)

        progress_secret = "github_pat_1234567890abcdefghijklmnop"
        completion_secret = "gitea-super-secret"
        control_server.handle_node_agent_progress(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "taskId": leased["id"],
                "events": [{"type": "output", "line": f"clone using {progress_secret}"}],
            },
        )
        control_server.handle_node_agent_complete(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "taskId": leased["id"],
                "status": "failed",
                "message": f"clone failed with {completion_secret}",
                "result": {},
            },
        )

        state_text = self._state_text()
        self.assertNotIn(progress_secret, state_text)
        self.assertNotIn(completion_secret, state_text)
        messages = [
            event["message"]
            for event in control_server.handle_builder_task_events(
                self.service_token,
                created["id"],
                after=0,
                limit=200,
            )["events"]
        ]
        self.assertIn("[redacted builder output]", messages)
        self.assertEqual(messages[-1], "builder task failed")

    def test_nested_authorization_result_is_rejected_without_persisting_secret(self):
        created = self._task(self._create(self._analyze_body(), idempotency_key="idem-result-secret"))
        issued = control_server.handle_node_agent_token(
            self.management_token,
            {"nodeName": "builder", "nodeId": "builder-node-id"},
        )
        leased = control_server.handle_node_agent_lease(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "os": "linux",
                "capabilities": ["docker-build", "builder-task-v1"],
                "waitSeconds": 0,
            },
        )["task"]
        result_secret = "gitea-super-secret"
        result = {
            "resolvedCommit": "e" * 40,
            "sourceTreeDigest": SNAPSHOT_DIGEST_A,
            "sourceSnapshotId": "snapshot-1",
            "sourceSnapshotDigest": SNAPSHOT_DIGEST_A,
            "deploymentPlanDigest": "sha256:" + ("e" * 64),
            "buildPlanDigest": "sha256:" + ("f" * 64),
            "evidenceDigest": "sha256:" + ("1" * 64),
            "artifacts": {"debug": {"authorization": result_secret}},
        }
        completed = control_server.handle_node_agent_complete(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "taskId": leased["id"],
                "status": "succeeded",
                "message": "must not survive",
                "result": result,
            },
        )

        self.assertEqual(completed["status"], "failed")
        self.assertEqual(self._get(created["id"])["message"], "invalid builder task result")
        self.assertNotIn(result_secret, self._state_text())

    def test_successful_analysis_records_an_immutable_principal_bound_snapshot(self):
        created = self._task(self._create(self._analyze_body(), idempotency_key="idem-valid-analysis"))
        issued = control_server.handle_node_agent_token(
            self.management_token,
            {"nodeName": "builder", "nodeId": "builder-node-id"},
        )
        leased = control_server.handle_node_agent_lease(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "os": "linux",
                "capabilities": ["docker-build", "builder-task-v1"],
                "waitSeconds": 0,
            },
        )["task"]
        self.assertEqual(leased["payload"]["externalRegistries"], ["docker.io"])
        persisted_child = load_state()["agentTasks"][leased["id"]]["payload"]
        self.assertNotIn("externalRegistries", persisted_child)
        deployment_digest = "sha256:" + ("2" * 64)
        build_digest = "sha256:" + ("3" * 64)
        evidence_digest = "sha256:" + ("4" * 64)
        result = {
            "resolvedCommit": "e" * 40,
            "sourceTreeDigest": "sha256:" + ("5" * 64),
            "sourceSnapshotId": "snapshot-valid-analysis",
            "sourceSnapshotDigest": "sha256:" + ("6" * 64),
            "deploymentPlanDigest": deployment_digest,
            "buildPlanDigest": build_digest,
            "evidenceDigest": evidence_digest,
            "policyVersion": "2026-07-11",
            "agentImageDigest": RUNNER_IMAGE,
            "artifacts": {
                "evidence": {
                    "digest": evidence_digest,
                    "mediaType": "application/vnd.lae.evidence+json",
                    "sizeBytes": 101,
                },
                "deploymentPlan": {
                    "digest": deployment_digest,
                    "mediaType": "application/vnd.lae.deployment-plan+json",
                    "sizeBytes": 202,
                },
                "buildPlan": {
                    "digest": build_digest,
                    "mediaType": "application/vnd.lae.build-plan-candidate+json",
                    "sizeBytes": 303,
                },
            },
        }
        completed = control_server.handle_node_agent_complete(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "taskId": leased["id"],
                "status": "succeeded",
                "message": "free-form success must not survive",
                "result": result,
            },
        )

        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(self._get(created["id"])["message"], "builder task succeeded")
        snapshot_scope = control_server._builder_source_snapshot_scope(
            "lae-service",
            "tenant-builder-test",
            "application-builder-test",
            "snapshot-valid-analysis",
        )
        snapshot = load_state()["builderSourceSnapshots"][snapshot_scope]
        self.assertEqual(snapshot["digest"], result["sourceSnapshotDigest"])
        self.assertEqual(snapshot["principalRef"], "lae-service")
        self.assertEqual(snapshot["tenantRef"], "tenant-builder-test")
        self.assertEqual(snapshot["applicationRef"], "application-builder-test")
        self.assertNotIn("free-form success", self._state_text())

    def test_build_plan_requires_snapshot_binding_plan_digest_and_valid_signature(self):
        bad_signature = self._build_body(operation_id="op-bad-signature")
        bad_signature["payload"]["signedBuildPlan"]["signature"]["value"] = "A" * 43
        with self.assertRaisesRegex(LumaError, "signature verification failed"):
            self._create(bad_signature, idempotency_key="idem-bad-signature")

        changed_plan = self._build_body(operation_id="op-changed-plan")
        changed_plan["payload"]["signedBuildPlan"]["builds"][0]["dockerfile"] = "Dockerfile.other"
        signature = hmac.new(
            self.plan_signing_secret,
            builder_plan_signature_payload(changed_plan),
            hashlib.sha256,
        ).digest()
        changed_plan["payload"]["signedBuildPlan"]["signature"]["value"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        with self.assertRaisesRegex(LumaError, "content does not match"):
            self._create(changed_plan, idempotency_key="idem-changed-plan")

        unknown_snapshot = self._build_body(operation_id="op-unknown-snapshot")
        unknown_snapshot["payload"]["sourceSnapshotId"] = "snapshot-does-not-exist"
        signature = hmac.new(
            self.plan_signing_secret,
            builder_plan_signature_payload(unknown_snapshot),
            hashlib.sha256,
        ).digest()
        unknown_snapshot["payload"]["signedBuildPlan"]["signature"]["value"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        with self.assertRaisesRegex(LumaError, "unknown or expired"):
            self._create(unknown_snapshot, idempotency_key="idem-unknown-snapshot")

    def test_build_plan_snapshot_mismatch_is_rejected_before_queue(self):
        body = self._build_body(snapshot_digest=SNAPSHOT_DIGEST_A)
        body["payload"]["signedBuildPlan"]["sourceSnapshotDigest"] = SNAPSHOT_DIGEST_B

        with self.assertRaisesRegex(LumaError, "(?i)snapshot.*digest.*match"):
            self._create(body, idempotency_key="idem-snapshot-mismatch")

        state = load_state()
        self.assertFalse(state.get("builderTasks"))
        self.assertFalse(state.get("agentTasks"))
        self.assertFalse(state.get("builderTaskIdempotency"))

    def test_legacy_build_endpoint_keeps_management_token_and_build_image_contract(self):
        captured = {}

        def fake_run_task(_state, node_name, action, payload, **_kwargs):
            captured.update({"node": node_name, "action": action, "payload": payload})
            return {
                "kind": "service",
                "image": "100.66.177.70:5000/acme/app:abc123",
                "manifest": "name: app\nimage: placeholder\nregion: cn\nexposure: none\n",
            }

        with patch("luma.control.server._run_node_agent_task", side_effect=fake_run_task), patch(
            "luma.control.server.handle_deployment",
            return_value={"service": "app", "steps": []},
        ) as deploy:
            result = control_server.handle_build_deploy(
                self.management_token,
                {"repoUrl": "https://github.com/acme/app", "ref": "main"},
            )

        self.assertEqual(captured["node"], "builder")
        self.assertEqual(captured["action"], "build-image")
        self.assertEqual(captured["payload"]["repoUrl"], "https://github.com/acme/app")
        self.assertEqual(captured["payload"]["ref"], "main")
        self.assertIn("image: 100.66.177.70:5000/acme/app:abc123", deploy.call_args.args[1]["manifest"])
        self.assertEqual(result["image"], "100.66.177.70:5000/acme/app:abc123")

        with self.assertRaisesRegex(LumaError, "unauthorized"):
            control_server.handle_build_deploy(
                self.service_token,
                {"repoUrl": "https://github.com/acme/app"},
            )


if __name__ == "__main__":
    unittest.main()
