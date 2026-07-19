import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from luma import credential_broker
from luma.control import server as control_server
from luma.control.state import init_state, load_state, save_state
from luma.credential_broker import (
    OBJECT_SOURCE_REQUEST_SCHEMA_VERSION,
    OBJECT_SOURCE_RESPONSE_SCHEMA_VERSION,
    ObjectSourceBrokerError,
    ObjectSourceLeaseBinding,
    RedeemedObjectSource,
)


RUNNER_IMAGE = "registry.internal/lae-agent-runner@sha256:" + ("a" * 64)
OBJECT_DIGEST = "sha256:" + ("b" * 64)
OTHER_DIGEST = "sha256:" + ("c" * 64)
BROKER_AUTH_CANARY = "object-broker-auth-canary-must-not-persist"
SIGNED_URL = (
    "https://objects.example.test/private/source.zip"
    "?X-Amz-Signature=object-url-canary"
)
GENERIC_FAILURE = "builder credential lease redemption failed"


class BuilderObjectSourceBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "luma.yaml"
        self.config_path.write_text("providers: {}\n", encoding="utf-8")
        self.service_token = "lae-service-token"
        self.env = patch.dict(
            os.environ,
            {
                "LUMA_CONTROL_STATE_DIR": str(self.state_dir),
                "LUMA_CONTROL_CONFIG": str(self.config_path),
                "LUMA_LAE_SERVICE_TOKEN": self.service_token,
                "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": RUNNER_IMAGE,
            },
            clear=True,
        )
        self.env.start()

        state = init_state(
            domain="luma.example.com",
            cluster_id="luma-object-broker-test",
            overwrite=True,
        )
        self.management_token = state["deployToken"]
        state["nodes"] = {
            "builder": {
                "name": "builder",
                "region": "home",
                "swarmNodeId": "builder-node-id",
                "agent": {
                    "status": "ready",
                    "os": "linux",
                    "capabilities": ["docker-build", "builder-analyze-v1"],
                },
            }
        }
        state["build"] = {"nodes": ["builder"], "defaultNode": "builder"}
        save_state(state)
        self.agent_token = control_server.handle_node_agent_token(
            self.management_token,
            {"nodeName": "builder", "nodeId": "builder-node-id"},
        )["agentToken"]

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def _descriptor(digest=OBJECT_DIGEST):
        return {
            "kind": "object",
            "digest": digest,
            "mediaType": "application/zip",
            "sizeBytes": 4096,
        }

    def _body(self, *, operation_id="operation-object-broker-test"):
        return {
            "schemaVersion": "luma.builder-task/v1",
            "kind": "analyze-source",
            "externalOperationId": operation_id,
            "tenantRef": "tenant-object-broker-test",
            "applicationRef": "application-object-broker-test",
            "payload": {
                "sourceRef": self._descriptor(),
                "credentialLeaseId": "object-lease-broker-test",
                "agentImageDigest": RUNNER_IMAGE,
                "policyVersion": "2026-07-11",
                "limits": {
                    "cpu": 1,
                    "memoryMiB": 512,
                    "diskMiB": 1024,
                    "timeoutSeconds": 120,
                },
            },
        }

    def _create(self, *, operation_id="operation-object-broker-test"):
        return control_server.handle_builder_task_create(
            self.service_token,
            self._body(operation_id=operation_id),
            idempotency_key="idem-" + operation_id,
        )["task"]

    def _lease(self):
        return control_server.handle_node_agent_lease(
            self.agent_token,
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "os": "linux",
                "capabilities": ["docker-build", "builder-analyze-v1"],
                "waitSeconds": 0,
            },
        )["task"]

    @classmethod
    def _binding(cls):
        return ObjectSourceLeaseBinding(
            lease_id="object-lease-broker-test",
            builder_task_id="builder-task-test",
            external_operation_id="operation-object-broker-test",
            principal_ref="lae-service",
            tenant_ref="tenant-object-broker-test",
            application_ref="application-object-broker-test",
            object_descriptor=cls._descriptor(),
        )

    @staticmethod
    def _broker_response(binding, *, expires_at=1_000_060, **overrides):
        request = binding.request_body()
        response = {
            **request,
            "schemaVersion": OBJECT_SOURCE_RESPONSE_SCHEMA_VERSION,
            "method": "GET",
            "expiresAt": expires_at,
            "objectUrl": SIGNED_URL,
            "allowedHost": "objects.example.test",
        }
        response.update(overrides)
        return response

    def _configured_broker(self):
        # The object broker intentionally reuses the existing credential broker
        # token file when no object-specific token path is configured.
        token_file = self.root / "shared-broker.token"
        token_file.write_text(BROKER_AUTH_CANARY + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        return patch.dict(
            os.environ,
            {
                "LUMA_OBJECT_SOURCE_BROKER_URL": (
                    "https://broker.internal/v1/object-source-leases/redeem"
                ),
                "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": str(token_file),
            },
            clear=False,
        )

    def _state_text(self):
        return (self.state_dir / "control.json").read_text(encoding="utf-8")

    def test_protocol_is_exact_bound_short_lived_and_reuses_shared_token_file(self):
        binding = self._binding()
        captured = {}

        def post(config, body):
            captured["url"] = config.url
            captured["token"] = config.bearer_token
            captured["configRepr"] = repr(config)
            captured["body"] = body
            return self._broker_response(binding)

        with self._configured_broker(), patch.object(
            credential_broker,
            "_post_object_source_redemption",
            side_effect=post,
        ):
            result = credential_broker.redeem_builder_object_source(
                binding,
                now=1_000_000,
            )

        self.assertEqual(
            captured["url"],
            "https://broker.internal/v1/object-source-leases/redeem",
        )
        self.assertEqual(captured["token"], BROKER_AUTH_CANARY)
        self.assertEqual(
            captured["body"],
            {
                "schemaVersion": OBJECT_SOURCE_REQUEST_SCHEMA_VERSION,
                "leaseId": "object-lease-broker-test",
                "builderTaskId": "builder-task-test",
                "externalOperationId": "operation-object-broker-test",
                "principalRef": "lae-service",
                "tenantRef": "tenant-object-broker-test",
                "applicationRef": "application-object-broker-test",
                "object": self._descriptor(),
            },
        )
        self.assertEqual(result.object_url, SIGNED_URL)
        self.assertEqual(result.allowed_host, "objects.example.test")
        self.assertEqual(result.expires_at, 1_000_060)
        self.assertNotIn(SIGNED_URL, repr(result))
        self.assertNotIn(BROKER_AUTH_CANARY, captured["configRepr"])
        self.assertNotIn(BROKER_AUTH_CANARY, self._state_text())

    def test_max_ttl_is_validated_at_response_time(self):
        binding = self._binding()
        with self._configured_broker(), patch.object(
            credential_broker,
            "_post_object_source_redemption",
            return_value=self._broker_response(binding, expires_at=1_000_301),
        ), patch.object(
            credential_broker,
            "_unix_time",
            side_effect=(1_000_000, 1_000_001),
        ):
            result = credential_broker.redeem_builder_object_source(binding)

        self.assertEqual(result.expires_at, 1_000_301)

    def test_response_schema_binding_ttl_method_and_host_are_fail_closed(self):
        binding = self._binding()

        def mutate(case):
            response = self._broker_response(binding)
            if case == "schema":
                response["schemaVersion"] = "luma.object-source-redemption-result/v2"
            elif case == "unknown-field":
                response["unexpected"] = "response-canary"
            elif case == "binding":
                response["tenantRef"] = "another-tenant"
            elif case == "descriptor":
                response["object"] = self._descriptor(OTHER_DIGEST)
            elif case == "expired":
                response["expiresAt"] = 1_000_000
            elif case == "ttl":
                response["expiresAt"] = 1_000_301
            elif case == "method":
                response["method"] = "POST"
            elif case == "host":
                response["objectUrl"] = (
                    "https://attacker.example/private/source.zip?secret=url-canary"
                )
            return response

        for case in (
            "schema",
            "unknown-field",
            "binding",
            "descriptor",
            "expired",
            "ttl",
            "method",
            "host",
        ):
            with self.subTest(case=case), self._configured_broker(), patch.object(
                credential_broker,
                "_post_object_source_redemption",
                return_value=mutate(case),
            ), self.assertRaises(ObjectSourceBrokerError) as raised:
                credential_broker.redeem_builder_object_source(
                    binding,
                    now=1_000_000,
                )
            self.assertEqual(
                str(raised.exception),
                "object source broker redemption failed",
            )
            self.assertNotIn("url-canary", str(raised.exception))

    def test_broker_redirect_is_rejected_without_leaking_location(self):
        binding = self._binding()
        redirect_url = "https://redirect.example/private?secret=redirect-canary"
        captured = {}

        class RedirectOpener:
            def open(self, request, timeout):
                captured["authorization"] = request.get_header("Authorization")
                captured["body"] = json.loads(request.data.decode("utf-8"))
                raise urllib.error.HTTPError(
                    request.full_url,
                    307,
                    "redirect",
                    {"Location": redirect_url},
                    None,
                )

        def opener_factory(*handlers):
            captured["redirectHandler"] = any(
                handler.__class__.__name__ == "_RejectRedirects"
                for handler in handlers
            )
            return RedirectOpener()

        with self._configured_broker(), patch.object(
            credential_broker.urllib.request,
            "build_opener",
            side_effect=opener_factory,
        ), self.assertRaises(ObjectSourceBrokerError) as raised:
            credential_broker.redeem_builder_object_source(
                binding,
                now=1_000_000,
            )

        self.assertEqual(
            str(raised.exception),
            "object source broker redemption failed",
        )
        self.assertNotIn("redirect-canary", str(raised.exception))
        self.assertEqual(captured["authorization"], "Bearer " + BROKER_AUTH_CANARY)
        self.assertEqual(captured["body"], binding.request_body())
        self.assertTrue(captured["redirectHandler"])

    def test_object_broker_configuration_is_independent_and_fail_closed(self):
        binding = self._binding()
        with patch.dict(
            os.environ,
            {
                "LUMA_OBJECT_SOURCE_BROKER_URL": (
                    "https://broker.internal/v1/object-source-leases/redeem"
                ),
                "LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE": "",
                "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": "",
            },
            clear=False,
        ), self.assertRaises(ObjectSourceBrokerError):
            credential_broker.redeem_builder_object_source(
                binding,
                now=1_000_000,
            )

        unsafe_token = self.root / "unsafe-object-broker.token"
        unsafe_token.write_text(BROKER_AUTH_CANARY, encoding="utf-8")
        unsafe_token.chmod(0o604)
        with patch.dict(
            os.environ,
            {
                "LUMA_OBJECT_SOURCE_BROKER_URL": (
                    "https://broker.internal/v1/object-source-leases/redeem"
                ),
                "LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE": str(unsafe_token),
                "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": "",
            },
            clear=False,
        ), self.assertRaises(ObjectSourceBrokerError):
            credential_broker.redeem_builder_object_source(
                binding,
                now=1_000_000,
            )

        with patch.dict(
            os.environ,
            {
                "LUMA_OBJECT_SOURCE_BROKER_URL": (
                    "http://broker.internal/v1/object-source-leases/redeem"
                ),
                "LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE": str(unsafe_token),
            },
            clear=False,
        ), self.assertRaises(ObjectSourceBrokerError):
            credential_broker.redeem_builder_object_source(
                binding,
                now=1_000_000,
            )

    def test_object_source_without_broker_fails_parent_and_child_generically(self):
        created = self._create(operation_id="operation-object-no-broker")
        self.assertIsNone(self._lease())

        state = load_state()
        parent = state["builderTasks"][created["id"]]
        child = state["agentTasks"][parent["agentTaskId"]]
        self.assertEqual(parent["status"], "failed")
        self.assertEqual(child["status"], "failed")
        self.assertEqual(parent["message"], GENERIC_FAILURE)
        self.assertEqual(child["message"], GENERIC_FAILURE)

    def test_signed_url_is_only_in_returned_node_lease_and_pollution_is_removed(self):
        created = self._create(operation_id="operation-object-transient")
        state = load_state()
        child_id = state["builderTasks"][created["id"]]["agentTaskId"]
        child_payload = state["agentTasks"][child_id]["payload"]
        child_payload["objectUrl"] = "https://polluted.invalid/?secret=polluted-canary"
        child_payload["objectAllowedHost"] = "polluted.invalid"
        child_payload["sourceRef"] = {
            **self._descriptor(OTHER_DIGEST),
            "objectUrl": "https://nested.invalid/?secret=nested-canary",
            "objectAllowedHost": "nested.invalid",
        }
        save_state(state)
        captured = {}

        def redeem(binding):
            captured["binding"] = binding
            return RedeemedObjectSource(
                expires_at=int(time.time()) + 60,
                allowed_host="objects.example.test",
                object_url=SIGNED_URL,
            )

        with patch.object(
            control_server,
            "redeem_builder_object_source",
            side_effect=redeem,
        ):
            leased = self._lease()

        self.assertIsNotNone(leased)
        self.assertEqual(leased["payload"]["sourceRef"], self._descriptor())
        self.assertEqual(leased["payload"]["objectUrl"], SIGNED_URL)
        self.assertEqual(
            leased["payload"]["objectAllowedHost"],
            "objects.example.test",
        )
        binding = captured["binding"]
        self.assertEqual(binding.builder_task_id, created["id"])
        self.assertEqual(binding.lease_id, "object-lease-broker-test")
        self.assertEqual(binding.external_operation_id, "operation-object-transient")
        self.assertEqual(binding.principal_ref, "lae-service")
        self.assertEqual(binding.tenant_ref, "tenant-object-broker-test")
        self.assertEqual(binding.application_ref, "application-object-broker-test")
        self.assertEqual(dict(binding.object_descriptor), self._descriptor())

        durable = load_state()
        durable_child = durable["agentTasks"][child_id]["payload"]
        self.assertNotIn("objectUrl", durable_child)
        self.assertNotIn("objectAllowedHost", durable_child)
        self.assertNotIn("objectUrl", durable_child["sourceRef"])
        self.assertNotIn("objectAllowedHost", durable_child["sourceRef"])
        persisted = self._state_text()
        for canary in (
            SIGNED_URL,
            "object-url-canary",
            "polluted-canary",
            "nested-canary",
            "objects.example.test",
        ):
            self.assertNotIn(canary, persisted)
        parent = durable["builderTasks"][created["id"]]
        self.assertNotIn("objectUrl", json.dumps(parent["events"], sort_keys=True))

    def test_cancellation_during_redemption_drops_signed_url(self):
        created = self._create(operation_id="operation-object-cancel")
        entered = threading.Event()
        release = threading.Event()
        lease_result = []

        def redeem(_binding):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return RedeemedObjectSource(
                expires_at=int(time.time()) + 60,
                allowed_host="objects.example.test",
                object_url=SIGNED_URL,
            )

        with patch.object(
            control_server,
            "redeem_builder_object_source",
            side_effect=redeem,
        ):
            worker = threading.Thread(
                target=lambda: lease_result.append(self._lease())
            )
            worker.start()
            self.assertTrue(entered.wait(timeout=2))
            canceled = control_server.handle_builder_task_cancel(
                self.service_token,
                created["id"],
            )["task"]
            self.assertEqual(canceled["status"], "cancel_requested")
            release.set()
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(lease_result, [None])
        state = load_state()
        parent = state["builderTasks"][created["id"]]
        child = state["agentTasks"][parent["agentTaskId"]]
        self.assertEqual(parent["status"], "canceled")
        self.assertEqual(child["status"], "canceled")
        self.assertNotIn(SIGNED_URL, self._state_text())
        self.assertNotIn("object-url-canary", self._state_text())


if __name__ == "__main__":
    unittest.main()
