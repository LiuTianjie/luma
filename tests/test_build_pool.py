import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.control import server as srv
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError

CAPABILITIES = ["docker-build", "native-arm-build-v1", "repository-build-inputs-v1", "repository-build-plan-v1"]


class BuildPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, LUMA_CONTROL_STATE_DIR=str(root / "state"), LUMA_CONTROL_CONFIG=str(root / "luma.yaml"))
        env.start()
        self.addCleanup(env.stop)
        (root / "luma.yaml").write_text("providers: {}\n")
        state = init_state(domain="luma.example.com", cluster_id="pool-test", overwrite=True)
        state["build"] = {"nodes": ["builder", "cn-worker-1", "lab"], "defaultNode": "builder",
                          "registryHost": "100.64.0.70:5000", "pushHost": "100.64.0.70:5000"}
        state["nodes"] = {
            name: {"name": name, "region": region, "nomadStatus": "ready",
                   "agent": {"status": "ready", "os": "linux", "arch": "amd64", "capabilities": list(CAPABILITIES)}}
            for name, region in (("builder", "home"), ("cn-worker-1", "cn"), ("lab", "home"))
        }
        save_state(state)
        self.token = state["deployToken"]
        self.proxies = patch.object(srv, "_build_proxy_for_request",
                                    side_effect=lambda _config, _state, node, _body: f"http://egress-{node}:3128")
        self.proxies.start()
        self.addCleanup(self.proxies.stop)
        self.metrics = patch.object(srv, "_record_metrics_history")
        self.metrics.start()
        self.addCleanup(self.metrics.stop)

    def agent_token(self, node):
        return srv.handle_node_agent_token(self.token, {"nodeName": node})["agentToken"]

    def lease(self, node):
        body = {"nodeName": node, "os": "linux", "arch": "amd64", "capabilities": list(CAPABILITIES), "activeTaskId": ""}
        return srv.handle_node_agent_lease(self.agent_token(node), body)["task"]

    def pooled_task(self, candidates, *, affinity=0, build_run_id=""):
        return srv._queue_pooled_agent_task(
            load_state(), candidates, "build-image", {"repoUrl": "https://git.example.com/acme/app.git"},
            payload_by_node={name: {"proxy": f"http://egress-{name}:3128"} for name in candidates},
            required_capability="docker-build", build_run_id=build_run_id, affinity_seconds=affinity,
        )

    def busy(self, node):
        srv._mutate_control_state(lambda state: state.setdefault("agentTasks", {}).update(
            {f"busy-{node}": {"id": f"busy-{node}", "nodeName": node, "status": "running", "leasedAt": int(time.time())}}))

    def test_any_candidate_may_lease_and_binds_its_own_payload(self):
        task_id = self.pooled_task(["builder", "cn-worker-1"])
        self.assertIsNone(self.lease("lab"))  # Not a candidate.
        leased = self.lease("cn-worker-1")
        self.assertEqual(leased["id"], task_id)
        self.assertEqual(leased["payload"]["proxy"], "http://egress-cn-worker-1:3128")
        task = load_state()["agentTasks"][task_id]
        self.assertEqual((task["nodeName"], task["status"]), ("cn-worker-1", "running"))
        self.assertNotIn("payloadByNode", task)
        self.assertIsNone(self.lease("builder"))  # Bound exactly once.

    def test_affinity_gives_the_warm_builder_a_head_start(self):
        task_id = self.pooled_task(["builder", "cn-worker-1"], affinity=60)
        self.assertIsNone(self.lease("cn-worker-1"))
        self.assertEqual(self.lease("builder")["id"], task_id)

    def test_busy_or_offline_warm_builder_spills_to_an_idle_candidate(self):
        for blocked in ("busy", "offline"):
            with self.subTest(blocked=blocked):
                if blocked == "busy":
                    self.busy("builder")
                else:
                    srv._mutate_control_state(lambda state: state["agentTasks"].pop("busy-builder", None))
                    srv._mutate_control_state(lambda state: state["nodes"]["builder"]["agent"].update(status="offline"))
                task_id = self.pooled_task(["builder", "cn-worker-1"], affinity=60)
                self.assertEqual(self.lease("cn-worker-1")["id"], task_id)
                srv._mutate_control_state(lambda state: state["agentTasks"][task_id].update(status="succeeded"))

    def test_affinity_window_bounds_the_wait_for_an_idle_but_silent_agent(self):
        task_id = self.pooled_task(["builder", "cn-worker-1"], affinity=60)
        srv._mutate_control_state(lambda state: state["agentTasks"][task_id].update(createdAt=int(time.time()) - 61))
        self.assertEqual(self.lease("cn-worker-1")["id"], task_id)

    def test_incompatible_candidate_skips_pooled_work_without_failing_it(self):
        task_id = self.pooled_task(["builder", "cn-worker-1"])
        srv._mutate_control_state(lambda state: state["nodes"]["cn-worker-1"]["agent"].update(capabilities=["docker-build"]))
        body = {"nodeName": "cn-worker-1", "os": "linux", "arch": "amd64", "capabilities": ["docker-build"], "activeTaskId": ""}
        self.assertIsNone(srv.handle_node_agent_lease(self.agent_token("cn-worker-1"), body)["task"])
        self.assertEqual(load_state()["agentTasks"][task_id]["status"], "queued")
        self.assertEqual(self.lease("builder")["id"], task_id)

    def test_lease_records_the_builder_on_the_build_run(self):
        run = srv._create_build_run({"ref": "main"}, source="https://git.example.com/acme/app.git", build_node="builder",
                                    project_key="acme/app", build_candidates=["builder", "cn-worker-1"])
        self.busy("builder")
        self.pooled_task(["builder", "cn-worker-1"], build_run_id=run)
        self.lease("cn-worker-1")
        record = load_state()["buildRuns"][run]
        self.assertEqual((record["buildNode"], record["request"]["buildNode"]), ("cn-worker-1", "cn-worker-1"))

    def test_waiting_caller_learns_which_builder_ran_the_task(self):
        state = load_state()
        results = {}

        def run():
            results["build"] = srv._run_node_agent_task(
                state, "builder", "build-image", {"repoUrl": "https://git.example.com/acme/app.git"},
                required_capability="docker-build", pool=["builder", "cn-worker-1"], timeout=10,
                progress=lambda event: results.setdefault("events", []).append(event),
            )

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.time() + 5
        while time.time() < deadline and not any(task.get("poolCandidates") for task in (load_state().get("agentTasks") or {}).values()):
            time.sleep(0.05)
        leased = self.lease("cn-worker-1")
        srv._mutate_control_state(lambda current: current["agentTasks"][leased["id"]].update(status="succeeded", result={"image": "x"}))
        worker.join(5)
        self.assertEqual(results["build"]["agentNode"], "cn-worker-1")
        self.assertEqual(results["build"]["image"], "x")
        self.assertIn("Leased by builder cn-worker-1", [event.get("message") for event in results["events"]])

    def test_busy_build_nodes_tracks_active_agent_tasks(self):
        self.busy("builder")
        state = load_state()
        self.assertEqual(srv._busy_build_nodes(state), {"builder"})
        self.assertEqual(srv._busy_build_nodes(state, exclude_run="busy-builder"), set())

    def test_no_ready_candidate_rejects_pooled_dispatch(self):
        srv._mutate_control_state(lambda state: [state["nodes"][name]["agent"].update(status="offline") for name in ("builder", "cn-worker-1")])
        with self.assertRaisesRegex(LumaError, "no node agent is ready in builder pool"):
            self.pooled_task(["builder", "cn-worker-1"])


if __name__ == "__main__":
    unittest.main()
