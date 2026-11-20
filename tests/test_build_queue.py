import argparse
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from starlette.testclient import TestClient

from luma.cli.builds import _wait_for_queued_build
from luma.control import server as srv
from luma.control.client import ControlClient
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError


class BuildQueueRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            LUMA_CONTROL_STATE_DIR=str(root / "state"),
            LUMA_CONTROL_CONFIG=str(root / "luma.yaml"),
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
        state = init_state(domain="luma.example.com", cluster_id="test", overwrite=True)
        state["build"] = {"defaultNode": "builder", "registryHost": "builder:5000"}
        state["nodes"] = {
            "cn": {
                "name": "cn",
                "region": "cn",
                "nomadStatus": "ready",
                "agent": {"status": "ready", "os": "linux", "arch": "amd64"},
            }
        }
        save_state(state)
        self.token = state["deployToken"]

    def test_create_app_starts_zero_build_queue_worker_threads(self):
        with patch.object(srv.operations_api.OperationsWorker, "start"), patch.object(
            srv.operations_api.OperationsWorker, "close"
        ):
            app = srv.create_app()
            with TestClient(app) as client:
                names = [thread.name for thread in threading.enumerate() if thread.is_alive()]
                self.assertFalse(
                    any(name.startswith("luma-build-queue-") for name in names),
                    f"unexpected build queue threads: {names}",
                )
                self.assertFalse(hasattr(app.state, "build_queue_worker"))
                health = client.get("/v1/health")
                self.assertEqual(health.status_code, 200)
                self.assertNotIn("build-queue-v1", health.json()["capabilities"])

    def test_old_cli_local_build_with_queue_true_completes_synchronously(self):
        with patch.object(srv.operations_api.OperationsWorker, "start"), patch.object(
            srv.operations_api.OperationsWorker, "close"
        ), patch.object(
            srv, "handle_deployment", return_value={"service": "app", "steps": []}
        ) as deploy:
            with TestClient(srv.create_app()) as client:
                headers = {"Authorization": f"Bearer {self.token}"}
                prepared = client.post(
                    "/v1/builds/local/prepare",
                    headers=headers,
                    json={
                        "repoUrl": "https://github.com/acme/app.git",
                        "region": "cn",
                        "queue": True,
                        "workflow": {"recipe": {"method": "local-build"}},
                    },
                )
                self.assertEqual(prepared.status_code, 200, prepared.text)
                build_id = prepared.json()["run"]["id"]
                image = prepared.json()["upload"]["image"]

                completed = client.post(
                    f"/v1/builds/local/{build_id}/complete",
                    headers=headers,
                    json={
                        "queue": True,
                        "workflow": {"recipe": {"method": "local-build"}},
                        "buildResult": {
                            "buildCapabilities": ["native-arm-build-v1"],
                            "kind": "service",
                            "image": image,
                            "manifest": "name: app\nregion: cn\nexposure: none\n",
                        },
                    },
                )
                self.assertEqual(completed.status_code, 200, completed.text)
                payload = completed.json()
                self.assertNotIn("queued", payload)
                self.assertEqual(payload["service"], "app")
                self.assertEqual(payload["buildRunId"], build_id)

                detail = client.get(f"/v1/builds/{build_id}", headers=headers)
                self.assertEqual(detail.status_code, 200, detail.text)
                run = detail.json()["run"]
                self.assertEqual(run["status"], "succeeded")
                self.assertEqual(run["result"]["service"], "app")
                self.assertEqual(run["result"]["image"], image)

        deploy.assert_called_once()
        deploy_body = deploy.call_args.args[1]
        self.assertNotIn("queue", deploy_body)
        self.assertNotIn("workflow", deploy_body)

    def test_old_cli_local_build_failure_marks_run_failed_synchronously(self):
        prepared = srv.handle_local_build_prepare(
            self.token,
            {"repoUrl": "https://github.com/acme/app.git", "region": "cn", "queue": True},
        )
        build_id = prepared["run"]["id"]
        with patch.object(srv, "handle_deployment", side_effect=LumaError("nomad rollout failed")):
            with self.assertRaisesRegex(LumaError, "nomad rollout failed"):
                srv.handle_local_build_complete(
                    self.token,
                    build_id,
                    {
                        "queue": True,
                        "buildResult": {
                            "buildCapabilities": ["native-arm-build-v1"],
                            "kind": "service",
                            "image": prepared["upload"]["image"],
                            "manifest": "name: app\nregion: cn\nexposure: none\n",
                        },
                    },
                )
        run = srv.handle_build_run_get(self.token, build_id)["run"]
        self.assertEqual(run["status"], "failed")
        self.assertIn("nomad rollout failed", run["message"])

    def test_legacy_remote_build_endpoints_return_retirement_error(self):
        expected = "Legacy built-in remote build queue has been retired"
        with patch.object(srv.operations_api.OperationsWorker, "start"), patch.object(
            srv.operations_api.OperationsWorker, "close"
        ):
            with TestClient(srv.create_app()) as client:
                headers = {"Authorization": f"Bearer {self.token}"}
                for path, body in (
                    ("/v1/builds", {"repoUrl": "https://github.com/acme/app.git", "queue": True}),
                    ("/v1/builds/build-123/retry", {"queue": True}),
                    ("/v1/builds/build-123/cancel", {}),
                ):
                    with self.subTest(path=path):
                        response = client.post(path, headers=headers, json=body)
                        self.assertEqual(response.status_code, 400)
                        self.assertIn(expected, response.json()["error"])
                        unauth = client.post(path, headers={"Authorization": "Bearer wrong"}, json=body)
                        self.assertEqual(unauth.status_code, 401)

                for path, body in (
                    ("/v1/builds/stream", {"repoUrl": "https://github.com/acme/app.git"}),
                    ("/v1/builds/build-123/retry/stream", {}),
                ):
                    with self.subTest(path=path):
                        response = client.post(path, headers=headers, json=body)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(expected, response.text)

    def test_application_update_delegates_to_standalone_builder(self):
        manifest = "name: app\nimage: builder:5000/acme/app:v1\nregion: cn\nexposure: none\n"
        service = srv._load_service_manifest(manifest)
        srv._mark_service_deployment(
            service,
            manifest,
            "https://github.com/acme/app.git",
            status="active",
            steps=[],
            git_source={
                "repoUrl": "https://github.com/acme/app.git",
                "ref": "main",
                "region": "cn",
            },
        )

        with self.assertRaisesRegex(LumaError, "luma addon install builder"):
            srv.handle_application_update(self.token, {"name": "app"})

        events = []
        with patch.object(
            srv.independent_builder,
            "submit",
            return_value={"build": {"id": "bld-42", "status": "queued"}},
        ) as submit:
            result = srv.handle_application_update(
                self.token,
                {"name": "app", "envSecrets": {"API_KEY": "secret"}},
                progress=events.append,
            )
        submit.assert_called_once()
        submitted_token, submitted_body = submit.call_args.args
        self.assertEqual(submitted_token, self.token)
        self.assertEqual(submitted_body["repoUrl"], "https://github.com/acme/app.git")
        self.assertEqual(submitted_body["ref"], "main")
        self.assertEqual(submitted_body["envSecrets"], {"API_KEY": "secret"})
        self.assertEqual(result["service"], "app")
        self.assertEqual(result["build"]["id"], "bld-42")
        self.assertEqual([event["status"] for event in events], ["start", "ok"])

    def test_startup_reconciliation_closes_leftover_queued_and_finalizing_runs(self):
        state = load_state()
        state["buildRuns"] = {
            "legacy-queued": {
                "id": "legacy-queued",
                "status": "queued",
                "controlProcessInstanceId": srv._CONTROL_PROCESS_INSTANCE_ID,
                "events": [],
                "createdAt": 1,
                "updatedAt": 1,
            },
            "legacy-finalizing": {
                "id": "legacy-finalizing",
                "status": "finalizing",
                "controlProcessInstanceId": "old-control",
                "queueExecuting": True,
                "events": [],
                "createdAt": 1,
                "updatedAt": 1,
            },
            "legacy-succeeded": {
                "id": "legacy-succeeded",
                "status": "succeeded",
                "controlProcessInstanceId": "old-control",
                "events": [],
                "createdAt": 1,
                "updatedAt": 1,
            },
        }
        state["buildQueue"] = {"legacy-queued": {"kind": "remote", "body": {}}}
        save_state(state)

        self.assertEqual(srv._reconcile_orphaned_build_runs_after_control_restart(), 2)
        recovered = load_state()
        self.assertEqual(recovered["buildRuns"]["legacy-queued"]["status"], "failed")
        self.assertIn("retired", recovered["buildRuns"]["legacy-queued"]["message"])
        self.assertEqual(recovered["buildRuns"]["legacy-finalizing"]["status"], "failed")
        self.assertNotIn("queueExecuting", recovered["buildRuns"]["legacy-finalizing"])
        self.assertEqual(recovered["buildRuns"]["legacy-succeeded"]["status"], "succeeded")
        self.assertNotIn("buildQueue", recovered)
        self.assertEqual(srv._reconcile_orphaned_build_runs_after_control_restart(), 0)

    def test_active_build_blockers_tracks_live_local_builds(self):
        prepared = srv.handle_local_build_prepare(
            self.token, {"repoUrl": "https://github.com/acme/app.git", "region": "cn"}
        )
        build_id = prepared["run"]["id"]
        self.assertIn(build_id, srv._active_build_blockers(load_state()))
        srv.handle_local_build_fail(self.token, build_id, {"message": "aborted"})
        self.assertNotIn(build_id, srv._active_build_blockers(load_state()))


class QueueClientTests(unittest.TestCase):
    def test_lease_long_poll_reserves_time_to_receive_committed_task(self):
        client = ControlClient("https://control.example.com", "token")
        for timeout in (5, 15, 30, 60, 120):
            with self.subTest(timeout=timeout), patch.object(client, "request", return_value={}) as request:
                client.lease_agent_task(node_name="builder", timeout=timeout)
            wait = request.call_args.args[2]["waitSeconds"]
            self.assertGreaterEqual(wait, 0)
            self.assertLessEqual(wait, 30)
            self.assertGreaterEqual(request.call_args.kwargs["timeout"] - wait, min(timeout, 15))

    def test_idle_lease_explicitly_reports_no_active_task(self):
        client = ControlClient("https://control.example.com", "token")
        with patch.object(client, "request", return_value={}) as request:
            client.lease_agent_task(node_name="builder")
        self.assertEqual(request.call_args.args[2]["activeTaskId"], "")

    def test_capability_negotiation_and_legacy_fallback(self):
        for supported in (True, False):
            client = ControlClient("https://control.example.com", "token")
            with patch.object(
                client, "health", return_value={"capabilities": ["build-queue-v1"] if supported else []}
            ), patch.object(client, "request", return_value={}) as request:
                client.prepare_local_build({"repoUrl": "https://github.com/acme/app.git"})
                self.assertEqual(request.call_args.args[2].get("queue", False), supported)
                client.complete_local_build("b1", build_result={})
                self.assertEqual(request.call_args.args[2].get("queue", False), supported)
                client.build_deploy(repo_url="https://github.com/acme/app.git")
                self.assertEqual(request.call_args.args[2].get("queue", False), supported)

    def test_poll_timeout_never_cancels_or_fails_server_job(self):
        args = argparse.Namespace(timeout=0, format="json", quiet=True)
        client = Mock()
        client.get_build.return_value = {"run": {"status": "queued", "queuePosition": 2, "waitingFor": "b0"}}
        with self.assertRaisesRegex(LumaError, "server task continues"):
            _wait_for_queued_build(args, client, {"queued": True, "buildRunId": "b1"})
        client.cancel_build.assert_not_called()
        client.fail_local_build.assert_not_called()

    def test_poll_does_not_treat_acceptance_as_deployment_success(self):
        args = argparse.Namespace(timeout=10, format="json", quiet=True)
        client = Mock()
        client.get_build.side_effect = [
            {"run": {"status": "queued", "queuePosition": 1}},
            {"run": {"status": "succeeded", "result": {"service": "app", "image": "image:immutable"}}},
        ]
        with patch("luma.cli.common.time.sleep"):
            result = _wait_for_queued_build(args, client, {"queued": True, "buildRunId": "b1"})
        self.assertEqual(result, {"service": "app", "image": "image:immutable", "buildRunId": "b1"})


if __name__ == "__main__":
    unittest.main()
