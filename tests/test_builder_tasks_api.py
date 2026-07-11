import io
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from luma.control.client import ControlClient
from luma.control.server import create_app, handle_node_agent_complete, handle_node_agent_lease, handle_node_agent_token
from luma.control.state import init_state, load_state, save_state


class BuilderTaskApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "luma.yaml"
        self.config_path.write_text("providers: {}\n", encoding="utf-8")
        self.service_token = "lae-builder-api-test-token"
        self.env = patch.dict(
            os.environ,
            {
                "LUMA_CONTROL_STATE_DIR": str(self.state_dir),
                "LUMA_CONTROL_CONFIG": str(self.config_path),
                "LUMA_LAE_SERVICE_TOKEN": self.service_token,
                "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": "registry.internal/lae-runner@sha256:" + ("a" * 64),
            },
            clear=False,
        )
        self.env.start()
        state = init_state(domain="luma.example.com", cluster_id="luma-builder-api", overwrite=True)
        self.management_token = state["deployToken"]
        state["nodes"] = {
            "builder": {
                "name": "builder",
                "region": "home",
                "nodeId": "builder-node-id",
                "agent": {
                    "status": "ready",
                    "os": "linux",
                    "capabilities": ["docker-build", "builder-task-v1"],
                },
            }
        }
        state["build"] = {"nodes": ["builder"], "defaultNode": "builder"}
        save_state(state)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def _body():
        return {
            "schemaVersion": "luma.builder-task/v1",
            "kind": "analyze-source",
            "externalOperationId": "op-api-test",
            "tenantRef": "tenant-api-test",
            "applicationRef": "app-api-test",
            "payload": {
                "sourceRef": {"repository": "https://github.com/acme/app.git", "ref": "main"},
                "credentialLeaseId": "credential-lease-api-test",
                "agentImageDigest": "registry.internal/lae-runner@sha256:" + ("a" * 64),
                "policyVersion": "2026-07-11",
                "limits": {"cpu": 1, "memoryMiB": 1024, "diskMiB": 4096, "timeoutSeconds": 300},
            },
        }

    def test_asgi_builder_routes_require_scoped_token_and_idempotency_header(self):
        with TestClient(create_app()) as client:
            health = client.get("/v1/health")
            self.assertIn("builder-task-api-v1", health.json()["capabilities"])

            management = client.post(
                "/v1/builder/tasks",
                json=self._body(),
                headers={"Authorization": f"Bearer {self.management_token}", "Idempotency-Key": "idem-management"},
            )
            self.assertEqual(management.status_code, 401)

            missing_key = client.post(
                "/v1/builder/tasks",
                json=self._body(),
                headers={"Authorization": f"Bearer {self.service_token}"},
            )
            self.assertEqual(missing_key.status_code, 400)

            created = client.post(
                "/v1/builder/tasks",
                json=self._body(),
                headers={"Authorization": f"Bearer {self.service_token}", "Idempotency-Key": "idem-api"},
            )
            self.assertEqual(created.status_code, 202)
            task_id = created.json()["task"]["id"]

            fetched = client.get(
                f"/v1/builder/tasks/{task_id}",
                headers={"Authorization": f"Bearer {self.service_token}"},
            )
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(fetched.json()["task"]["id"], task_id)

            events = client.get(
                f"/v1/builder/tasks/{task_id}/events?after=0&limit=1",
                headers={"Authorization": f"Bearer {self.service_token}"},
            )
            self.assertEqual(events.status_code, 200)
            self.assertEqual(len(events.json()["events"]), 1)

            canceled = client.post(
                f"/v1/builder/tasks/{task_id}/cancel",
                json={},
                headers={"Authorization": f"Bearer {self.service_token}"},
            )
            self.assertEqual(canceled.status_code, 200)
            self.assertEqual(canceled.json()["task"]["status"], "canceled")

    def test_invalid_agent_result_fails_parent_without_persisting_secret(self):
        with TestClient(create_app()) as client:
            created = client.post(
                "/v1/builder/tasks",
                json=self._body(),
                headers={"Authorization": f"Bearer {self.service_token}", "Idempotency-Key": "idem-result"},
            ).json()["task"]

        issued = handle_node_agent_token(
            self.management_token,
            {"nodeName": "builder", "nodeId": "builder-node-id"},
        )
        leased = handle_node_agent_lease(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "os": "linux",
                "capabilities": ["docker-build", "builder-task-v1"],
                "waitSeconds": 0,
            },
        )["task"]
        sentinel = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        completion = handle_node_agent_complete(
            issued["agentToken"],
            {
                "nodeName": "builder",
                "nodeId": "builder-node-id",
                "taskId": leased["id"],
                "status": "succeeded",
                "message": "finished",
                "result": {"resolvedCommit": "b" * 40, "token": sentinel},
            },
        )

        self.assertEqual(completion["status"], "failed")
        parent = load_state()["builderTasks"][created["id"]]
        self.assertEqual(parent["status"], "failed")
        self.assertEqual(parent["result"], {})
        self.assertNotIn(sentinel, (self.state_dir / "control.json").read_text(encoding="utf-8"))

    def test_asgi_builder_routes_use_stable_conflict_and_not_found_statuses(self):
        with TestClient(create_app()) as client:
            headers = {
                "Authorization": f"Bearer {self.service_token}",
                "Idempotency-Key": "idem-conflict-status",
            }
            created = client.post("/v1/builder/tasks", json=self._body(), headers=headers)
            self.assertEqual(created.status_code, 202)

            changed = self._body()
            changed["payload"]["sourceRef"]["ref"] = "release"
            conflict = client.post("/v1/builder/tasks", json=changed, headers=headers)
            self.assertEqual(conflict.status_code, 409)

            missing = client.get(
                "/v1/builder/tasks/builder-missing",
                headers={"Authorization": f"Bearer {self.service_token}"},
            )
            self.assertEqual(missing.status_code, 404)

    def test_asgi_malformed_builder_json_is_a_stable_bad_request(self):
        with TestClient(create_app()) as client:
            response = client.post(
                "/v1/builder/tasks",
                content=b'{"schemaVersion":',
                headers={
                    "Authorization": f"Bearer {self.service_token}",
                    "Idempotency-Key": "idem-malformed-json",
                    "Content-Type": "application/json",
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errorInfo"]["code"], "bad_request")
        self.assertIn("malformed JSON", response.json()["errorInfo"]["message"])

    def test_control_client_sends_idempotency_header_and_encodes_task_paths(self):
        client = ControlClient("https://luma.example.com", self.service_token)
        response = MagicMock()
        response.read.return_value = b'{"task":{"id":"builder-1"}}'
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            client.create_builder_task(self._body(), idempotency_key="idem-client")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://luma.example.com/v1/builder/tasks")
        self.assertEqual(request.get_header("Idempotency-key"), "idem-client")

        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            client.get_builder_task_events("builder/id", after=7, limit=25)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://luma.example.com/v1/builder/tasks/builder%2Fid/events?after=7&limit=25",
        )


if __name__ == "__main__":
    unittest.main()
