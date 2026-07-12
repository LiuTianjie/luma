import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from luma import credential_broker
from luma.control import server as control_server
from luma.control.state import init_state, load_state, save_state
from luma.credential_broker import (
    BROKER_REQUEST_SCHEMA_VERSION,
    BROKER_RESPONSE_SCHEMA_VERSION,
    CredentialBrokerError,
    CredentialLeaseBinding,
    RedeemedCredential,
)
from luma.errors import LumaError


RUNNER_IMAGE = "registry.internal/lae-agent-runner@sha256:" + ("a" * 64)
GIT_CANARY = "github_pat_credential_broker_canary_1234567890"
BROKER_AUTH_CANARY = "broker-auth-canary-must-not-persist"
GENERIC_FAILURE = "builder credential lease redemption failed"


class BuilderCredentialBrokerTests(unittest.TestCase):
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

        state = init_state(domain="luma.example.com", cluster_id="luma-broker-test", overwrite=True)
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

    def _body(self, *, operation_id="operation-broker-test", repository="https://github.com/acme/private.git"):
        return {
            "schemaVersion": "luma.builder-task/v1",
            "kind": "analyze-source",
            "externalOperationId": operation_id,
            "tenantRef": "tenant-broker-test",
            "applicationRef": "application-broker-test",
            "payload": {
                "sourceRef": {"repository": repository, "ref": "main", "subdirectory": ""},
                "credentialLeaseId": "credential-lease-broker-test",
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

    def _create(self, *, operation_id="operation-broker-test", repository="https://github.com/acme/private.git"):
        result = control_server.handle_builder_task_create(
            self.service_token,
            self._body(operation_id=operation_id, repository=repository),
            idempotency_key="idem-" + operation_id,
        )
        return result["task"]

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

    def _state_text(self):
        return (self.state_dir / "control.json").read_text(encoding="utf-8")

    @staticmethod
    def _binding():
        return CredentialLeaseBinding(
            lease_id="credential-lease-broker-test",
            builder_task_id="builder-task-test",
            external_operation_id="operation-broker-test",
            principal_ref="lae-service",
            tenant_ref="tenant-broker-test",
            application_ref="application-broker-test",
            repository="https://github.com/acme/private.git",
        )

    @staticmethod
    def _broker_response(binding, *, kind="git-https", expires_at=1_000_100, **overrides):
        response = {
            "schemaVersion": BROKER_RESPONSE_SCHEMA_VERSION,
            **binding.request_body(),
            "kind": kind,
            "expiresAt": expires_at,
        }
        response["schemaVersion"] = BROKER_RESPONSE_SCHEMA_VERSION
        if kind == "git-https":
            response["credential"] = {"username": "x-access-token", "password": GIT_CANARY}
        response.update(overrides)
        return response

    def _configured_broker(self):
        token_file = self.root / "broker.token"
        token_file.write_text(BROKER_AUTH_CANARY + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        return patch.dict(
            os.environ,
            {
                "LUMA_CREDENTIAL_BROKER_URL": "https://broker.internal/v1/credential-leases/redeem",
                "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": str(token_file),
            },
            clear=False,
        )

    def test_git_https_credential_is_only_injected_into_the_http_lease_payload(self):
        created = self._create()
        captured = {}

        def redeem(binding):
            captured["binding"] = binding
            return RedeemedCredential(
                kind="git-https",
                expires_at=int(time.time()) + 60,
                username="x-access-token",
                password=GIT_CANARY,
            )

        with patch.object(control_server, "redeem_builder_credential", side_effect=redeem):
            leased = self._lease()

        self.assertIsNotNone(leased)
        self.assertEqual(leased["payload"]["gitUsername"], "x-access-token")
        self.assertEqual(leased["payload"]["gitToken"], GIT_CANARY)
        binding = captured["binding"]
        self.assertEqual(binding.lease_id, "credential-lease-broker-test")
        self.assertEqual(binding.builder_task_id, created["id"])
        self.assertEqual(binding.external_operation_id, "operation-broker-test")
        self.assertEqual(binding.principal_ref, "lae-service")
        self.assertEqual(binding.tenant_ref, "tenant-broker-test")
        self.assertEqual(binding.application_ref, "application-broker-test")
        self.assertEqual(binding.repository, "https://github.com/acme/private.git")
        self.assertNotIn(GIT_CANARY, self._state_text())
        state = load_state()
        self.assertEqual(state["agentTasks"][leased["id"]]["status"], "running")
        self.assertEqual(state["builderTasks"][created["id"]]["status"], "running")

    def test_none_response_and_unconfigured_public_https_continue_anonymously(self):
        self._create(operation_id="operation-none")
        with patch.object(
            control_server,
            "redeem_builder_credential",
            return_value=RedeemedCredential(kind="none", expires_at=int(time.time()) + 60),
        ):
            leased = self._lease()
        self.assertIsNotNone(leased)
        self.assertNotIn("gitToken", leased["payload"])
        self.assertNotIn("gitUsername", leased["payload"])

        # No broker configuration is a supported anonymous path for a public
        # credential-free HTTPS repository.
        binding = self._binding()
        credential = credential_broker.redeem_builder_credential(binding, now=1_000_000)
        self.assertEqual(credential.kind, "none")
        self.assertFalse(credential.password)

    def test_protocol_is_closed_and_token_file_is_used_for_a_bound_short_lease(self):
        binding = self._binding()
        captured = {}

        def post(config, body):
            captured["url"] = config.url
            captured["token"] = config.bearer_token
            captured["body"] = dict(body)
            return self._broker_response(binding)

        with self._configured_broker(), patch.object(credential_broker, "_post_redemption", side_effect=post):
            result = credential_broker.redeem_builder_credential(binding, now=1_000_000)

        self.assertEqual(result.kind, "git-https")
        self.assertEqual(result.username, "x-access-token")
        self.assertEqual(result.password, GIT_CANARY)
        self.assertEqual(captured["token"], BROKER_AUTH_CANARY)
        self.assertEqual(
            set(captured["body"]),
            {
                "schemaVersion",
                "leaseId",
                "builderTaskId",
                "externalOperationId",
                "principalRef",
                "tenantRef",
                "applicationRef",
                "repository",
            },
        )
        self.assertEqual(captured["body"]["schemaVersion"], BROKER_REQUEST_SCHEMA_VERSION)
        self.assertEqual(captured["url"], "https://broker.internal/v1/credential-leases/redeem")
        self.assertNotIn(BROKER_AUTH_CANARY, self._state_text())

    def test_max_ttl_is_validated_at_response_time(self):
        binding = self._binding()
        response = self._broker_response(
            binding,
            kind="none",
            expires_at=1_000_301,
        )
        with self._configured_broker(), patch.object(
            credential_broker,
            "_post_redemption",
            return_value=response,
        ), patch.object(
            credential_broker,
            "_unix_time",
            side_effect=(1_000_000, 1_000_001),
        ):
            result = credential_broker.redeem_builder_credential(binding)

        self.assertEqual(result.kind, "none")
        self.assertEqual(result.expires_at, 1_000_301)

    def test_broker_token_file_rejects_symlinks_and_world_readable_permissions(self):
        binding = self._binding()
        token_file = self.root / "unsafe-broker.token"
        token_file.write_text(BROKER_AUTH_CANARY, encoding="utf-8")
        token_file.chmod(0o604)
        env = {
            "LUMA_CREDENTIAL_BROKER_URL": "https://broker.internal/v1/credential-leases/redeem",
            "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": str(token_file),
        }
        with patch.dict(os.environ, env, clear=False), self.assertRaises(CredentialBrokerError):
            credential_broker.redeem_builder_credential(binding, now=1_000_000)

        safe_file = self.root / "safe-broker.token"
        safe_file.write_text(BROKER_AUTH_CANARY, encoding="utf-8")
        safe_file.chmod(0o600)
        token_file.unlink()
        token_file.symlink_to(safe_file)
        with patch.dict(os.environ, env, clear=False), self.assertRaises(CredentialBrokerError):
            credential_broker.redeem_builder_credential(binding, now=1_000_000)

    def test_transport_timeout_is_collapsed_to_a_secret_free_generic_error(self):
        binding = self._binding()
        captured = {}

        class TimeoutOpener:
            def open(self, request, timeout):
                captured["authorization"] = request.get_header("Authorization")
                captured["timeout"] = timeout
                captured["body"] = json.loads(request.data.decode("utf-8"))
                raise TimeoutError("timeout included remote-canary-that-must-not-escape")

        with self._configured_broker(), patch.object(
            credential_broker.urllib.request,
            "build_opener",
            return_value=TimeoutOpener(),
        ):
            with self.assertRaises(CredentialBrokerError) as raised:
                credential_broker.redeem_builder_credential(binding, now=1_000_000)

        self.assertEqual(str(raised.exception), "credential broker redemption failed")
        self.assertNotIn("remote-canary", str(raised.exception))
        self.assertEqual(captured["authorization"], "Bearer " + BROKER_AUTH_CANARY)
        self.assertEqual(captured["body"], binding.request_body())
        self.assertEqual(captured["timeout"], 5.0)

    def test_malformed_wrong_scope_expired_and_timeout_fail_parent_and_child_generically(self):
        def response_for(body, name):
            if name == "malformed":
                return {"unexpected": "response-body-canary"}
            response = {
                "schemaVersion": BROKER_RESPONSE_SCHEMA_VERSION,
                **{key: value for key, value in body.items() if key != "schemaVersion"},
                "kind": "none",
                "expiresAt": int(time.time()) + 60,
            }
            if name == "wrong-scope":
                response["tenantRef"] = "another-tenant"
            if name == "expired":
                response["expiresAt"] = int(time.time()) - 1
            return response

        for index, name in enumerate(("malformed", "wrong-scope", "expired")):
            with self.subTest(name=name):
                operation_id = f"operation-failure-{index}"
                created = self._create(operation_id=operation_id)
                with self._configured_broker(), patch.object(
                    credential_broker,
                    "_post_redemption",
                    side_effect=lambda _config, body, case=name: response_for(body, case),
                ):
                    leased = self._lease()
                self.assertIsNone(leased)
                state = load_state()
                child = state["agentTasks"][state["builderTasks"][created["id"]]["agentTaskId"]]
                parent = state["builderTasks"][created["id"]]
                self.assertEqual(child["status"], "failed")
                self.assertEqual(parent["status"], "failed")
                self.assertEqual(child["message"], GENERIC_FAILURE)
                self.assertEqual(parent["message"], GENERIC_FAILURE)
                self.assertNotIn("response-body-canary", self._state_text())
                self.assertNotIn(GIT_CANARY, self._state_text())

        created = self._create(operation_id="operation-timeout")
        with self._configured_broker(), patch.object(
            credential_broker,
            "_post_redemption",
            side_effect=CredentialBrokerError("credential broker redemption failed"),
        ):
            leased = self._lease()
        self.assertIsNone(leased)
        state = load_state()
        self.assertEqual(state["builderTasks"][created["id"]]["status"], "failed")
        self.assertEqual(state["builderTasks"][created["id"]]["message"], GENERIC_FAILURE)

    def test_concurrent_pollers_cannot_double_redeem_and_broker_io_holds_no_state_lock(self):
        self._create(operation_id="operation-concurrent")
        entered = threading.Event()
        release = threading.Event()
        calls = []
        first_result = []

        def redeem(_binding):
            calls.append("redeem")
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return RedeemedCredential(kind="none", expires_at=int(time.time()) + 60)

        def first_lease():
            first_result.append(self._lease())

        with patch.object(control_server, "redeem_builder_credential", side_effect=redeem):
            first = threading.Thread(target=first_lease)
            first.start()
            self.assertTrue(entered.wait(timeout=2))

            second_result = []
            second = threading.Thread(target=lambda: second_result.append(self._lease()))
            second.start()
            second.join(timeout=2)
            self.assertFalse(second.is_alive(), "second poller blocked on state lock held during broker I/O")
            self.assertEqual(second_result, [None])

            release.set()
            first.join(timeout=3)

        self.assertFalse(first.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(first_result), 1)
        self.assertIsNotNone(first_result[0])

    def test_cancellation_during_redemption_drops_credential_and_finishes_terminally(self):
        created = self._create(operation_id="operation-cancel-redemption")
        entered = threading.Event()
        release = threading.Event()
        lease_result = []

        def redeem(_binding):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return RedeemedCredential(
                kind="git-https",
                expires_at=int(time.time()) + 60,
                username="x-access-token",
                password=GIT_CANARY,
            )

        with patch.object(control_server, "redeem_builder_credential", side_effect=redeem):
            worker = threading.Thread(target=lambda: lease_result.append(self._lease()))
            worker.start()
            self.assertTrue(entered.wait(timeout=2))
            canceled = control_server.handle_builder_task_cancel(self.service_token, created["id"])["task"]
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
        self.assertNotIn(GIT_CANARY, self._state_text())

    def test_management_token_and_legacy_git_providers_do_not_supply_analyze_credentials(self):
        state = load_state()
        state["gitProviders"] = {
            "github:legacy": {
                "type": "github",
                "token": GIT_CANARY,
                "username": "legacy-user",
            }
        }
        state["secrets"] = {"GITHUB_TOKEN": "legacy-secret-token-canary"}
        save_state(state)

        with self.assertRaisesRegex(LumaError, "unauthorized"):
            control_server.handle_builder_task_create(
                self.management_token,
                self._body(operation_id="operation-management"),
                idempotency_key="idem-management",
            )

        self._create(operation_id="operation-no-legacy")
        leased = self._lease()
        self.assertIsNotNone(leased)
        self.assertNotIn("gitToken", leased["payload"])
        self.assertNotIn("gitUsername", leased["payload"])

    def test_legacy_build_image_provider_injection_remains_compatible(self):
        state = load_state()
        state["gitProviders"] = {
            "github:legacy": {
                "type": "github",
                "token": GIT_CANARY,
                "username": "legacy-user",
            }
        }
        state["agentTasks"] = {
            "task-build-image": {
                "id": "task-build-image",
                "nodeName": "builder",
                "action": "build-image",
                "payload": {
                    "repoUrl": "https://github.com/acme/app.git",
                    "gitProviderId": "github:legacy",
                },
                "status": "queued",
                "createdAt": 1,
                "updatedAt": 1,
            }
        }
        save_state(state)

        leased = self._lease()
        self.assertEqual(leased["action"], "build-image")
        self.assertEqual(leased["payload"]["gitUsername"], "legacy-user")
        self.assertEqual(leased["payload"]["gitToken"], GIT_CANARY)
        self.assertNotIn(GIT_CANARY, json.dumps(load_state()["agentTasks"], sort_keys=True))

    def test_broker_url_must_be_https_and_environment_token_requires_explicit_test_mode(self):
        binding = self._binding()
        with patch.dict(
            os.environ,
            {
                "LUMA_CREDENTIAL_BROKER_URL": "http://broker.internal/redeem",
                "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": str(self.root / "missing"),
            },
            clear=False,
        ), self.assertRaises(CredentialBrokerError):
            credential_broker.redeem_builder_credential(binding, now=1_000_000)

        with patch.dict(
            os.environ,
            {
                "LUMA_CREDENTIAL_BROKER_URL": "https://broker.internal/redeem",
                "LUMA_CREDENTIAL_BROKER_TOKEN": BROKER_AUTH_CANARY,
                "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": "",
                "LUMA_CREDENTIAL_BROKER_TEST_MODE": "0",
            },
            clear=False,
        ), self.assertRaises(CredentialBrokerError):
            credential_broker.redeem_builder_credential(binding, now=1_000_000)

        with patch.dict(
            os.environ,
            {
                "LUMA_CREDENTIAL_BROKER_URL": "https://broker.internal/redeem",
                "LUMA_CREDENTIAL_BROKER_TOKEN": BROKER_AUTH_CANARY,
                "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": "",
                "LUMA_CREDENTIAL_BROKER_TEST_MODE": "1",
            },
            clear=False,
        ), patch.object(
            credential_broker,
            "_post_redemption",
            return_value=self._broker_response(binding, kind="none"),
        ):
            result = credential_broker.redeem_builder_credential(binding, now=1_000_000)
        self.assertEqual(result.kind, "none")


if __name__ == "__main__":
    unittest.main()
