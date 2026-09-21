"""Negotiated action ownership during mixed Python/Go agent migration."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from luma.control import server as control
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError


class AgentActionCapabilities(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = root / "luma.yaml"
        config.write_text("providers: {}\n")
        env = patch.dict(os.environ, LUMA_CONTROL_STATE_DIR=str(root / "state"), LUMA_CONTROL_CONFIG=str(config))
        env.start()
        self.addCleanup(env.stop)
        state = init_state(domain="fixture.test", cluster_id="fixture", overwrite=True)
        state["nodes"] = {"worker": {"name": "worker", "nodeId": "node-1", "region": "cn"}}
        self.token = control._issue_node_agent_token(state, "worker", node_id="node-1")
        self.body = {"nodeName": "worker", "nodeId": "node-1", "os": "linux", "capabilities": ["managed-volume-path"],
                     "supportedActions": ["prepare-managed-volume-path"]}
        control._update_agent_heartbeat(state["nodes"]["worker"], self.body)
        save_state(state)

    def test_unknown_action_rejected_before_queue_even_without_capability_gate(self):
        state = load_state()
        with self.assertRaisesRegex(LumaError, "does not support action"):
            control._queue_node_agent_task(state, "worker", "configure-insecure-registry", {"registry": "fixture.invalid"}, required_capability=None)
        self.assertFalse(load_state().get("agentTasks"))

    def test_explicit_empty_action_list_never_leases(self):
        state = load_state()
        state["agentTasks"] = {"task": {"id": "task", "nodeName": "worker", "status": "queued", "action": "prepare-managed-volume-path", "payload": {}}}
        save_state(state)
        response = control.handle_node_agent_lease(self.token, {**self.body, "supportedActions": []})
        self.assertIsNone(response["task"])
        self.assertEqual(load_state()["agentTasks"]["task"]["status"], "queued")

    def test_python_rollback_clears_go_action_restriction(self):
        state = load_state()
        state["agentTasks"] = {"task": {"id": "task", "nodeName": "worker", "status": "queued", "action": "configure-insecure-registry", "payload": {}}}
        save_state(state)
        legacy = {key: value for key, value in self.body.items() if key != "supportedActions"}
        response = control.handle_node_agent_lease(self.token, legacy)
        self.assertEqual(response["task"]["id"], "task")
        self.assertNotIn("supportedActions", load_state()["nodes"]["worker"]["agent"])

    def test_malformed_negotiation_does_not_clear_restriction(self):
        for value in (None, "*", ["*"], [1], ["a" * 81], ["valid"] * 101):
            with self.subTest(value=value):
                with self.assertRaisesRegex(LumaError, "supportedActions"):
                    control.handle_node_agent_lease(self.token, {**self.body, "supportedActions": value})
                self.assertEqual(load_state()["nodes"]["worker"]["agent"]["supportedActions"], self.body["supportedActions"])

    def test_identity_only_completion_presence_preserves_negotiation(self):
        state = load_state()
        record = state["nodes"]["worker"]
        control._update_agent_heartbeat(record, {"nodeName": "worker", "nodeId": "node-1", "status": "succeeded"})
        self.assertEqual(record["agent"]["supportedActions"], self.body["supportedActions"])
