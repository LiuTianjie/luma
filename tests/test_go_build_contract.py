"""Go Control queue to Agent protocol to Nomad deployment, on isolated fixtures.

Python drives assertions only; every Control mutation and build handoff runs in
Go. The Agent's real Git/BuildKit executor is verified separately.
"""
import json
import subprocess
import shutil
import time
import unittest

from luma.control.state import load_state, save_state
from tests import test_go_control_agent_contract as agents
from tests import test_go_deployment_contract as deployments

SHA = "1234567"  # Agent build result uses git rev-parse --short HEAD.


@unittest.skipUnless(shutil.which("go") and shutil.which("openssl"), "Go and openssl required")
class GoBuildContracts(unittest.TestCase):
    setUpClass = classmethod(agents.GoControlAgentContracts.setUpClass.__func__)
    task = agents.GoControlAgentContracts.task
    start = agents.GoControlAgentContracts.start
    stop = agents.GoControlAgentContracts.stop
    request = agents.GoControlAgentContracts.request
    stored = agents.GoControlAgentContracts.stored
    argv = deployments.GoDeploymentContracts.argv

    def setUp(self):
        deployments.GoDeploymentContracts.setUp(self)
        state = load_state()
        state["nodes"]["worker"].update(region="global", os="linux", arch="arm64")
        state["nodes"]["worker"]["agent"].update(capabilities=["docker-build"], supportedActions=["build-image"])
        state["build"] = dict(defaultNode="worker", registryHost="registry.test:5000", pushHost="localhost:5000")
        state["secrets"] = dict(GITHUB_TOKEN="fixture-private-git")
        state["registries"] = {"registry.test:5000": dict(username="robot", password="fixture-private-registry")}
        save_state(state)
        self.presence.update(capabilities=["docker-build"], supportedActions=["build-image"])

    def lease(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status, leased = self.request("/v1/node-agent/lease", self.presence)
            self.assertEqual(status, 200, leased)
            if leased.get("task"):
                return leased["task"]
            time.sleep(.05)
        self.fail("build task was never leased")

    def await_run(self, run_id, expected="succeeded"):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = self.stored("buildRuns", run_id)
            if run["status"] in {"succeeded", "failed", "canceled"}:
                self.assertEqual(run["status"], expected, run)
                return run
            time.sleep(.05)
        self.fail("build did not complete: " + str(run))

    def test_native_build_queue_handoff_deploy_and_credentials(self):
        self.start()
        status, accepted = self.request("/v1/builds", dict(repoUrl="team/app", queue=True, envSecrets={}), agents.MANAGEMENT)
        self.assertEqual(status, 200, accepted)
        run_id = accepted["buildRunId"]
        task = self.lease()
        payload = task["payload"]
        self.assertEqual(payload["repoUrl"], "https://github.com/team/app")
        self.assertEqual(payload["gitToken"], "fixture-private-git")
        self.assertEqual(payload["registryAuth"]["password"], "fixture-private-registry")
        self.assertEqual(payload["targetPlatformsByNode"]["worker"], "linux/arm64")
        self.assertNotIn("fixture-private", json.dumps(self.stored("agentTasks", task["id"])))
        image = "registry.test:5000/team/app:" + SHA
        built = dict(kind="service", image=image, sha=SHA,
                     manifest="name: built-app\nregion: global\nexposure: none\nbuild: {context: .}\n")
        status, result = self.request("/v1/node-agent/tasks/complete", dict(**self.presence, taskId=task["id"], status="succeeded", result=built))
        self.assertEqual(status, 200, result)
        run = self.await_run(run_id)
        self.assertEqual(run["result"]["image"], image)
        self.assertFalse(run["queueExecuting"])
        job = next(iter(self.nomad.jobs.values()))
        self.assertEqual(job["TaskGroups"][0]["Tasks"][0]["Config"]["image"], image)
        self.assertEqual(len(self.nomad.submissions), 1)
        self.stop()
        state = load_state()
        self.assertNotIn(run_id, state["buildQueue"])
        self.assertNotIn("build:", state["deployments"]["services"]["built-app"]["manifest"])

    def test_compose_uses_built_result_instead_of_stale_caller_images(self):
        self.start()
        stale = "services:\n  web: {image: retired.test/web:old}\n"
        status, accepted = self.request("/v1/builds", dict(repoUrl="team/stack", queue=True, composeContent=stale), agents.MANAGEMENT)
        self.assertEqual(status, 200, accepted)
        task = self.lease()
        image = "registry.test:5000/team/stack/web:" + SHA
        compose = "services:\n  web: {image: '" + image + "'}\n  worker: {image: '" + image + "'}\n"
        built = dict(kind="compose", image=image, images={"web": image}, imageAliases={"source:web": image}, sha=SHA,
                     manifest="name: built-stack\nregion: global\nservices:\n  web: {exposure: none}\n  worker: {exposure: none}\n",
                     composeContent=compose)
        status, result = self.request("/v1/node-agent/tasks/complete", dict(**self.presence, taskId=task["id"], status="succeeded", result=built))
        self.assertEqual(status, 200, result)
        run = self.await_run(accepted["buildRunId"])
        self.assertEqual(run["result"]["images"], {"web": image})
        tasks = [t for job in self.nomad.jobs.values() for g in job["TaskGroups"] for t in g["Tasks"]]
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t["Config"]["image"] == image for t in tasks))
        status, updated = self.request("/v1/applications/update", dict(stack="built-stack", queue=True), agents.MANAGEMENT)
        self.assertEqual(status, 200, updated)
        task = self.lease()
        status, result = self.request("/v1/node-agent/tasks/complete", dict(**self.presence, taskId=task["id"], status="succeeded", result=built))
        self.assertEqual(status, 200, result)
        run = self.await_run(updated["buildRunId"])
        self.assertEqual(run["request"]["expectedName"], "built-stack")
        self.assertEqual(run["result"]["deployment"], "built-stack")

    def test_cancel_running_build_stops_before_nomad(self):
        self.start()
        status, accepted = self.request("/v1/builds", dict(repoUrl="team/app", queue=True), agents.MANAGEMENT)
        self.assertEqual(status, 200, accepted)
        task = self.lease()
        status, canceled = self.request("/v1/builds/" + accepted["buildRunId"] + "/cancel", {}, agents.MANAGEMENT)
        self.assertEqual(status, 200, canceled)
        presence = {**self.presence, "activeTaskId": task["id"]}
        status, heartbeat = self.request("/v1/node-agent/heartbeat", presence)
        self.assertEqual(status, 200, heartbeat)
        self.assertTrue(heartbeat["cancelRequested"])
        status, completed = self.request("/v1/node-agent/tasks/complete", dict(**presence, taskId=task["id"], status="canceled", result={}))
        self.assertEqual(status, 200, completed)
        self.await_run(accepted["buildRunId"], "canceled")
        self.assertEqual(self.nomad.submissions, [])

    def test_go_cli_import_workflow_and_build_log(self):
        self.start()
        command = [str(self.cli), "--control-url", self.endpoint, "--token", agents.MANAGEMENT,
                   "--insecure", "--no-env", "--format", "json"]
        process = subprocess.Popen(command + ["import", "team/app", "--proxy-mode", "direct", "--timeout", "15"],
                                   cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        task = self.lease()
        status, progress = self.request("/v1/node-agent/tasks/progress", dict(nodeName="worker", nodeId="node-1", taskId=task["id"],
                                       events=[dict(type="output", line="real protocol build log", ts=int(time.time()))]))
        self.assertEqual(status, 200, progress)
        image = "registry.test:5000/team/app:" + SHA
        built = dict(kind="service", image=image, sha=SHA, manifest="name: cli-built-app\nregion: global\nexposure: none\nbuild: {}\n")
        status, completion = self.request("/v1/node-agent/tasks/complete", dict(**self.presence, taskId=task["id"], status="succeeded", result=built))
        self.assertEqual(status, 200, completion)
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        result = json.loads(stdout)["result"]
        self.assertEqual(result["image"], image)
        logs = subprocess.run(command + ["build", "logs", result["buildRunId"]], cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(logs.returncode, 0, logs.stdout + logs.stderr)
        self.assertIn("real protocol build log", logs.stdout)
        rows = subprocess.run(command + ["build", "list", "--app", "cli-built-app"], cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(rows.returncode, 0, rows.stdout + rows.stderr)
        self.assertIn(result["buildRunId"], rows.stdout)
        self.stop()
        state = load_state()
        record = state["deployments"]["services"]["cli-built-app"]
        self.assertEqual(record["gitSource"]["buildRunId"], result["buildRunId"])
        self.assertEqual(record["gitSource"]["proxyMode"], "direct")
        self.assertEqual(record["gitSource"]["revision"], SHA)
        workflow = state["deploymentWorkflows"]["cli-built-app"]
        self.assertEqual(workflow["recipe"]["method"], "remote-build")
        self.assertNotIn("fixture-private", json.dumps(workflow))

    def complete_native(self, name="rebuilt-app"):
        task = self.lease()
        image = "registry.test:5000/team/app:" + SHA
        built = dict(kind="service", image=image, sha=SHA,
                     manifest=f"name: {name}\nregion: global\nexposure: none\nbuild: {{}}\n")
        status, result = self.request("/v1/node-agent/tasks/complete", dict(**self.presence, taskId=task["id"], status="succeeded", result=built))
        self.assertEqual(status, 200, result)
        return task

    def test_retry_lineage_immutable_parent_cli_and_git_update(self):
        self.start()
        status, accepted = self.request("/v1/builds", dict(repoUrl="team/app", queue=True, proxyMode="direct"), agents.MANAGEMENT)
        self.assertEqual(status, 200, accepted)
        parent_id = accepted["buildRunId"]
        task = self.lease()
        status, rejected = self.request(f"/v1/builds/{parent_id}/retry", dict(queue=True), agents.MANAGEMENT)
        self.assertEqual(status, 409, rejected)
        built = dict(kind="service", image="registry.test:5000/team/app:"+SHA, sha=SHA,
                     manifest="name: rebuilt-app\nregion: global\nexposure: none\nbuild: {}\n")
        status, result = self.request("/v1/node-agent/tasks/complete", dict(**self.presence, taskId=task["id"], status="succeeded", result=built))
        self.assertEqual(status, 200, result)
        parent = self.await_run(parent_id)
        command = [str(self.cli), "--control-url", self.endpoint, "--token", agents.MANAGEMENT,
                   "--insecure", "--no-env", "--format", "json"]
        process = subprocess.Popen(command + ["build", "retry", parent_id, "--timeout", "15"], cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.complete_native()
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stdout+stderr)
        result = json.loads(stdout)["result"]
        retried = self.await_run(result["buildRunId"])
        self.assertEqual(retried["retryOf"], parent_id)
        self.assertEqual(retried["retryRootId"], parent_id)
        self.assertEqual(retried["request"]["expectedName"], "rebuilt-app")
        self.assertTrue(retried["result"]["workflow"]["saved"])
        self.assertEqual(self.stored("buildRuns", parent_id), parent)
        status, next_retry = self.request(f"/v1/builds/{retried['id']}/retry", dict(queue=True), agents.MANAGEMENT)
        self.assertEqual(status, 200, next_retry)
        self.complete_native()
        child = self.await_run(next_retry["buildRunId"])
        self.assertEqual(child["retryOf"], retried["id"])
        self.assertEqual(child["retryRootId"], parent_id)
        status, update = self.request("/v1/applications/update", dict(name="rebuilt-app", queue=True), agents.MANAGEMENT)
        self.assertEqual(status, 200, update)
        task = self.complete_native()
        self.assertEqual(task["payload"]["repoUrl"], "https://github.com/team/app")
        updated = self.await_run(update["buildRunId"])
        self.assertEqual(updated["request"]["proxyMode"], "direct")
        self.assertEqual(updated["request"]["expectedName"], "rebuilt-app")
        # Changing the repository's manifest name must never create a second app.
        status, wrong = self.request(f"/v1/builds/{parent_id}/retry", dict(queue=True), agents.MANAGEMENT)
        self.assertEqual(status, 200, wrong)
        count = len(self.nomad.submissions)
        self.complete_native("different-app")
        self.await_run(wrong["buildRunId"], "failed")
        self.assertEqual(len(self.nomad.submissions), count)

    def test_control_records_queued_workflow_without_cli_and_preserves_newer_revision(self):
        self.start()
        recipe = dict(schemaVersion=1, method="remote-build", argv=["import", "team/app"])
        selector = dict(name="rebuilt-app", repoUrl="team/app")
        status, accepted = self.request("/v1/builds", dict(repoUrl="team/app", queue=True,
            workflow=dict(recipe=recipe, selector=selector, expectedRevision=0)), agents.MANAGEMENT)
        self.assertEqual(status, 200, accepted)
        # No observing CLI or workflow POST follows: the worker owns recording.
        self.complete_native()
        run = self.await_run(accepted["buildRunId"])
        self.assertTrue(run["result"]["workflow"]["saved"])
        status, stored = self.request("/v1/workflows/rebuilt-app", None, token=agents.MANAGEMENT)
        self.assertEqual(status, 200, stored)
        first = stored["workflow"]
        status, accepted = self.request("/v1/builds", dict(repoUrl="team/app", queue=True,
            workflow=dict(recipe=recipe, selector=selector, expectedRevision=first["revision"])), agents.MANAGEMENT)
        self.assertEqual(status, 200, accepted)
        # A newer intentional edit while this build is running wins.
        newer = dict(schemaVersion=1, method="remote-build", argv=["import", "team/app", "--ref", "newer"])
        status, edited = self.request("/v1/workflows", dict(name="rebuilt-app", recipe=newer,
            selector=selector, source="manual", expectedRevision=first["revision"]), agents.MANAGEMENT)
        self.assertEqual(status, 200, edited)
        self.complete_native()
        run = self.await_run(accepted["buildRunId"])
        self.assertFalse(run["result"]["workflow"]["saved"])
        self.assertTrue(run["result"]["workflow"]["warning"])
        status, stored = self.request("/v1/workflows/rebuilt-app", None, token=agents.MANAGEMENT)
        self.assertEqual(status, 200, stored)
        self.assertEqual(stored["workflow"]["recipe"], newer)
        status, stale = self.request("/v1/builds", dict(repoUrl="team/app", queue=True,
            workflow=dict(recipe=recipe, selector=selector, expectedRevision=first["revision"], acceptChange=True)), agents.MANAGEMENT)
        self.assertEqual(status, 409, stale)

    def test_nonqueue_stream_contains_agent_progress(self):
        import concurrent.futures
        import ssl
        import urllib.request
        self.start()
        def stream():
            request = urllib.request.Request(self.endpoint+"/v1/builds/stream",
                data=json.dumps(dict(repoUrl="team/app")).encode(),
                headers={"Authorization":"Bearer "+agents.MANAGEMENT, "Content-Type":"application/json"})
            with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=15) as response:
                return [json.loads(line) for line in response if line.strip()]
        with concurrent.futures.ThreadPoolExecutor() as pool:
            response = pool.submit(stream)
            task = self.lease()
            for start in range(0, 120, 40):
                status, progress = self.request("/v1/node-agent/tasks/progress", dict(nodeName="worker", nodeId="node-1", taskId=task["id"],
                    events=[dict(type="output", line=f"compiler step {i}", ts=int(time.time())) for i in range(start, start+40)]))
                self.assertEqual(status, 200, progress)
            built = dict(kind="service", image="registry.test:5000/team/app:"+SHA, sha=SHA,
                         manifest="name: stream-app\nregion: global\nexposure: none\nbuild: {}\n")
            status, completed = self.request("/v1/node-agent/tasks/complete", dict(**self.presence, taskId=task["id"], status="succeeded", result=built))
            self.assertEqual(status, 200, completed)
            events = response.result(timeout=20)
        logs = [e["message"] for e in events if e.get("message", "").startswith("compiler step ")]
        self.assertEqual(logs, [f"compiler step {i}" for i in range(120)])
        self.assertEqual(events[-1]["status"], "done")
        self.assertEqual(events[-1]["result"]["service"], "stream-app")

    def test_local_retry_and_direct_image_update_are_rejected(self):
        state = load_state()
        state["buildRuns"] = {"local-attempt": dict(id="local-attempt", mode="local", status="succeeded", source="team/app", request={})}
        state["deployments"] = {"services": {"image-only": dict(name="image-only", manifest="name: image-only\nimage: demo:1\n")}}
        save_state(state)
        self.start()
        status, result = self.request("/v1/builds/local-attempt/retry", dict(queue=True), agents.MANAGEMENT)
        self.assertEqual(status, 409, result)
        status, result = self.request("/v1/applications/update", dict(name="image-only", queue=True), agents.MANAGEMENT)
        self.assertEqual(status, 400, result)
        self.assertEqual(self.nomad.submissions, [])

    def test_go_cli_build_config_controls_next_build_and_read_only(self):
        state = load_state()
        state["nodes"]["worker"]["region"] = "cn"
        state["nomadServerAddr"] = "100.64.0.1"
        state["build"]["extension"] = {"keep": True}
        save_state(state)
        self.start()
        def cli(*args):
            command = [str(self.cli), "--control-url", self.endpoint, "--token", agents.MANAGEMENT,
                       "--insecure", "--no-env", "--format", "json", "build", "config", *args]
            result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            return json.loads(result.stdout)["result"]["build"]
        config = cli("--node", "Node One", "--node", "worker", "--default-node", "old-worker",
                     "--direct-egress-node", "node-1", "--registry-host", "registry.test:5000", "--push-host", "localhost:5000")
        self.assertEqual(config["defaultNode"], "worker")
        self.assertEqual(config["directEgressNodes"], ["worker"])
        self.assertEqual([n["name"] for n in config["nodes"]], ["worker"])
        self.assertEqual(cli(), config)
        for expected_proxy in ["", "http://100.64.0.1:7890"]:
            if expected_proxy:
                config = cli("--clear-direct-egress")
                self.assertEqual(config["directEgressNodes"], [])
            status, accepted = self.request("/v1/builds", dict(repoUrl="team/app", queue=True), agents.MANAGEMENT)
            self.assertEqual(status, 200, accepted)
            task = self.lease()
            self.assertEqual(task["payload"].get("proxy", ""), expected_proxy)
            self.assertEqual(task["payload"]["registryHost"], "registry.test:5000")
            status, complete = self.request("/v1/node-agent/tasks/complete", dict(**self.presence, taskId=task["id"], status="failed", result={"message":"fixture completed configuration inspection"}))
            self.assertEqual(status, 200, complete)
            self.await_run(accepted["buildRunId"], "failed")
        status, rejected = self.request("/v1/builds/config", dict(defaultNode="missing", registryHost="changed.test"), agents.MANAGEMENT)
        self.assertEqual(status, 400, rejected)
        self.assertEqual(cli()["registryHost"], "registry.test:5000")
        status, rejected = self.request("/v1/builds/config", dict(defaultNode="worker"), agents.TOKEN)
        self.assertEqual(status, 401, rejected)
        self.stop()
        state = load_state()
        self.assertEqual(state["build"]["extension"], {"keep": True})
        self.assertEqual(state["build"]["nodes"], ["worker"])
        self.start("--read-only")
        self.assertEqual(cli()["defaultNode"], "worker")
        status, rejected = self.request("/v1/builds/config", dict(defaultNode="worker"), agents.MANAGEMENT)
        self.assertEqual(status, 501, rejected)
        self.assertEqual(self.nomad.submissions, [])

    def test_local_native_and_compose_reservations_deploy_without_build_agent(self):
        state = load_state()
        state["nodes"]["worker"]["agent"].update(capabilities=[], supportedActions=[])
        save_state(state)
        self.start()
        for kind in ["service", "compose"]:
            name = "local-" + kind
            recipe = dict(schemaVersion=1, method="local-build", argv=["build", "local", ".", "--repo-url", "https://github.com/team/app"])
            request = dict(repoUrl="team/app", sourceRevision="abc1234-dirty", targetNodes=["worker"], expectedName=name,
                           workflow=dict(recipe=recipe, selector=dict(name=name, repoUrl="team/app"), expectedRevision=0))
            status, prepared = self.request("/v1/builds/local/prepare", request, agents.MANAGEMENT)
            self.assertEqual(status, 200, prepared)
            upload = prepared["upload"]
            self.assertEqual(upload["platform"], "linux/arm64")
            self.assertEqual(upload["repository"], "team/app")
            self.assertEqual(prepared["run"]["mode"], "local")
            run_id = prepared["run"]["id"]
            built = dict(kind=kind, image=upload["image"], sha=upload["tag"], manifest=f"name: {name}\nregion: global\nexposure: none\nbuild: {{}}\n")
            if kind == "compose":
                built.update(manifest=f"name: {name}\nregion: global\nservices:\n  web: {{exposure: none}}\n  worker: {{exposure: none}}\n",
                             images={"web": upload["image"]}, imageAliases={"shared:dev": upload["image"]},
                             composeContent="services:\n  web: {image: shared:dev}\n  worker: {image: shared:dev}\n")
            # Validation failures leave the reservation usable and submit no job.
            before = len(self.nomad.submissions)
            for altered in [dict(image="foreign.test/team/app:"+upload["tag"]), dict(image=upload["image"].replace("team/app", "team/other")),
                            dict(image=upload["image"]+"@sha256:"+"a"*64), dict(manifest="name: unrelated\nregion: global\n")]:
                status, rejected = self.request(f"/v1/builds/local/{run_id}/complete", dict(queue=True, buildResult={**built, **altered}), agents.MANAGEMENT)
                self.assertEqual(status, 400, rejected)
                self.assertEqual(len(self.nomad.submissions), before)
            status, accepted = self.request(f"/v1/builds/local/{run_id}/complete", dict(queue=True, buildResult=built, envSecrets={}), agents.MANAGEMENT)
            self.assertEqual(status, 200, accepted)
            run = self.await_run(run_id)
            self.assertEqual(run["result"]["revision"], "abc1234-dirty")
            self.assertTrue(run["result"]["workflow"]["saved"])
            self.assertNotIn("agentTaskId", run)
            count = len(self.nomad.submissions)
            status, duplicate = self.request(f"/v1/builds/local/{run_id}/complete", dict(queue=True, buildResult=built), agents.MANAGEMENT)
            self.assertEqual(status, 409, duplicate)
            self.assertEqual(len(self.nomad.submissions), count)
        self.stop()
        state = load_state()
        self.assertEqual(state["agentTasks"], {})
        self.assertEqual(state["deployments"]["services"]["local-service"]["gitSource"]["revision"], "abc1234-dirty")
        self.assertEqual(state["deployments"]["compose"]["local-compose"]["gitSource"]["repoUrl"], "https://github.com/team/app")
