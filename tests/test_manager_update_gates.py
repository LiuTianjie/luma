"""Manager update is a Control operation with a maintenance lease."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.errors import LumaError


def _manager_state(**extra):
    state = {
        "clusterId": "luma-test",
        "domain": "luma.example.com",
        "deployToken": "management-token",
        "nodes": {
            "manager": {
                "labels": {"role.nomad-manager": "true"},
                "agent": {
                    "status": "online",
                    "lastSeen": int(time.time()),
                    "capabilities": ["manager-update-v1"],
                    "version": "0.1.319",
                },
            }
        },
        "buildRuns": {},
    }
    state.update(extra)
    return state


class ManagerUpdateGateTests(unittest.TestCase):
    def test_rejects_new_deploy_while_maintenance_is_held(self):
        from luma.control.server import handle_deployment

        state = _manager_state(controlMaintenance={
            "kind": "manager-update",
            "id": "manager-op-1",
            "status": "held",
        })
        with patch("luma.control.server.load_state", return_value=state):
            with self.assertRaisesRegex(LumaError, "cluster maintenance in progress"):
                handle_deployment("management-token", {"manifest": "name: demo\n"})

    def test_rejects_manager_update_when_a_build_is_running(self):
        from luma.control.server import handle_manager_update_start

        state = _manager_state(buildRuns={"build-1": {"id": "build-1", "status": "running"}})
        with patch("luma.control.server.load_state", return_value=state):
            with self.assertRaisesRegex(LumaError, "active build"):
                handle_manager_update_start("management-token", {"installRef": "v0.1.320"})

    def test_rejects_manager_update_when_control_image_is_not_cached(self):
        from luma.control.server import handle_manager_update_start

        state = _manager_state()
        with patch("luma.control.server.load_state", return_value=state), patch(
            "luma.control.server._prepared_control_image",
            side_effect=LumaError("Control image is not cached in the internal registry"),
        ):
            with self.assertRaisesRegex(LumaError, "not cached"):
                handle_manager_update_start("management-token", {"installRef": "v0.1.320"})

    def test_start_holds_maintenance_and_status_releases_it(self):
        from luma.control import server

        state = _manager_state()
        held = {}

        def set_maintenance(**kwargs):
            held.update(kwargs)
            state["controlMaintenance"] = {
                "kind": kwargs["kind"],
                "id": kwargs["operation_id"],
                "status": "held",
            }

        released = []
        written = []
        agent_start = {
            "taskId": "task-1",
            "updateId": "manager-1783835000000-aabbccdd",
            "status": "running",
            "installRef": "v0.1.320",
            "controlImage": "ghcr.io/liutianjie/luma-control:v0.1.320",
            "message": "started",
        }
        with patch.object(server, "load_state", return_value=state), patch.object(
            server, "_run_node_agent_task", return_value=agent_start
        ), patch.object(
            server, "_prepared_control_image", return_value=agent_start["controlImage"]
        ), patch.object(
            server, "_manager_update_baseline", return_value={"routeOk": True, "agentVersion": "0.1.319"}
        ), patch.object(server, "_set_control_maintenance", side_effect=set_maintenance), patch.object(
            server, "_manager_update_operation_write", side_effect=written.append
        ):
            response = server.handle_manager_update_start("management-token", {"installRef": "v0.1.320"})
        self.assertEqual(held["kind"], "manager-update")
        self.assertTrue(response["operationId"].startswith("manager-op-"))
        self.assertEqual(response["controlImage"], agent_start["controlImage"])

        operation = dict(written[-1])
        agent_status = {
            "status": "succeeded",
            "updateId": agent_start["updateId"],
            "message": "control healthy",
        }
        with patch.object(server, "load_state", return_value=state), patch.object(
            server, "_run_node_agent_task", return_value=dict(agent_status)
        ), patch.object(server, "_latest_manager_update_operation", return_value=operation), patch.object(
            server, "_control_maintenance", return_value={"id": operation["id"], "status": "held"}
        ), patch.object(server, "_manager_update_operation_read", return_value=operation), patch.object(
            server, "_verify_manager_update", return_value={"ok": True}
        ), patch.object(server, "_manager_update_operation_write") as persist, patch.object(
            server, "_release_control_maintenance", side_effect=lambda op_id, status="released": released.append((op_id, status))
        ):
            status = server.handle_manager_update_status("management-token", agent_start["updateId"])
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(released, [(response["operationId"], "succeeded")])
        persist.assert_called()

    def test_status_keeps_failed_when_route_newly_breaks(self):
        from luma.control import server

        operation = {
            "id": "manager-op-1",
            "baseline": {"routeOk": True},
            "domain": "luma.example.com",
        }
        state = _manager_state()
        with patch.object(server, "load_state", return_value=state), patch.object(
            server, "_run_node_agent_task",
            return_value={"status": "succeeded", "message": "unit exited 0"},
        ), patch.object(server, "_latest_manager_update_operation", return_value=operation), patch.object(
            server, "_control_maintenance", return_value={"id": "manager-op-1", "status": "held"}
        ), patch.object(server, "_manager_update_operation_read", return_value=operation), patch.object(
            server, "_verify_manager_update",
            return_value={"ok": False, "message": "route newly failing"},
        ), patch.object(server, "_manager_update_operation_write"), patch.object(
            server, "_release_control_maintenance"
        ) as release:
            status = server.handle_manager_update_status("management-token")
        self.assertEqual(status["status"], "failed")
        self.assertIn("route newly failing", status["message"])
        release.assert_called_once_with("manager-op-1", status="failed")

    def test_status_does_not_reverify_a_finished_operation(self):
        from luma.control import server

        operation = {
            "id": "manager-op-1",
            "status": "succeeded",
            "message": "already done",
            "finishedAt": 1,
            "baseline": {"routeOk": True},
        }
        state = _manager_state()
        with patch.object(server, "load_state", return_value=state), patch.object(
            server, "_run_node_agent_task",
            return_value={"status": "succeeded", "message": "unit exited 0"},
        ), patch.object(server, "_control_maintenance", return_value=None), patch.object(
            server, "_latest_manager_update_operation", return_value=operation
        ), patch.object(server, "_verify_manager_update") as verify, patch.object(
            server, "_manager_update_operation_write"
        ) as persist, patch.object(server, "_release_control_maintenance") as release:
            status = server.handle_manager_update_status("management-token")
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["message"], "already done")
        verify.assert_not_called()
        persist.assert_not_called()
        release.assert_not_called()


class PreparedControlImageTests(unittest.TestCase):
    def test_accepts_already_internal_image(self):
        from luma.control.server import _prepared_control_image

        state = _manager_state()
        with patch("luma.control.server._control_image_prepare_plan", return_value={
            "destinationImage": "registry.internal/luma-control:v0.1.320",
            "alreadyInternal": True,
        }):
            image = _prepared_control_image(state, "v0.1.320", "registry.internal/luma-control:v0.1.320")
        self.assertEqual(image, "registry.internal/luma-control:v0.1.320")

    def test_allows_direct_image_when_builder_registry_is_not_configured(self):
        from luma.control.server import _prepared_control_image

        state = _manager_state()
        with patch(
            "luma.control.server._control_image_prepare_plan",
            side_effect=LumaError("internal build registryHost is not configured; configure Builder registry settings before updating Control"),
        ):
            image = _prepared_control_image(
                state, "v0.1.320", "ghcr.io/liutianjie/luma-control:v0.1.320"
            )
        self.assertEqual(image, "ghcr.io/liutianjie/luma-control:v0.1.320")

    def test_requires_succeeded_prepare_record(self):
        from luma.control.server import _prepared_control_image

        state = _manager_state()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("luma.control.server._control_image_prepare_plan", return_value={
                "destinationImage": "registry.internal/luma-control:v0.1.320",
                "alreadyInternal": False,
            }), patch("luma.control.server._control_image_prepare_dir", return_value=root):
                with self.assertRaisesRegex(LumaError, "not cached"):
                    _prepared_control_image(state, "v0.1.320", "ghcr.io/liutianjie/luma-control:v0.1.320")
