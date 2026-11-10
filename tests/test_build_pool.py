import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.control import build_queue as queue
from luma.control import database
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
        state["build"] = {"nodes": ["builder", "bd-prod", "lab"], "defaultNode": "builder",
                          "registryHost": "100.64.0.70:5000", "pushHost": "100.64.0.70:5000"}
        state["nodes"] = {
            name: {"name": name, "region": region, "nomadStatus": "ready",
                   "agent": {"status": "ready", "os": "linux", "arch": "amd64", "capabilities": list(CAPABILITIES)}}
            for name, region in (("builder", "home"), ("bd-prod", "cn"), ("lab", "home"))
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
        task_id = self.pooled_task(["builder", "bd-prod"])
        self.assertIsNone(self.lease("lab"))  # Not a candidate.
        leased = self.lease("bd-prod")
        self.assertEqual(leased["id"], task_id)
        self.assertEqual(leased["payload"]["proxy"], "http://egress-bd-prod:3128")
        task = load_state()["agentTasks"][task_id]
        self.assertEqual((task["nodeName"], task["status"]), ("bd-prod", "running"))
        self.assertNotIn("payloadByNode", task)
        self.assertIsNone(self.lease("builder"))  # Bound exactly once.

    def test_affinity_gives_the_warm_builder_a_head_start(self):
        task_id = self.pooled_task(["builder", "bd-prod"], affinity=60)
        self.assertIsNone(self.lease("bd-prod"))
        self.assertEqual(self.lease("builder")["id"], task_id)

    def test_busy_or_offline_warm_builder_spills_to_an_idle_candidate(self):
        for blocked in ("busy", "offline"):
            with self.subTest(blocked=blocked):
                if blocked == "busy":
                    self.busy("builder")
                else:
                    srv._mutate_control_state(lambda state: state["agentTasks"].pop("busy-builder", None))
                    srv._mutate_control_state(lambda state: state["nodes"]["builder"]["agent"].update(status="offline"))
                task_id = self.pooled_task(["builder", "bd-prod"], affinity=60)
                self.assertEqual(self.lease("bd-prod")["id"], task_id)
                srv._mutate_control_state(lambda state: state["agentTasks"][task_id].update(status="succeeded"))

    def test_affinity_window_bounds_the_wait_for_an_idle_but_silent_agent(self):
        task_id = self.pooled_task(["builder", "bd-prod"], affinity=60)
        srv._mutate_control_state(lambda state: state["agentTasks"][task_id].update(createdAt=int(time.time()) - 61))
        self.assertEqual(self.lease("bd-prod")["id"], task_id)

    def test_incompatible_candidate_skips_pooled_work_without_failing_it(self):
        task_id = self.pooled_task(["builder", "bd-prod"])
        srv._mutate_control_state(lambda state: state["nodes"]["bd-prod"]["agent"].update(capabilities=["docker-build"]))
        body = {"nodeName": "bd-prod", "os": "linux", "arch": "amd64", "capabilities": ["docker-build"], "activeTaskId": ""}
        self.assertIsNone(srv.handle_node_agent_lease(self.agent_token("bd-prod"), body)["task"])
        self.assertEqual(load_state()["agentTasks"][task_id]["status"], "queued")
        self.assertEqual(self.lease("builder")["id"], task_id)

    def test_builder_promised_by_admission_never_takes_pooled_work(self):
        pinned = srv._create_build_run({"ref": "main"}, source="https://git.example.com/acme/pinned.git", build_node="bd-prod",
                                       project_key="acme/pinned", queued_work={"ref": "main"})
        self.assertEqual(queue.claim()[0], pinned)  # Admitted; its task is not queued yet.
        task_id = self.pooled_task(["bd-prod", "builder"])
        self.assertIsNone(self.lease("bd-prod"))
        self.assertEqual(self.lease("builder")["id"], task_id)

    def test_lease_records_the_builder_on_the_build_run(self):
        run = srv._create_build_run({"ref": "main"}, source="https://git.example.com/acme/app.git", build_node="builder",
                                    project_key="acme/app", build_candidates=["builder", "bd-prod"])
        self.busy("builder")
        self.pooled_task(["builder", "bd-prod"], build_run_id=run)
        self.lease("bd-prod")
        record = load_state()["buildRuns"][run]
        self.assertEqual((record["buildNode"], record["request"]["buildNode"]), ("bd-prod", "bd-prod"))

    def test_waiting_caller_learns_which_builder_ran_the_task(self):
        state = load_state()
        results = {}

        def run():
            results["build"] = srv._run_node_agent_task(
                state, "builder", "build-image", {"repoUrl": "https://git.example.com/acme/app.git"},
                required_capability="docker-build", pool=["builder", "bd-prod"], timeout=10,
                progress=lambda event: results.setdefault("events", []).append(event),
            )

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.time() + 5
        while time.time() < deadline and not any(task.get("poolCandidates") for task in (load_state().get("agentTasks") or {}).values()):
            time.sleep(0.05)
        leased = self.lease("bd-prod")
        srv._mutate_control_state(lambda current: current["agentTasks"][leased["id"]].update(status="succeeded", result={"image": "x"}))
        worker.join(5)
        self.assertEqual(results["build"]["agentNode"], "bd-prod")
        self.assertEqual(results["build"]["image"], "x")
        self.assertIn("Leased by builder bd-prod", [event.get("message") for event in results["events"]])

    def test_pool_admits_one_run_per_idle_candidate(self):
        runs = [srv._create_build_run({"ref": "main"}, source=f"https://git.example.com/acme/app-{i}.git", build_node="builder",
                                      project_key=f"acme/app-{i}", queued_work={"ref": "main"},
                                      build_candidates=["builder", "bd-prod", "lab"]) for i in range(4)]
        self.busy("lab")
        claimed = [queue.claim() for _ in range(4)]
        self.assertEqual([item[0] if item else None for item in claimed], [runs[0], runs[1], None, None])
        position = queue.position(runs[2])
        self.assertEqual((position["waitReason"], position["waitingForNode"]), ("builder", "builder"))

    def test_offline_pool_waits_and_reports_its_first_candidate(self):
        run = srv._create_build_run({"ref": "main"}, source="https://git.example.com/acme/app.git", build_node="builder",
                                    project_key="acme/app", queued_work={"ref": "main"}, build_candidates=["builder", "bd-prod"])
        srv._mutate_control_state(lambda state: [state["nodes"][name]["agent"].update(status="offline") for name in ("builder", "bd-prod")])
        self.assertEqual(queue.position(run)["waitReason"], "builder-offline")
        self.assertIsNone(queue.claim())
        srv._mutate_control_state(lambda state: state["nodes"]["bd-prod"]["agent"].update(status="ready"))
        self.assertEqual(queue.claim()[0], run)

    def test_import_pools_same_platform_builders_and_remembers_the_warm_one(self):
        calls = []

        def task(state, node, action, payload, **kwargs):
            calls.append((node, kwargs.get("pool"), kwargs.get("payload_by_node"), kwargs.get("affinity_seconds")))
            return {"kind": "service", "image": "100.64.0.70:5000/acme/app:abc", "agentNode": "bd-prod",
                    "buildCapabilities": ["native-arm-build-v1"], "manifest": "name: app\nregion: cn\nexposure: none\n"}

        build_id = srv.handle_build_deploy(self.token, {"repoUrl": "https://git.example.com/acme/app.git", "ref": "main", "queue": True})["buildRunId"]
        self.assertEqual(load_state()["buildRuns"][build_id]["buildCandidates"], ["builder", "bd-prod", "lab"])
        with patch.object(srv, "_run_node_agent_task", side_effect=task), \
             patch.object(srv, "handle_deployment", return_value={"service": "app"}) as deploy:
            while (item := queue.claim()):
                queue.execute(item)
        node, pool, payloads, affinity = calls[0]
        self.assertEqual(pool, ["builder", "bd-prod", "lab"])
        self.assertEqual(payloads["bd-prod"], {"proxy": "http://egress-bd-prod:3128"})
        self.assertEqual(affinity, srv._build_affinity_seconds())
        self.assertEqual(deploy.call_args.args[1]["gitSource"]["buildNode"], "bd-prod")
        state = load_state()
        self.assertEqual(state["buildRuns"][build_id]["status"], "succeeded")
        self.assertEqual(state["buildAffinity"]["acme/app"]["node"], "bd-prod")
        # The next build of the project prefers its warm builder.
        self.assertEqual(srv._rank_build_candidates(state, srv._compatible_build_records(state, []),
                                                    project_key="acme/app", prefer_idle=False)[0], "bd-prod")

    def test_pinned_import_and_loopback_push_host_never_pool(self):
        for body, build in (({"buildNode": "lab"}, {}), ({}, {"pushHost": "localhost:5000"})):
            with self.subTest(body=body, build=build):
                srv._mutate_control_state(lambda state: state["build"].update(build))
                response = srv.handle_build_deploy(self.token, {"repoUrl": "https://git.example.com/acme/app.git", "queue": True, **body})
                self.assertNotIn("buildCandidates", load_state()["buildRuns"][response["buildRunId"]])
                srv.handle_build_run_cancel(self.token, response["buildRunId"])

    def test_unplanned_pool_excludes_builders_with_an_unknown_engine_platform(self):
        srv._mutate_control_state(lambda state: [state["nodes"]["lab"]["agent"].pop(key) for key in ("os", "arch")])
        self.assertFalse(srv._has_native_builder_choices(load_state()))
        response = srv.handle_build_deploy(self.token, {"repoUrl": "https://git.example.com/acme/app.git", "queue": True})
        self.assertEqual(load_state()["buildRuns"][response["buildRunId"]]["buildCandidates"], ["builder", "bd-prod"])

    def test_import_admission_and_planning_never_hydrate_historical_logs(self):
        srv._mutate_control_state(lambda state: state.setdefault("buildRuns", {}).update({
            "old-history": {"id": "old-history", "status": "succeeded", "events": [{"message": "old log"}]},
        }))
        decode = database.decode_payload

        def guard(conn, payload, *identity):
            if "old-history" in identity:
                raise AssertionError("builder selection read unrelated build history")
            return decode(conn, payload, *identity)

        with patch.object(database, "decode_payload", side_effect=guard):
            accepted = srv.handle_build_deploy(self.token, {
                "repoUrl": "https://git.example.com/acme/app.git", "queue": True,
            })
            build_id = accepted["buildRunId"]
            self.assertEqual(accepted["run"]["status"], "queued")
            srv._mutate_control_state(lambda state: state["nodes"]["lab"]["agent"].update(arch="arm64"))
            with patch.object(srv, "_run_node_agent_task", return_value={
                    "kind": "build-plan", "commit": "a" * 40, "platforms": ["linux/arm64"],
                    "buildCapabilities": CAPABILITIES,
            }):
                planned = srv.handle_build_deploy(self.token, {
                    "repoUrl": "https://git.example.com/acme/app.git", "queue": True,
                }, _queued_run_id=build_id)
            self.assertEqual(planned["requeueBuild"]["candidates"], ["lab"])

    def test_scheduling_snapshot_retains_live_owners_and_completed_child_receipts(self):
        def prepare(state):
            state["buildRuns"] = {
                "unwinding": {"id": "unwinding", "status": "failed", "queueExecuting": True,
                              "buildNode": "builder", "agentTaskId": "child-running"},
                "finishing": {"id": "finishing", "status": "running", "queueExecuting": True,
                              "buildNode": "bd-prod", "agentTaskId": "child-done"},
            }
            state["buildQueue"] = {"unwinding": {"kind": "remote"}, "finishing": {"kind": "remote"}}
            state["agentTasks"] = {
                "child-running": {"id": "child-running", "status": "running", "nodeName": "builder",
                                  "buildRunId": "unwinding", "progress": [{"line": "still stopping"}]},
                "child-done": {"id": "child-done", "status": "succeeded", "nodeName": "bd-prod",
                               "buildRunId": "finishing", "progress": [{"line": "done"}]},
            }

        srv._mutate_control_state(prepare)
        with patch.object(database, "read_entity", side_effect=AssertionError("scheduling read a log stream")):
            state = queue.scheduling_state()
        self.assertEqual(srv._busy_build_nodes(state), {"builder"})
        self.assertEqual(srv._busy_build_nodes(state, exclude_run="unwinding"), set())
        self.assertEqual(state["agentTasks"]["child-done"]["status"], "succeeded")
        self.assertNotIn("progress", state["agentTasks"]["child-running"])

    def test_no_ready_candidate_rejects_pooled_dispatch(self):
        srv._mutate_control_state(lambda state: [state["nodes"][name]["agent"].update(status="offline") for name in ("builder", "bd-prod")])
        with self.assertRaisesRegex(LumaError, "no node agent is ready in builder pool"):
            self.pooled_task(["builder", "bd-prod"])


if __name__ == "__main__":
    unittest.main()
