from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from starlette.testclient import TestClient

from luma.build_capabilities import REQUIRED_BUILD_CAPABILITY, normalize_build_capabilities
from luma.cli import build_parser
from luma.cli.builds import cmd_import
from luma.control.workflows import handle_workflow_check
from luma.control import server
from luma.control.client import ControlClient
from luma.deploy_workflow import comparison_fields, make_recipe, validate_recipe
from luma.errors import LumaError

CAPABILITY = "repository-build-inputs-v1"


def builder_state(capabilities=None):
    return {
        "build": {"defaultNode": "builder", "registryHost": "registry.internal:5000"},
        "nodes": {"builder": {"name": "builder", "region": "home", "agent": {
            "status": "ready", "os": "linux", "capabilities": capabilities if capabilities is not None else ["docker-build", CAPABILITY],
        }}},
    }


class BuildCapabilityClientTests(unittest.TestCase):
    def test_cli_flag_is_repeatable_and_body_is_canonical(self):
        args = build_parser().parse_args(["import", "acme/web", "--require-build-capability", CAPABILITY,
                                          "--require-build-capability", CAPABILITY])
        self.assertEqual(args.require_build_capabilities, [CAPABILITY, CAPABILITY])
        self.assertEqual(ControlClient._build_body(vars(args))["requireBuildCapabilities"], [CAPABILITY])
        self.assertNotIn("requireBuildCapabilities", ControlClient._build_body({}))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["build", "local", ".", "--require-build-capability", CAPABILITY])

    def test_request_shape_rejects_coercion_and_unbounded_names(self):
        for raw in (CAPABILITY, {CAPABILITY: True}, [None], [True], [""], [" two"], ["Upper"], ["x" * 97], ["x"] * 33):
            with self.subTest(raw=raw), self.assertRaises(LumaError):
                normalize_build_capabilities(raw)

    def test_control_health_gate_prevents_both_request_and_stream(self):
        for operation in ("build_deploy", "build_deploy_events", "retry_build"):
            client = ControlClient("https://control.example.com", "test")
            with patch.object(client, "health", return_value={"capabilities": []}), patch.object(client, "request") as request, patch.object(client, "stream") as stream:
                with self.assertRaisesRegex(LumaError, "update the manager"):
                    getattr(client, operation)(*( ["build-1"] if operation == "retry_build" else []), require_build_capabilities=[CAPABILITY])
                request.assert_not_called()
                stream.assert_not_called()

    def test_upgraded_client_transmits_requirements_to_control(self):
        client = ControlClient("https://control.example.com", "test")
        with patch.object(client, "health", return_value={"capabilities": [REQUIRED_BUILD_CAPABILITY]}), patch.object(client, "_with_build_queue", side_effect=lambda body: body), patch.object(client, "request") as request:
            client.build_deploy(repo_url="https://github.com/acme/web.git", require_build_capabilities=[CAPABILITY])
            self.assertEqual(request.call_args.args[2]["requireBuildCapabilities"], [CAPABILITY])

    def test_preflight_reuses_control_default_and_requires_literal_ready(self):
        client = ControlClient("https://control.example.com", "test")
        build = {"defaultNode": "builder", "nodes": [{"name": "builder", "ready": True, "storageCapabilities": [CAPABILITY]}]}
        with patch.object(client, "health", return_value={"capabilities": [REQUIRED_BUILD_CAPABILITY]}), patch.object(client, "status", return_value={"build": build}):
            self.assertEqual(client.check_build_capabilities(required=[CAPABILITY]), {"buildNode": "builder", "buildCapabilities": [CAPABILITY]})
            build["nodes"][0]["storageCapabilities"] = []
            with self.assertRaisesRegex(LumaError, CAPABILITY):
                client.check_build_capabilities(required=[CAPABILITY])
            build["nodes"][0]["ready"] = "true"
            with self.assertRaisesRegex(LumaError, "not declared and ready"):
                client.check_build_capabilities(required=[CAPABILITY])
            with self.assertRaisesRegex(LumaError, "not declared and ready"):
                client.check_build_capabilities(required=[CAPABILITY], build_node="other")

    def test_health_advertises_requirement_enforcement(self):
        client = TestClient(server.create_app())
        try:
            self.assertIn(REQUIRED_BUILD_CAPABILITY, client.get("/v1/health").json()["capabilities"])
        finally:
            client.close()

    def test_workflow_records_compares_and_retries_requirements(self):
        args = build_parser().parse_args(["--no-env", "import", "acme/web", "--require-build-capability", CAPABILITY])
        recipe = validate_recipe(make_recipe(args))
        self.assertEqual(comparison_fields(recipe)["require_build_capabilities"], [CAPABILITY])
        old = make_recipe(build_parser().parse_args(["--no-env", "import", "acme/web"]))
        self.assertNotIn("require_build_capabilities", comparison_fields(old))
        retry_args = build_parser().parse_args(["--no-env", "build", "retry", "build-1"])
        retry_args._workflow_retry_run = {"request": {"repoUrl": "https://github.com/acme/web.git", "requireBuildCapabilities": [CAPABILITY]}}
        retry = validate_recipe(make_recipe(retry_args))
        self.assertEqual(comparison_fields(retry)["require_build_capabilities"], [CAPABILITY])


    def test_workflow_dropping_requirement_needs_approval_but_first_record_does_not(self):
        before = make_recipe(build_parser().parse_args(["--no-env", "import", "acme/web", "--require-build-capability", CAPABILITY]))
        after = make_recipe(build_parser().parse_args(["--no-env", "import", "acme/web"]))
        state = {"deploymentWorkflows": {"web": {"name": "web", "recipe": before, "repoKey": "github.com/acme/web"}}}
        with patch("luma.control.workflows.load_state", return_value=state), patch("luma.control.workflows.require_token"):
            check = handle_workflow_check("test", {"recipe": after, "selector": {"name": "web"}})
            self.assertEqual(check["status"], "confirmation-required")
            self.assertIn({"field": "require_build_capabilities", "previous": [CAPABILITY], "requested": None}, check["differences"])
            state["deploymentWorkflows"] = {}
            self.assertEqual(handle_workflow_check("test", {"recipe": before})["status"], "unrecorded")

    def test_cli_preflight_precedes_environment_read_and_passes_result_contract(self):
        args = build_parser().parse_args(["--no-env", "import", "acme/web", "--require-build-capability", CAPABILITY, "--format", "json"])
        client = Mock()
        client.build_deploy_events.return_value = iter([{"status": "done", "result": {"service": "web", "buildCapabilities": [CAPABILITY]}}])
        with patch("luma.cli.builds._control_context", return_value=("https://control.example.com", "test", False, None)), patch("luma.cli.common.ControlClient", return_value=client), patch("luma.cli.builds._workflow_prepare"), patch("luma.cli.builds._workflow_finish"), patch("luma.cli.builds._import_env_secrets") as env, contextlib.redirect_stdout(io.StringIO()):
            client.check_build_capabilities.side_effect = LumaError("update the manager")
            with self.assertRaisesRegex(LumaError, "update the manager"):
                cmd_import(args)
            env.assert_not_called()
            client.build_deploy_events.assert_not_called()
            client.check_build_capabilities.side_effect = None
            self.assertEqual(cmd_import(args), 0)
            self.assertEqual(client.build_deploy_events.call_args.kwargs["require_build_capabilities"], [CAPABILITY])
            client.build_deploy_events.return_value = iter([{"status": "done", "result": {"service": "web"}}])
            with self.assertRaisesRegex(LumaError, "Control did not confirm"):
                cmd_import(args)


class BuildCapabilityControlTests(unittest.TestCase):
    def setUp(self):
        self.state = builder_state()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, kwargs in {
            "load_state": {"return_value": self.state},
            "require_token": {}, "_reject_control_maintenance": {}, "_apply_state_secrets": {},
            "load_config": {}, "_build_proxy_for_request": {"return_value": ""},
            "_target_build_platform_inventory": {"return_value": {"byNode": {}, "byRegion": {}}},
            "_create_build_run": {"return_value": "build-test"},
            "_build_run_cancel_requested": {"return_value": False}, "_append_build_run_event": {},
            "_complete_build_run": {},
        }.items():
            self.stack.enter_context(patch.object(server, name, **kwargs))
        self.body = {"repoUrl": "https://github.com/acme/web.git", "requireBuildCapabilities": [CAPABILITY]}
        self.build_result = {"image": "registry.internal/acme/web:abc", "manifest": "name: web\nregion: home\nexposure: none\n", "buildCapabilities": [CAPABILITY]}

    def test_old_builder_is_rejected_before_build_or_enqueue(self):
        self.state["nodes"]["builder"]["agent"]["capabilities"] = ["docker-build"]
        with patch.object(server, "_run_node_agent_task") as task:
            for queued in (False, True):
                with self.assertRaisesRegex(LumaError, "builder builder.*" + CAPABILITY):
                    server.handle_build_deploy("test", {**self.body, "queue": queued})
            task.assert_not_called()
            server._create_build_run.assert_not_called()

    def test_missing_result_ack_never_deploys(self):
        for acknowledgement in (None, [], CAPABILITY, ["other-v1"]):
            with self.subTest(acknowledgement=acknowledgement), patch.object(server, "_run_node_agent_task", return_value={**self.build_result, "buildCapabilities": acknowledgement}), patch.object(server, "handle_deployment") as deploy, patch.object(server, "handle_compose_deployment") as compose:
                with self.assertRaisesRegex(LumaError, "Builder did not confirm"):
                    server.handle_build_deploy("test", self.body)
                deploy.assert_not_called()
                compose.assert_not_called()
                self.assertEqual(server._complete_build_run.call_args.args[1], "failed")

    def test_requirement_reaches_agent_and_success_evidence(self):
        with patch.object(server, "_run_node_agent_task", return_value=self.build_result) as task, patch.object(server, "handle_deployment", return_value={"service": "web"}) as deploy:
            result = server.handle_build_deploy("test", self.body)
            self.assertEqual(task.call_args.args[3]["requireBuildCapabilities"], [CAPABILITY])
            self.assertEqual(result["buildCapabilities"], [CAPABILITY])
            self.assertEqual(deploy.call_args.args[1]["gitSource"]["requireBuildCapabilities"], [CAPABILITY])

    def test_no_requirement_preserves_old_builder_behavior(self):
        self.state["nodes"]["builder"]["agent"]["capabilities"] = ["docker-build"]
        self.build_result.pop("buildCapabilities")
        with patch.object(server, "_run_node_agent_task", return_value=self.build_result) as task, patch.object(server, "handle_deployment", return_value={"service": "web"}):
            result = server.handle_build_deploy("test", {"repoUrl": self.body["repoUrl"]})
            self.assertNotIn("requireBuildCapabilities", task.call_args.args[3])
            self.assertNotIn("buildCapabilities", result)

    def test_fresh_queue_state_rechecks_agent_before_creating_task(self):
        live = builder_state(["docker-build"])
        with patch.object(server, "_mutate_control_state", side_effect=lambda fn: fn(live)):
            with self.assertRaisesRegex(LumaError, CAPABILITY):
                server._queue_node_agent_task(self.state, "builder", "build-image", self.body, required_capability="docker-build")
            self.assertNotIn("agentTasks", live)

    def test_lease_downgrade_fails_queued_task_without_disclosing_credentials(self):
        state = builder_state(["docker-build"])
        state["agentTasks"] = {"task-1": {"nodeName": "builder", "action": "build-image", "payload": self.body, "status": "queued"}}
        with patch.object(server, "_mutate_control_state_if_changed", side_effect=lambda fn: fn(state)[0]), patch.object(server, "_require_node_agent_token", return_value=state["nodes"]["builder"]), patch.object(server, "_update_agent_heartbeat"), patch.object(server, "_prune_agent_tasks"), patch.object(server, "_prune_build_runs"), patch.object(server, "_reconcile_interrupted_agent_tasks"), patch.object(server, "load_runtime_state", return_value=state), patch.object(server, "_record_metrics_history"), patch.object(server, "_agent_task_lease_payload") as payload:
            result = server.handle_node_agent_lease("test", {"nodeName": "builder"})
        self.assertIsNone(result["task"])
        self.assertEqual(state["agentTasks"]["task-1"]["status"], "failed")
        self.assertIn(CAPABILITY, state["agentTasks"]["task-1"]["message"])
        payload.assert_not_called()

    def test_retry_cannot_drop_original_requirements(self):
        self.state["buildRuns"] = {"build-1": {"request": self.body}}
        with patch.object(server, "handle_build_deploy", return_value={}) as build:
            server.handle_build_run_retry("test", "build-1", {"requireBuildCapabilities": []})
            self.assertEqual(build.call_args.args[1]["requireBuildCapabilities"], [CAPABILITY])

    def test_local_request_does_not_silently_accept_remote_requirement(self):
        with self.assertRaisesRegex(LumaError, "only for remote"):
            server.handle_local_build_prepare("test", self.body)


if __name__ == "__main__":
    unittest.main()
