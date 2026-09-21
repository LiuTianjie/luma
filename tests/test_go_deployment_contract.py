"""Go CLI -> TLS Go Control -> Nomad HTTP contract -> existing SQLite schema."""
from __future__ import annotations

import copy
import concurrent.futures
import http.server
import json
import shutil
import ssl
import subprocess
import threading
import os
import pty
import select
import signal
import socket
import yaml
import termios
import time
import unittest
from unittest.mock import patch

from luma.control.state import load_state, save_state
from tests import test_go_control_agent_contract as fixtures


class NomadFixture:
    def __init__(self):
        self.jobs = {}
        self.submissions = []
        self.scheduler = {"MemoryOversubscriptionEnabled": False, "SchedulerAlgorithm": "binpack"}
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, value, code=200):
                raw = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def body(self):
                return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

            def do_GET(self):
                if self.path == "/v1/operator/scheduler/configuration":
                    return self.reply({"SchedulerConfig": owner.scheduler})
                parts = self.path.split("/")
                kind, name = parts[2:4]
                if kind == "evaluation":
                    job = owner.jobs[name.removeprefix("eval-")]
                    return self.reply(dict(ID=name, JobID=job["ID"], JobModifyIndex=job["JobModifyIndex"],
                                           Status="complete", DeploymentID="rollout-" + job["ID"]))
                if kind == "deployment":
                    job = owner.jobs[name.removeprefix("rollout-")]
                    return self.reply(dict(ID=name, JobID=job["ID"], JobModifyIndex=job["JobModifyIndex"],
                                           JobVersion=job["Version"], Status="successful",
                                           TaskGroups={g["Name"]: {"DesiredTotal": g["Count"]} for g in job["TaskGroups"]}))
                if kind != "job" or name not in owner.jobs:
                    return self.reply({}, 404)
                job = owner.jobs[name]
                if len(parts) == 4:
                    return self.reply(job)
                if parts[4] == "allocations":
                    return self.reply([dict(ID="alloc-" + name + g["Name"] + str(i), NodeID="node-1", JobID=name,
                                            TaskGroup=g["Name"], JobVersion=job["Version"], CreateIndex=job["JobModifyIndex"],
                                            DesiredStatus="run", ClientStatus="running", DeploymentStatus={"Healthy": True},
                                            TaskStates={t["Name"]: {"State": "running", "Failed": False} for t in g["Tasks"]})
                                       for g in job["TaskGroups"] for i in range(g["Count"])])
                return self.reply([])

            def do_PUT(self):
                owner.scheduler = self.body()
                self.reply({"Updated": True})

            def do_POST(self):
                if self.path != "/v1/jobs":
                    return self.reply({}, 404)
                body = self.body()
                job = body["Job"]
                previous = owner.jobs.get(job["ID"], {})
                if not body.get("EnforceIndex") or body["JobModifyIndex"] != previous.get("JobModifyIndex", 0):
                    return self.reply({}, 409)
                owner.submissions.append(copy.deepcopy(body))
                job.update(JobModifyIndex=100 + len(owner.submissions), Version=previous.get("Version", -1) + 1)
                owner.jobs[job["ID"]] = job
                self.reply(dict(EvalID="eval-" + job["ID"], JobModifyIndex=job["JobModifyIndex"]))

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@unittest.skipUnless(shutil.which("go") and shutil.which("openssl"), "Go and openssl required")
class GoDeploymentContracts(unittest.TestCase):
    # Share only TLS/process/state helpers; do not inherit unrelated Agent tests.
    setUpClass = classmethod(fixtures.GoControlAgentContracts.setUpClass.__func__)
    task = fixtures.GoControlAgentContracts.task
    start = fixtures.GoControlAgentContracts.start
    stop = fixtures.GoControlAgentContracts.stop
    request = fixtures.GoControlAgentContracts.request

    def setUp(self):
        fixtures.GoControlAgentContracts.setUp(self)
        state = load_state()
        state["agentTasks"] = {}
        state["nodes"]["worker"].update(region="cn", nomadStatus="ready")
        state["nodes"]["worker"]["agent"]["status"] = "ready"
        save_state(state)
        self.nomad = NomadFixture()
        self.addCleanup(self.nomad.close)

    def argv(self, mode="--agent-tasks"):
        if mode == "--read-only":
            return fixtures.GoControlAgentContracts.argv(self, mode)
        return fixtures.GoControlAgentContracts.argv(self, mode) + ["--nomad-url", self.nomad.url, "--rollout-timeout", "5s"] + (["--config", str(self.control_config)] if hasattr(self, "control_config") else []) + (["--route-ca-file", str(self.route_ca)] if hasattr(self, "route_ca") else [])

    def cli_call(self, *args):
        result = subprocess.run([str(self.cli), "--control-url", self.endpoint, "--token", fixtures.MANAGEMENT,
                                 "--insecure", "--no-env", "--format", "json", *args],
                                cwd=self.root, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)["result"]

    def test_native_preview_deploy_and_existing_data_adoption(self):
        manifest = "name: internal-db\nregion: cn\nexposure: none\nimage: busybox:1.36\nvolumes: [fixture-data:/data]\n"
        self.start()
        request = dict(manifest=manifest, sourceName="db.yml", origin="dashboard")
        status, preview = self.request("/v1/deployments/preview", request, fixtures.MANAGEMENT)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["localStorage"]["nodeId"], "node-1")
        self.assertEqual(self.nomad.submissions, [])
        status, result = self.request("/v1/deployments", request, fixtures.MANAGEMENT)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["orchestrator"]["healthyAllocations"], 1)
        self.assertEqual(len(self.nomad.submissions), 1)
        constraints = self.nomad.submissions[0]["Job"]["Constraints"]
        self.assertIn({"LTarget": "${node.unique.id}", "RTarget": "node-1", "Operand": "="}, constraints)
        self.stop()
        state = load_state()
        record = state["deployments"]["services"]["internal-db"]
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["localStorage"]["mounts"][0]["source"], "fixture-data")
        event = next(x for x in state["deploymentEvents"] if x["id"] == result["operationId"])
        self.assertEqual(event["origin"], "dashboard")
        # Existing Python-era manifest has no persisted ownership: recover it from
        # the running Nomad job, then preserve the same node and named volume.
        for key in ("node", "nodeId", "localStorage", "runtime"):
            record.pop(key, None)
        record["manifest"] = manifest
        save_state(state)
        self.start()
        path = self.root / "db.yml"
        path.write_text(manifest)
        deployed = self.cli_call("deploy", str(path))
        self.assertEqual(deployed["orchestrator"]["jobVersion"], 1)
        self.assertEqual(len(self.nomad.submissions), 2)
        self.assertEqual(self.nomad.submissions[1]["JobModifyIndex"], 101)
        for changed in (manifest.replace("fixture-data:", "empty-data:"), manifest + "node: other\n"):
            status, _ = self.request("/v1/deployments", dict(manifest=changed), fixtures.MANAGEMENT)
            self.assertEqual(status, 409)
        self.assertEqual(len(self.nomad.submissions), 2)

    def test_compose_cli_records_local_path_and_services(self):
        sidecar = self.root / "luma.compose.yml"
        sidecar.write_text("name: internal-stack\nregion: cn\nvolumes:\n  data:\n    local:\n      path: /srv/fixture-data\n    adopted: true\n")
        (self.root / "docker-compose.yml").write_text("services:\n  db:\n    image: busybox:1.36\n    volumes: [data:/data]\n  cache:\n    image: busybox:1.36\n")
        state = load_state()
        state["nodes"]["worker"]["agent"].update(capabilities=["managed-volume-path"], supportedActions=["prepare-managed-volume-path"])
        save_state(state)
        self.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(self.cli_call, "compose", "deploy", str(sidecar))
            status, lease = self.request("/v1/node-agent/lease", dict(self.presence, waitSeconds=2))
            self.assertEqual(status, 200, lease)
            task = lease["task"]
            self.assertEqual(task["action"], "prepare-managed-volume-path")
            self.assertEqual(task["payload"], dict(root="/srv", relative="fixture-data", preserveExisting=True))
            self.assertEqual(self.nomad.submissions, [])
            status, completed = self.request("/v1/node-agent/tasks/complete", dict(nodeName="worker", nodeId="node-1", taskId=task["id"], status="succeeded", result=dict(path="/srv/fixture-data")))
            self.assertEqual(status, 200, completed)
            result = work.result(timeout=5)
        self.assertEqual(result["orchestrator"]["healthyAllocations"], 1)
        self.assertEqual(len(self.nomad.submissions), 1)
        self.stop()
        record = load_state()["deployments"]["compose"]["internal-stack"]
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["localStorage"]["nodeId"], "node-1")
        self.assertEqual(record["localStorage"]["mounts"], [dict(service="db", type="bind", source="/srv/fixture-data", target="/data")])
        self.assertEqual(record["storagePrepared"][0]["path"], "/srv/fixture-data")
        self.assertEqual({t["Name"] for t in self.nomad.jobs["internal-stack"]["TaskGroups"][0]["Tasks"]}, {"db", "cache"})

    def test_requirements_preview_and_enforcement_do_not_expose_secret_values(self):
        manifest = "name: prerequisites\nregion: cn\nimage: busybox:1.36\nrequirements:\n  capabilities: [egress, registry, tailscale]\n  secrets: [APP_TOKEN]\n  init: [egress, registry, tailscale-node, secrets]\n  notes: [requires-private-runtime]\nenv:\n  TOKEN: ${APP_TOKEN}\n"
        self.start()
        body = dict(manifest=manifest, envSecrets=dict(APP_TOKEN="fixture-app-token"))
        status, preview = self.request("/v1/deployments/preview", body, fixtures.MANAGEMENT)
        self.assertEqual(status, 200, preview)
        report = preview["requirements"]
        checks = {item["kind"]: item for item in report["checks"]}
        self.assertFalse(report["ready"])
        self.assertEqual(checks["secrets"]["status"], "ready")
        self.assertEqual(checks["egress"]["missing"], ["EGRESS_SUBSCRIPTION_URL"])
        self.assertEqual(checks["tailscale"]["status"], "missing")
        self.assertNotIn("fixture-app-token", json.dumps(preview))
        status, error = self.request("/v1/deployments", body, fixtures.MANAGEMENT)
        self.assertEqual(status, 400, error)
        self.assertEqual(self.nomad.submissions, [])
        self.stop()
        state = load_state()
        state.setdefault("secrets", {})["EGRESS_SUBSCRIPTION_URL"] = "private-subscription-fixture"
        state["registries"]["docker.io"] = dict(username="fixture", password="private-hub-fixture")
        state["nodes"]["worker"]["tailscaleIP"] = "100.64.0.1"
        save_state(state)
        self.start()
        status, response = self.request("/v1/deployments", dict(body, origin="dashboard"), fixtures.MANAGEMENT)
        self.assertEqual(status, 400, response)
        self.assertIn("setup check", response["error"])
        self.assertEqual(self.nomad.submissions, [])
        status, response = self.request("/v1/deployments", body, fixtures.MANAGEMENT)
        self.assertEqual(status, 200, response)
        self.assertTrue(response["requirements"]["ready"])
        self.assertEqual(response["requirements"]["init"], ["egress", "registry", "tailscale-node", "secrets"])
        self.stop()
        state = load_state()
        state["setupChecks"] = dict(checks=dict(egress=dict(status="ready")))
        save_state(state)
        self.start()
        status, response = self.request("/v1/deployments", dict(body, origin="dashboard"), fixtures.MANAGEMENT)
        self.assertEqual(status, 200, response)
        for value in ("fixture-app-token", "private-subscription-fixture", "private-hub-fixture"):
            self.assertNotIn(value, json.dumps(response))

    def test_unported_integrations_do_not_submit(self):
        self.start()
        status, _ = self.request("/v1/deployments", dict(manifest="name: public\nregion: cn\nimage: nginx\nexposure: cn-edge\ndomain: fixture.test\nport: 80\n"), fixtures.MANAGEMENT)
        self.assertEqual(status, 501)
        self.assertEqual(self.nomad.submissions, [])

    def test_edge_deployments_load_config_remove_shadows_and_probe_tls(self):
        route_cert, route_key = self.root / "route.pem", self.root / "route.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                        "-keyout", str(route_key), "-out", str(route_cert)], capture_output=True, check=True)
        class Route(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Route)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(route_cert, route_key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup_route():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(cleanup_route)
        self.route_ca = route_cert
        self.control_config = self.root / "control.yml"
        self.control_config.write_text("defaults:\n  stackRoot: jobs\n  routesRoot: routes\n  entrypoint: fixture-tls\n  certResolver: fixture-acme\nproviders:\n  dns:\n    provider: cloudflare\n")
        state = load_state()
        state["nodes"]["worker"]["nomadRole"] = "server"
        save_state(state)
        routes = self.root / "routes"
        routes.mkdir()
        native_shadow, compose_shadow = routes / "public.yml", routes / "public-stack-web.yml"
        unrelated = routes / "keep.yml"
        for path in (native_shadow, compose_shadow, unrelated):
            path.write_text("old route")
        domain = f"127.0.0.1:{server.server_port}"
        manifest = f"name: public\nregion: cn\nnode: worker\nimage: nginx:alpine\nexposure: cn-edge\ndomain: {domain}\nport: 80\n"
        source = self.root / "public.yml"
        source.write_text(manifest)
        self.start()
        status, preview = self.request("/v1/deployments/preview", dict(manifest=manifest, skipDns=True), fixtures.MANAGEMENT)
        self.assertEqual(status, 200, preview)
        self.assertEqual(native_shadow.read_text(), "old route")
        self.assertEqual(self.nomad.submissions, [])
        result = self.cli_call("deploy", str(source), "--skip-dns")
        self.assertEqual(result["health"]["status"], "ready")
        self.assertEqual(result["dns"], "DNS skipped: --skip-dns")
        self.assertFalse(native_shadow.exists())
        self.assertTrue((self.root / "jobs/cn/public/public.nomad.json").is_file())
        service = self.nomad.jobs["public"]["TaskGroups"][0]["Services"][0]
        self.assertEqual(service["AddressMode"], "host")
        self.assertIn("traefik.http.routers.public.entrypoints=fixture-tls", service["Tags"])
        self.assertIn("traefik.http.routers.public.tls.certresolver=fixture-acme", service["Tags"])
        sidecar = self.root / "luma.compose.yml"
        sidecar.write_text(f"name: public-stack\nregion: cn\nservices:\n  web:\n    node: worker\n    exposure: cn-edge\n    domain: {domain}\n    port: 80\n")
        (self.root / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx:alpine\n")
        composed = self.cli_call("compose", "deploy", str(sidecar), "--skip-dns")
        self.assertEqual(composed["health"][0]["status"], "ready")
        self.assertIsInstance(composed["dns"], list)
        self.assertFalse(compose_shadow.exists())
        self.assertEqual(unrelated.read_text(), "old route")
        self.stop()
        record = load_state()["deployments"]["services"]["public"]
        self.assertEqual(record["health"]["public"]["status"], "ready")

    def secret_cli(self, *args, input=None, ok=True):
        result = subprocess.run([str(self.cli), "--control-url", self.endpoint, "--token", fixtures.MANAGEMENT,
                                 "--insecure", "--no-env", "--format", "json", "secret", *args],
                                cwd=self.root, input=input, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stderr)
        return result

    def test_relay_routes_tcp_reservations_and_agent_network_preparation(self):
        self.control_config = self.root / "control.yml"
        self.control_config.write_text("defaults:\n  stackRoot: jobs\n  routesRoot: routes\n")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        state = load_state()
        state["nodes"]["worker"].update(tailscaleIP="100.64.0.7")
        state["nodes"]["worker"]["agent"].update(capabilities=["nomad-cni-repair"], supportedActions=["repair-nomad-cni-hostports"], os="linux")
        save_state(state)
        self.start()
        source = self.root / "relay.yml"
        manifest = f"name: relay\nregion: cn\nnode: old-worker\nexposure: tcp-relay\ndomain: 127.0.0.1\nport: 8080\npublishPort: {port}\nimage: busybox:1.36\n"
        source.write_text(manifest)
        def finish_network_task():
            status, leased = self.request("/v1/node-agent/lease", dict(nodeName="worker", nodeId="node-1", capabilities=["nomad-cni-repair"], supportedActions=["repair-nomad-cni-hostports"], os="linux", activeTaskId="", waitSeconds=2))
            self.assertEqual(status, 200, leased)
            task = leased["task"]
            self.assertEqual(task["action"], "repair-nomad-cni-hostports")
            self.assertEqual(task["payload"], {"ports": [port]})
            status, body = self.request("/v1/node-agent/tasks/complete", dict(nodeName="worker", nodeId="node-1", taskId=task["id"], status="succeeded", result=dict(deleted=0, hostPorts=[port])))
            self.assertEqual(status, 200, body)
        for attempt in range(2):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                work = pool.submit(self.cli_call, "deploy", str(source), "--skip-dns")
                finish_network_task()
                result = work.result(timeout=5)
            self.assertEqual(result["health"]["status"], "ready")
            self.assertEqual(result["health"]["kind"], "public-tcp")
            self.assertEqual(result["ingressRefreshPorts"], [port] if attempt == 0 else [])
            self.assertEqual(result["networkPrepared"][0]["status"], "completed")
        route = yaml.safe_load((self.root / "routes/relay.yml").read_text())
        self.assertEqual(route["tcp"]["services"]["relay"]["loadBalancer"]["servers"], [{"address": f"100.64.0.7:{port}"}])
        count = len(self.nomad.submissions)
        status, body = self.request("/v1/compose-deployments", dict(manifest=f"name: collision\nregion: cn\nservices:\n  app:\n    node: worker\n    exposure: tcp-relay\n    domain: 127.0.0.1\n    port: 8080\n    publishPort: {port}\n", composeContent="services:\n  app:\n    image: busybox:1.36\n", skipDns=True), fixtures.MANAGEMENT)
        self.assertEqual(status, 400, body)
        self.assertEqual(len(self.nomad.submissions), count)
        self.stop()
        record = load_state()["deployments"]["services"]["relay"]
        self.assertEqual(record["tcpRelayPorts"], [port])
        self.assertEqual(record["node"], "worker")

    def test_secret_management_cli_scope_import_and_read_only_names(self):
        self.start()
        sensitive = "credential-with-multiple\nlines"
        result = self.secret_cli("set", "TOKEN", "--scope", "My App", "--value-stdin", input=sensitive + "\n")
        self.assertNotIn(sensitive, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"], dict(name="TOKEN", scope="my-app", saved=True))
        env = self.root / "secrets.env"
        env.write_text("SECOND=another-private-value\nTHIRD=third-value\n")
        self.assertEqual(json.loads(self.secret_cli("import", str(env), "--scope", "My App").stdout)["result"]["imported"], 2)
        names = json.loads(self.secret_cli("list").stdout)["result"]["secrets"]
        self.assertEqual(names, ["my-app/SECOND", "my-app/THIRD", "my-app/TOKEN"])
        self.assertEqual(self.request("/v1/secrets", None, fixtures.TOKEN)[0], 401)
        self.assertTrue(json.loads(self.secret_cli("remove", "THIRD", "--scope", "My App").stdout)["result"]["removed"])
        self.stop()
        self.assertEqual(load_state()["scopedSecrets"]["my-app"]["TOKEN"], sensitive)
        self.start("--read-only")
        status, listed = self.request("/v1/secrets", None, fixtures.MANAGEMENT)
        self.assertEqual(status, 200)
        self.assertEqual(listed["secrets"], ["my-app/SECOND", "my-app/TOKEN"])
        self.assertEqual(self.request("/v1/secrets", dict(name="TOKEN", value="no"), fixtures.MANAGEMENT)[0], 501)

    def test_native_and_compose_deployment_secrets_stay_out_of_public_artifacts(self):
        state = load_state()
        state["secrets"] = dict(TOKEN="global-value")
        save_state(state)
        self.control_config = self.root / "control.yml"
        self.control_config.write_text("defaults:\n  stackRoot: jobs\n  routesRoot: routes\n")
        self.start()
        literal = 'scoped-value-with-"quotes"\n${UNCHANGED}'
        native = "name: secret-native\nregion: cn\nimage: busybox:1.36\nenv:\n  TOKEN: prefix:${TOKEN}\nrequirements:\n  secrets: [TOKEN]\n"
        request = dict(manifest=native, envSecrets=dict(TOKEN=literal, UNUSED="must-not-persist"))
        status, preview = self.request("/v1/deployments/preview", request, fixtures.MANAGEMENT)
        self.assertEqual(status, 200, preview)
        self.assertIn("${TOKEN}", preview["artifacts"][0]["content"])
        self.assertNotIn(literal, json.dumps(preview))
        status, result = self.request("/v1/deployments", request, fixtures.MANAGEMENT)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["secrets"]["imported"], 1)
        env = self.nomad.jobs["secret-native"]["TaskGroups"][0]["Tasks"][0]["Env"]
        self.assertEqual(env["TOKEN"], "prefix:" + literal)
        self.assertNotIn(literal, json.dumps(result))
        artifact = self.root / "jobs/cn/secret-native/secret-native.nomad.json"
        self.assertIn("${TOKEN}", artifact.read_text())
        self.assertNotIn("scoped-value", artifact.read_text())
        sidecar = self.root / "luma.compose.yml"
        sidecar.write_text("name: secret-compose\nregion: cn\n")
        (self.root / "docker-compose.yml").write_text("services:\n  db:\n    image: busybox:1.36\n    environment:\n      TOKEN: ${TOKEN}\n")
        incoming = self.root / "compose.env"
        incoming.write_text("TOKEN=compose-value\nUNUSED=discarded\n")
        composed = self.cli_call("compose", "deploy", str(sidecar), "--env", str(incoming))
        self.assertEqual(composed["secrets"]["scope"], "secret-compose")
        self.assertEqual(self.nomad.jobs["secret-compose"]["TaskGroups"][0]["Tasks"][0]["Env"]["TOKEN"], "compose-value")
        # A third application without an explicit env/scope keeps global compatibility.
        status, result = self.request("/v1/deployments", dict(manifest=native.replace("secret-native", "secret-global")), fixtures.MANAGEMENT)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.nomad.jobs["secret-global"]["TaskGroups"][0]["Tasks"][0]["Env"]["TOKEN"], "prefix:global-value")
        # An explicit empty env file must not silently opt into the global secret.
        empty = self.root / "empty.env"
        empty.write_text("")
        source = self.root / "fresh.yml"
        source.write_text(native.replace("secret-native", "secret-empty"))
        completed = subprocess.run([str(self.cli), "--control-url", self.endpoint, "--token", fixtures.MANAGEMENT,
                                    "--insecure", "--no-env", "--format", "json", "deploy", str(source), "--env", str(empty)],
                                   text=True, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("missing scoped deployment secrets", completed.stdout)
        self.assertNotIn("secret-empty", self.nomad.jobs)
        self.stop()
        state = load_state()
        self.assertEqual(state["scopedSecrets"]["secret-native"], dict(TOKEN=literal))
        self.assertEqual(state["scopedSecrets"]["secret-compose"], dict(TOKEN="compose-value"))
        for key in ("deployments", "deploymentEvents"):
            public = json.dumps(state[key])
            self.assertNotIn("scoped-value", public)
            self.assertNotIn("compose-value", public)
            self.assertNotIn("must-not-persist", public)

    def test_interactive_secret_prompt_hides_input_and_restores_terminal_on_cancel(self):
        self.start()
        for cancel in (False, True):
            master, slave = pty.openpty()
            try:
                before = termios.tcgetattr(slave)
                process = subprocess.Popen([str(self.cli), "--control-url", self.endpoint, "--token", fixtures.MANAGEMENT,
                                            "--insecure", "--no-env", "secret", "set", "TTY_TOKEN"],
                                           stdin=slave, stdout=slave, stderr=slave)
                transcript = b""
                deadline = time.monotonic() + 10
                while b"TTY_TOKEN: " not in transcript and time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.2)
                    if ready:
                        transcript += os.read(master, 65536)
                self.assertIn(b"TTY_TOKEN: ", transcript)
                if cancel:
                    process.send_signal(signal.SIGINT)
                else:
                    os.write(master, b"hidden-terminal-value\r")
                process.wait(timeout=10)
                while select.select([master], [], [], 0)[0]:
                    transcript += os.read(master, 65536)
                self.assertEqual(process.returncode, 1 if cancel else 0)
                self.assertNotIn(b"hidden-terminal-value", transcript)
                after = termios.tcgetattr(slave)
                # Darwin may set PENDIN when restoring canonical input; it is a
                # kernel pending-input marker, not an echo/canonical mode change.
                pending = getattr(termios, "PENDIN", 0)
                after[3] &= ~pending
                before[3] &= ~pending
                self.assertEqual(after, before)
            finally:
                if "process" in locals() and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                os.close(master)
                os.close(slave)

    def test_runtime_proxy_uses_control_region_policy_and_preserves_explicit_values(self):
        state = load_state()
        state["nomadRpcAddr"] = "http://100.64.0.8:4647"
        state["regions"] = {"private": {"egress": "proxy"}, "abroad": {"egress": "direct"}}
        save_state(state)
        self.control_config = self.root / "control.yml"
        self.control_config.write_text("defaults:\n  stackRoot: jobs\n  routesRoot: routes\n  nomadServer: 100.64.0.99\n")
        self.start()
        for region, expected in (("cn", "http://100.64.0.8:7890"), ("home", "http://100.64.0.8:7890"),
                                 ("private", "http://100.64.0.8:7890"), ("global", None), ("abroad", None)):
            with self.subTest(region=region):
                name = "proxy-" + region
                manifest = f"name: {name}\nregion: {region}\nimage: busybox:1.36\nproxy: true\nenv:\n  HTTP_PROXY: ${{CUSTOM_PROXY}}\n  NO_PROXY: internal.test\n"
                incoming = dict(manifest=manifest, envSecrets=dict(CUSTOM_PROXY="http://custom.test:3128"))
                status, preview = self.request("/v1/deployments/preview", incoming, fixtures.MANAGEMENT)
                self.assertEqual(status, 200, preview)
                env = json.loads(preview["artifacts"][0]["content"])["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
                self.assertEqual(env["HTTP_PROXY"], "${CUSTOM_PROXY}")
                self.assertEqual(env.get("HTTPS_PROXY"), expected)
                status, result = self.request("/v1/deployments", incoming, fixtures.MANAGEMENT)
                self.assertEqual(status, 200, result)
                env = self.nomad.jobs[name]["TaskGroups"][0]["Tasks"][0]["Env"]
                self.assertEqual(env["HTTP_PROXY"], "http://custom.test:3128")
                self.assertEqual(env.get("HTTPS_PROXY"), expected)
                self.assertEqual(env["NO_PROXY"], "internal.test")
        sidecar = self.root / "proxy.compose.yml"
        sidecar.write_text("name: proxy-compose\nregion: cn\nservices:\n  enabled:\n    proxy: true\n")
        (self.root / "docker-compose.yml").write_text("services:\n  enabled:\n    image: busybox:1.36\n  disabled:\n    image: busybox:1.36\n    environment:\n      KEEP: original\n")
        self.cli_call("compose", "deploy", str(sidecar))
        tasks = {t["Name"]: t.get("Env", {}) for t in self.nomad.jobs["proxy-compose"]["TaskGroups"][0]["Tasks"]}
        self.assertEqual(tasks["enabled"], dict(HTTP_PROXY="http://100.64.0.8:7890", HTTPS_PROXY="http://100.64.0.8:7890"))
        self.assertEqual(tasks["disabled"], dict(KEEP="original"))
        before = len(self.nomad.submissions)
        status, result = self.request("/v1/deployments", dict(manifest="name: unknown\nregion: unregistered\nimage: busybox:1.36\n"), fixtures.MANAGEMENT)
        self.assertEqual(status, 400, result)
        self.assertEqual(len(self.nomad.submissions), before)
        self.stop()
        state = load_state()
        state.pop("nomadRpcAddr")
        state["nomadAddr"] = "http://127.0.0.1:4646"
        save_state(state)
        self.control_config.write_text("defaults:\n  stackRoot: jobs\n  routesRoot: routes\n")
        self.start()
        status, result = self.request("/v1/deployments", dict(manifest="name: unavailable\nregion: cn\nimage: busybox:1.36\nproxy: true\n"), fixtures.MANAGEMENT)
        self.assertEqual(status, 400, result)
        self.assertIn("no manager gateway address", result["error"])
        self.assertEqual(len(self.nomad.submissions), before)
        status, result = self.request("/v1/deployments", dict(manifest="name: explicit-proxies\nregion: cn\nimage: busybox:1.36\nproxy: true\nenv:\n  HTTP_PROXY: http://custom.test:3128\n  HTTPS_PROXY: http://custom.test:3128\n"), fixtures.MANAGEMENT)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.nomad.jobs["explicit-proxies"]["TaskGroups"][0]["Tasks"][0]["Env"],
                         dict(HTTP_PROXY="http://custom.test:3128", HTTPS_PROXY="http://custom.test:3128"))

    def test_cloudflare_tunnel_token_reaches_only_sidecar_with_no_dns_record(self):
        self.control_config = self.root / "control.yml"
        self.control_config.write_text("defaults:\n  stackRoot: jobs\n  routesRoot: routes\nproviders:\n  dns:\n    provider: cloudflare\n")
        self.start()
        source = self.root / "tunnel.yml"
        manifest = "name: private-tunnel\nregion: cn\nimage: nginx:alpine\nexposure: cloudflare-tunnel\ndomain: 127.0.0.1:1\nport: 80\ntunnel:\n  tokenEnv: PRIVATE_TUNNEL_TOKEN\n"
        source.write_text(manifest)
        request = dict(manifest=manifest, envSecrets=dict(PRIVATE_TUNNEL_TOKEN="private-tunnel-value", UNUSED="not-imported"))
        status, preview = self.request("/v1/deployments/preview", request, fixtures.MANAGEMENT)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["dnsRecords"], [])
        self.assertEqual([a["kind"] for a in preview["artifacts"]], ["job"])
        self.assertIn("${PRIVATE_TUNNEL_TOKEN}", preview["artifacts"][0]["content"])
        self.assertNotIn("private-tunnel-value", json.dumps(preview))
        status, result = self.request("/v1/deployments", dict(manifest=manifest), fixtures.MANAGEMENT)
        self.assertEqual(status, 400, result)
        self.assertEqual(self.nomad.submissions, [])
        envfile = self.root / "tunnel.env"
        envfile.write_text("PRIVATE_TUNNEL_TOKEN=private-tunnel-value\nUNUSED=not-imported\n")
        result = self.cli_call("deploy", str(source), "--env", str(envfile))
        tasks = {t["Name"]: t for t in self.nomad.jobs["private-tunnel"]["TaskGroups"][0]["Tasks"]}
        self.assertEqual(self.nomad.jobs["private-tunnel"]["TaskGroups"][0]["Networks"], [{"Mode": "bridge"}])
        self.assertNotIn("ports", tasks["private-tunnel"]["Config"])
        self.assertNotIn("TUNNEL_TOKEN", tasks["private-tunnel"].get("Env", {}))
        self.assertEqual(tasks["cloudflared"]["Env"], dict(TUNNEL_TOKEN="private-tunnel-value"))
        self.assertEqual(tasks["cloudflared"]["Config"]["args"], ["tunnel", "--no-autoupdate", "run"])
        self.assertEqual(result["health"]["status"], "inconclusive")
        self.assertEqual(result["secrets"]["referenced"], ["PRIVATE_TUNNEL_TOKEN"])
        artifact = self.root / "jobs/cn/private-tunnel/private-tunnel.nomad.json"
        self.assertIn("${PRIVATE_TUNNEL_TOKEN}", artifact.read_text())
        self.assertNotIn("private-tunnel-value", artifact.read_text())
        self.assertEqual(list((self.root / "routes").iterdir()), [])
        self.stop()
        state = load_state()
        self.assertEqual(state["scopedSecrets"]["private-tunnel"], dict(PRIVATE_TUNNEL_TOKEN="private-tunnel-value"))
        for key in ("deployments", "deploymentEvents"):
            self.assertNotIn("private-tunnel-value", json.dumps(state[key]))

    def test_private_runtime_auth_is_absent_from_preview_files_and_history(self):
        self.control_config = self.root / "control.yml"
        self.control_config.write_text("defaults:\n  stackRoot: jobs\n  routesRoot: routes\n")
        self.start()
        request = dict(manifest="name: private-runtime\nregion: cn\nimage: private.test/app:1\n")
        status, preview = self.request("/v1/deployments/preview", request, fixtures.MANAGEMENT)
        self.assertEqual(status, 200, preview)
        self.assertNotIn("auth", json.loads(preview["artifacts"][0]["content"])["Job"]["TaskGroups"][0]["Tasks"][0]["Config"])
        status, result = self.request("/v1/deployments", request, fixtures.MANAGEMENT)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.nomad.jobs["private-runtime"]["TaskGroups"][0]["Tasks"][0]["Config"]["auth"],
                         dict(username="fixture-user", password="fixture-registry-secret", server_address="private.test"))
        self.assertTrue(result["images"]["private-runtime"]["registryAuth"])
        self.assertNotIn("fixture-registry-secret", json.dumps(result))
        self.assertNotIn("fixture-registry-secret", (self.root / "jobs/cn/private-runtime/private-runtime.nomad.json").read_text())
        self.stop()
        state = load_state()
        for key in ("deployments", "deploymentEvents"):
            self.assertNotIn("fixture-registry-secret", json.dumps(state[key]))

    def test_registry_cli_login_list_deploy_remove_and_read_only(self):
        state = load_state()
        state["setupChecks"] = dict(checks=dict(registry=dict(status="passed", extra="keep"), unrelated=dict(status="passed")))
        save_state(state)
        self.start()
        secret = "private-registry-fixture-$-quotes-\"-'"
        result = subprocess.run([str(self.cli), "registry", "login", "Private.Test/", "--username", "new-user", "--password-stdin",
                                 "--control-url", self.endpoint, "--token", fixtures.MANAGEMENT, "--insecure", "--no-env", "--format", "json"],
                                input=secret + "\n", text=True, capture_output=True, cwd=self.root, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"], dict(host="private.test", username="new-user", saved=True))
        listed = self.cli_call("registry", "list")
        self.assertEqual(listed, dict(registries=[dict(host="private.test", serverAddress="private.test", username="new-user", configured=True)]))
        status, _ = self.request("/v1/registries", None, fixtures.TOKEN)
        self.assertEqual(status, 401)
        for body in (dict(host="https://private.test", username="u", password=secret), dict(host="private.test", username="u"), dict(host="private.test", username="u", password=secret, unsupported=True)):
            status, response = self.request("/v1/registries", body, fixtures.MANAGEMENT)
            self.assertEqual(status, 400, response)
            self.assertNotIn(secret, json.dumps(response))
        source = self.root / "registry.yml"
        source.write_text("name: registry-managed\nregion: cn\nimage: private.test/app:1\n")
        self.cli_call("deploy", str(source))
        auth = self.nomad.jobs["registry-managed"]["TaskGroups"][0]["Tasks"][0]["Config"]["auth"]
        self.assertEqual(auth, dict(username="new-user", password=secret, server_address="private.test"))
        self.stop()
        state = load_state()
        self.assertEqual(state["registries"]["private.test"]["password"], secret)
        self.assertEqual(state["setupChecks"]["checks"]["registry"]["status"], "stale")
        self.assertEqual(state["setupChecks"]["checks"]["registry"]["extra"], "keep")
        self.assertEqual(state["setupChecks"]["checks"]["unrelated"]["status"], "passed")
        self.start("--read-only")
        self.assertEqual(self.cli_call("registry", "list"), listed)
        status, _ = self.request("/v1/registries/remove", dict(host="private.test"), fixtures.MANAGEMENT)
        self.assertEqual(status, 501)
        self.stop()
        self.start()
        self.assertEqual(self.cli_call("registry", "remove", "PRIVATE.TEST"), dict(host="private.test", removed=True))
        self.assertEqual(self.cli_call("registry", "remove", "private.test"), dict(host="private.test", removed=False))
        self.assertEqual(self.cli_call("registry", "list"), dict(registries=[]))

    def test_pinned_image_preparation_uses_target_agent_without_persisted_auth(self):
        state = load_state()
        state["nodes"]["worker"]["agent"].update(capabilities=["docker-image"], supportedActions=["resolve-docker-image"], os="darwin", arch="arm64")
        save_state(state)
        self.start()
        digest = "sha256:" + "c" * 64
        presence = dict(nodeName="worker", nodeId="node-1", capabilities=["docker-image"], supportedActions=["resolve-docker-image"], os="darwin", arch="arm64", activeTaskId="", waitSeconds=2)

        def resolve_on_agent():
            status, leased = self.request("/v1/node-agent/lease", presence)
            self.assertEqual(status, 200, leased)
            task = leased["task"]
            self.assertEqual(task["action"], "resolve-docker-image")
            payload = task["payload"]
            self.assertEqual(payload["image"], "private.test/app:latest")
            self.assertTrue(payload["forcePull"])
            self.assertEqual(payload["platform"], "linux/arm64")
            self.assertEqual(payload["registryAuth"]["password"], "fixture-registry-secret")
            stored = fixtures.GoControlAgentContracts.stored(self, "agentTasks", task["id"])
            self.assertNotIn("registryAuth", stored["payload"])
            status, completed = self.request("/v1/node-agent/tasks/complete", dict(nodeName="worker", nodeId="node-1", taskId=task["id"], status="succeeded",
                                             result=dict(image=payload["image"], deployed="private.test/app@" + digest, platform=payload["platform"])))
            self.assertEqual(status, 200, completed)

        native = self.root / "direct.yml"
        native.write_text("name: direct-native\nnode: old-worker\nregion: cn\nimage: private.test/app:latest\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(self.cli_call, "deploy", str(native))
            resolve_on_agent()
            result = work.result(timeout=5)
        self.assertEqual(result["images"]["direct-native"]["resolvedBy"], "target-node")
        self.assertEqual(result["images"]["direct-native"]["node"], "worker")
        sidecar = self.root / "direct.compose.yml"
        sidecar.write_text("name: direct-compose\nregion: cn\nservices:\n  one:\n    node: worker\n  two:\n    node: worker\n")
        (self.root / "docker-compose.yml").write_text("services:\n  one:\n    image: private.test/app:latest\n  two:\n    image: private.test/app:latest\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(self.cli_call, "compose", "deploy", str(sidecar))
            resolve_on_agent()
            result = work.result(timeout=5)
        self.assertEqual(len(result["images"]), 2)
        self.stop()
        state = load_state()
        self.assertEqual(len(state["agentTasks"]), 2)
        self.assertNotIn("fixture-registry-secret", json.dumps([state["agentTasks"], state["deployments"], state["deploymentEvents"]]))

    def test_unpinned_latest_resolves_authenticated_registry_on_each_deploy(self):
        digest = ["sha256:" + "a" * 64]
        heads = []
        import base64
        auth = "Basic " + base64.b64encode(b"registry-user:registry-fixture-secret").decode()
        class Registry(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def answer(self):
                if self.headers.get("Authorization") != auth:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                    self.end_headers()
                    return
                if self.path == "/v2/":
                    self.send_response(200)
                    self.end_headers()
                    return
                heads.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "123")
                self.send_header("Content-Type", "application/vnd.oci.image.index.v1+json")
                self.send_header("Docker-Content-Digest", digest[0])
                self.end_headers()
            do_GET = answer
            do_HEAD = answer
        registry = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Registry)
        thread = threading.Thread(target=registry.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            registry.shutdown()
            registry.server_close()
            thread.join()
        self.addCleanup(cleanup)
        host = "127.0.0.1:" + str(registry.server_port)
        state = load_state()
        state["registries"][host] = dict(username="registry-user", password="registry-fixture-secret")
        save_state(state)
        self.start()
        source = self.root / "mutable.yml"
        source.write_text(f"name: mutable\nregion: cn\nimage: {host}/app:latest\n")
        status, preview = self.request("/v1/deployments/preview", dict(manifest=source.read_text()), fixtures.MANAGEMENT)
        self.assertEqual(status, 200, preview)
        self.assertEqual(heads, [])
        for value in ("a", "b"):
            digest[0] = "sha256:" + value * 64
            result = self.cli_call("deploy", str(source))
            self.assertEqual(result["images"]["mutable"]["resolvedBy"], "registry")
            config = self.nomad.jobs["mutable"]["TaskGroups"][0]["Tasks"][0]["Config"]
            self.assertEqual(config["image"], host + "/app@" + digest[0])
            self.assertEqual(config["auth"]["password"], "registry-fixture-secret")
        self.assertEqual(heads, ["/v2/app/manifests/latest"] * 2)

    def test_builder_cache_tasks_deduplicate_and_separate_source_push_runtime_credentials(self):
        insecure = patch.dict(os.environ, LUMA_LAE_BUILDER_REGISTRY_INSECURE="1")
        insecure.start()
        self.addCleanup(insecure.stop)
        state = load_state()
        state["build"] = dict(defaultNode="worker", nodes=["worker"], registryHost="registry.test:5000", pushHost="127.0.0.1:5000", directEgressNodes=["worker"])
        state["nodes"]["worker"]["agent"].update(capabilities=["runtime-image-cache-v1"], supportedActions=["cache-runtime-image"], os="linux", arch="arm64")
        state["registries"].update({"registry.test:5000": dict(username="runtime", password="runtime-private"),
                                    "127.0.0.1:5000": dict(username="push", password="push-private")})
        save_state(state)
        self.control_config = self.root / "control.yml"
        self.control_config.write_text("defaults:\n  stackRoot: jobs\n  routesRoot: routes\n  imageMirrors: []\n")
        self.start()
        digest = "sha256:" + "a" * 64
        presence = dict(nodeName="worker", nodeId="node-1", os="linux", arch="arm64", capabilities=["runtime-image-cache-v1"],
                        supportedActions=["cache-runtime-image"], activeTaskId="", waitSeconds=2)

        def serve_cache():
            status, leased = self.request("/v1/node-agent/lease", presence)
            self.assertEqual(status, 200, leased)
            task = leased["task"]
            self.assertIsNotNone(task)
            p = task["payload"]
            self.assertEqual(p["sourceRegistryAuth"]["password"], "fixture-registry-secret")
            self.assertEqual(p["destinationRegistryAuth"]["password"], "push-private")
            self.assertNotIn("runtime-private", json.dumps(p))
            stored = fixtures.GoControlAgentContracts.stored(self, "agentTasks", task["id"])
            for secret in ("fixture-registry-secret", "push-private", "runtime-private"):
                self.assertNotIn(secret, json.dumps(stored))
            result = dict(sourceImage=p["sourceImage"], pushImage=p["pushImage"], destinationImage=p["destinationImage"], digest=digest)
            status, completed = self.request("/v1/node-agent/tasks/complete", dict(nodeName="worker", nodeId="node-1", taskId=task["id"], status="succeeded", result=result))
            self.assertEqual(status, 200, completed)
            return p

        native = self.root / "cached.yml"
        native.write_text("name: cached-native\nregion: cn\nimage: private.test/app:1\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(self.cli_call, "deploy", str(native))
            first = serve_cache()
            result = work.result(timeout=5)
        expected = first["destinationImage"].rsplit(":", 1)[0] + "@" + digest
        task = self.nomad.jobs["cached-native"]["TaskGroups"][0]["Tasks"][0]
        self.assertEqual(task["Config"]["image"], expected)
        self.assertEqual(task["Config"]["auth"], dict(username="runtime", password="runtime-private", server_address="registry.test:5000"))
        self.assertTrue(result["images"]["cached-native"]["cached"])
        sidecar = self.root / "luma.compose.yml"
        sidecar.write_text("name: cached-compose\nregion: cn\n")
        (self.root / "docker-compose.yml").write_text("services:\n  one:\n    image: private.test/app:1\n  two:\n    image: private.test/app:1\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(self.cli_call, "compose", "deploy", str(sidecar))
            serve_cache()
            work.result(timeout=5)
        self.assertEqual({t["Config"]["image"] for t in self.nomad.jobs["cached-compose"]["TaskGroups"][0]["Tasks"]}, {expected})
        internal = dict(manifest="name: already-internal\nregion: cn\nimage: 127.0.0.1:5000/app:1\n")
        status, result = self.request("/v1/deployments", internal, fixtures.MANAGEMENT)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["images"]["already-internal"]["deployed"], "registry.test:5000/app:1")
        self.assertFalse(result["images"]["already-internal"]["cached"])
        self.stop()
        state = load_state()
        self.assertEqual(len(state["agentTasks"]), 2)
        for key in ("agentTasks", "deployments", "deploymentEvents"):
            for secret in ("fixture-registry-secret", "push-private", "runtime-private"):
                self.assertNotIn(secret, json.dumps(state[key]))
        for artifact in (self.root / "jobs").rglob("*.json"):
            for secret in ("fixture-registry-secret", "push-private", "runtime-private"):
                self.assertNotIn(secret, artifact.read_text())

    def test_incomplete_builder_registry_does_not_submit_or_queue(self):
        state = load_state()
        state["build"] = dict(registryHost="registry.test:5000")
        save_state(state)
        self.start()
        status, result = self.request("/v1/deployments", dict(manifest="name: incomplete-cache\nregion: cn\nimage: private.test/app:1\n"), fixtures.MANAGEMENT)
        self.assertEqual(status, 400, result)
        self.assertEqual(self.nomad.submissions, [])
        self.stop()
        self.assertEqual(load_state()["agentTasks"], {})
