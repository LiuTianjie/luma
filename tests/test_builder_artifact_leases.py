from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from luma.artifact_leases import ArtifactLeaseManager
from luma.agent import _complete_agent_task
from luma.builder_executor import open_builder_analysis_artifact
from luma.control import server as control_server
from luma.control.client import ControlClient
from luma.control.server import create_app, handle_node_agent_token
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError


class BuilderArtifactLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "luma.yaml"
        self.config_path.write_text("providers: {}\n", encoding="utf-8")
        self.service_token = "lae-artifact-service-token"
        self.environment = patch.dict(
            os.environ,
            {
                "LUMA_CONTROL_STATE_DIR": str(self.state_dir),
                "LUMA_CONTROL_CONFIG": str(self.config_path),
                "LUMA_LAE_SERVICE_TOKEN": self.service_token,
            },
            clear=False,
        )
        self.environment.start()
        state = init_state(
            domain="luma.example.com",
            cluster_id="luma-artifact-test",
            overwrite=True,
        )
        self.management_token = state["deployToken"]
        self.body = b'{"schemaVersion":"lae.deployment-plan/v1"}'
        self.digest = "sha256:" + hashlib.sha256(self.body).hexdigest()
        self.task_id = "builder-artifact-test"
        self.descriptor = {
            "digest": self.digest,
            "mediaType": "application/vnd.lae.deployment-plan+json",
            "sizeBytes": len(self.body),
        }
        state["nodes"] = {
            "builder": {
                "name": "builder",
                "region": "home",
                "nodeId": "builder-node-id",
                "agent": {
                    "status": "ready",
                    "os": "linux",
                    "capabilities": ["builder-artifact-export-v1"],
                },
            }
        }
        state["builderTasks"] = {
            self.task_id: {
                "id": self.task_id,
                "kind": "analyze-source",
                "principalRef": "lae-service",
                "tenantRef": "tenant-artifact-test",
                "applicationRef": "application-artifact-test",
                "externalOperationId": "operation-artifact-test",
                "status": "succeeded",
                "builderNode": "builder",
                "result": {"artifacts": {"deploymentPlan": self.descriptor}},
            }
        }
        save_state(state)
        self.previous_manager = control_server.ARTIFACT_DOWNLOADS
        control_server.ARTIFACT_DOWNLOADS = ArtifactLeaseManager(
            temporary_root=self.root / "rendezvous"
        )
        issued = handle_node_agent_token(
            self.management_token,
            {"nodeName": "builder", "nodeId": "builder-node-id"},
        )
        self.node_token = issued["agentToken"]

    def tearDown(self) -> None:
        control_server.ARTIFACT_DOWNLOADS = self.previous_manager
        self.environment.stop()
        self.tmp.cleanup()

    def lease_body(self) -> dict[str, object]:
        return {
            "schemaVersion": "luma.artifact-download-lease/v1",
            "tenantRef": "tenant-artifact-test",
            "applicationRef": "application-artifact-test",
            "externalOperationId": "operation-artifact-test",
            "builderTaskId": self.task_id,
            "artifact": {"name": "deploymentPlan", **self.descriptor},
            "ttlSeconds": 60,
        }

    def issue(self, client: TestClient):
        return client.post(
            f"/v1/builder/tasks/{self.task_id}/artifact-download-leases",
            json=self.lease_body(),
            headers={"Authorization": f"Bearer {self.service_token}"},
        )

    def upload(self, client: TestClient, lease_id: str, body: bytes | None = None):
        value = self.body if body is None else body
        return client.post(
            f"/v1/node-agent/artifact-downloads/{lease_id}/content",
            content=value,
            headers={
                "Authorization": f"Bearer {self.node_token}",
                "Content-Type": self.descriptor["mediaType"],
                "Content-Length": str(len(value)),
                "X-Luma-Artifact-Digest": self.digest,
                "X-Luma-Node-Name": "builder",
                "X-Luma-Node-Id": "builder-node-id",
            },
        )

    def test_http_issue_upload_download_and_replay_fence(self) -> None:
        with TestClient(create_app()) as client:
            self.assertIn(
                "builder-artifact-download-v1",
                client.get("/v1/health").json()["capabilities"],
            )
            issued = self.issue(client)
            self.assertEqual(issued.status_code, 201, issued.text)
            lease = issued.json()
            lease_id = lease["leaseId"]
            token = lease["downloadToken"]
            self.assertNotIn(token, json.dumps(load_state(), sort_keys=True))
            export_tasks = [
                task
                for task in load_state()["agentTasks"].values()
                if task.get("action") == "export-builder-artifact"
            ]
            self.assertEqual(len(export_tasks), 1)
            self.assertNotIn(token, json.dumps(export_tasks, sort_keys=True))
            self.assertNotIn("/var/lib/luma", json.dumps(export_tasks))

            uploaded = self.upload(client, lease_id)
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            downloaded = client.get(
                f"/v1/builder/artifact-downloads/{lease_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(downloaded.status_code, 200, downloaded.text)
            self.assertEqual(downloaded.content, self.body)
            self.assertEqual(
                downloaded.headers["x-luma-artifact-digest"], self.digest
            )
            replay = client.get(
                f"/v1/builder/artifact-downloads/{lease_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertNotEqual(replay.status_code, 200)

    def test_cross_principal_descriptor_and_late_state_changes_fail_closed(self) -> None:
        with TestClient(create_app()) as client:
            management = client.post(
                f"/v1/builder/tasks/{self.task_id}/artifact-download-leases",
                json=self.lease_body(),
                headers={"Authorization": f"Bearer {self.management_token}"},
            )
            self.assertEqual(management.status_code, 401)

            changed = self.lease_body()
            changed["artifact"] = {
                "name": "deploymentPlan",
                **self.descriptor,
                "digest": "sha256:" + "f" * 64,
            }
            mismatch = client.post(
                f"/v1/builder/tasks/{self.task_id}/artifact-download-leases",
                json=changed,
                headers={"Authorization": f"Bearer {self.service_token}"},
            )
            self.assertNotEqual(mismatch.status_code, 201)

            lease = self.issue(client).json()
            state = load_state()
            state["builderTasks"][self.task_id]["status"] = "canceled"
            save_state(state)
            rejected = self.upload(client, lease["leaseId"])
            self.assertNotEqual(rejected.status_code, 200)

    def test_size_digest_and_expiry_are_enforced_without_secret_persistence(self) -> None:
        with TestClient(create_app()) as client:
            lease = self.issue(client).json()
            token = lease["downloadToken"]
            bad = self.upload(client, lease["leaseId"], self.body + b"x")
            self.assertNotEqual(bad.status_code, 200)
            self.assertNotIn(token, json.dumps(load_state(), sort_keys=True))

        record = control_server.ARTIFACT_DOWNLOADS.get_record(lease["leaseId"])
        record.expires_at = time.time() - 1
        with self.assertRaisesRegex(LumaError, "not found"):
            control_server.ARTIFACT_DOWNLOADS.redeem(lease["leaseId"], token)


class BuilderArtifactFileTests(unittest.TestCase):
    def test_export_opens_only_the_content_addressed_verified_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = b'{"safe":true}'
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            hexadecimal = digest.removeprefix("sha256:")
            path = (
                root
                / "artifacts"
                / "deployment-plan"
                / "sha256"
                / hexadecimal[:2]
                / f"{hexadecimal}.json"
            )
            path.parent.mkdir(parents=True)
            path.write_bytes(body)
            payload = {
                "leaseId": "artdl_" + "a" * 40,
                "builderTaskId": "builder-file-test",
                "artifact": {
                    "name": "deploymentPlan",
                    "digest": digest,
                    "mediaType": "application/vnd.lae.deployment-plan+json",
                    "sizeBytes": len(body),
                },
            }
            with patch.dict(
                os.environ, {"LUMA_BUILDER_SNAPSHOT_ROOT": str(root)}, clear=False
            ):
                export = open_builder_analysis_artifact(payload)
                try:
                    self.assertEqual(export.stream.read(), body)
                finally:
                    export.close()
                path.write_bytes(body + b"x")
                with self.assertRaisesRegex(LumaError, "descriptor changed"):
                    open_builder_analysis_artifact(payload)

    def test_control_client_streams_bytes_without_token_in_url(self) -> None:
        captured: dict[str, object] = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _size: int = -1) -> bytes:
                return b'{"leaseId":"artdl_' + b"a" * 40 + b'","accepted":true}'

        class Opener:
            def open(self, request, *, timeout):
                captured["url"] = request.full_url
                captured["headers"] = dict(request.header_items())
                captured["body"] = b"".join(request.data)
                captured["timeout"] = timeout
                return Response()

        with patch("urllib.request.build_opener", return_value=Opener()):
            client = ControlClient(
                "https://luma.example.test", "node-agent-secret-token"
            )
            result = client.upload_builder_artifact(
                lease_id="artdl_" + "a" * 40,
                node_name="builder",
                node_id="builder-node-id",
                stream=io.BytesIO(b"verified bytes"),
                media_type="application/json",
                digest="sha256:" + "b" * 64,
                size_bytes=len(b"verified bytes"),
            )
        self.assertTrue(result["accepted"])
        self.assertEqual(captured["body"], b"verified bytes")
        self.assertNotIn("node-agent-secret-token", str(captured["url"]))
        self.assertEqual(
            captured["headers"]["Authorization"],
            "Bearer node-agent-secret-token",
        )

    def test_node_agent_exports_then_reports_only_safe_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "agent.json"
            config.write_text("{}", encoding="utf-8")
            body = b'{"safe":true}'
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            hexadecimal = digest.removeprefix("sha256:")
            path = (
                root
                / "snapshots"
                / "artifacts"
                / "deployment-plan"
                / "sha256"
                / hexadecimal[:2]
                / f"{hexadecimal}.json"
            )
            path.parent.mkdir(parents=True)
            path.write_bytes(body)
            lease_id = "artdl_" + "a" * 40
            task = {
                "id": "task-export-test",
                "action": "export-builder-artifact",
                "payload": {
                    "leaseId": lease_id,
                    "builderTaskId": "builder-export-test",
                    "artifact": {
                        "name": "deploymentPlan",
                        "digest": digest,
                        "mediaType": "application/vnd.lae.deployment-plan+json",
                        "sizeBytes": len(body),
                    },
                },
            }

            class Client:
                completion = None

                def heartbeat_agent(self, **_kwargs):
                    return {"cancelRequested": False}

                def upload_builder_artifact(self, **kwargs):
                    self.uploaded = kwargs["stream"].read()
                    return {"leaseId": kwargs["lease_id"], "accepted": True}

                def complete_agent_task(self, **kwargs):
                    self.completion = kwargs

            client = Client()
            with patch.dict(
                os.environ,
                {"LUMA_BUILDER_SNAPSHOT_ROOT": str(root / "snapshots")},
                clear=False,
            ):
                restart = _complete_agent_task(
                    client,
                    node_name="builder",
                    node_id="builder-node-id",
                    task=task,
                    config_path=config,
                )
            self.assertFalse(restart)
            self.assertEqual(client.uploaded, body)
            self.assertEqual(client.completion["status"], "succeeded")
            encoded = json.dumps(client.completion, sort_keys=True)
            self.assertNotIn(str(root), encoded)
            self.assertNotIn(body.decode(), encoded)


if __name__ == "__main__":
    unittest.main()
