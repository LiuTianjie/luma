"""Regression coverage for the incident's trust boundaries and staged migration."""
import copy
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from luma import bootstrap, nomad_acl, nomad_firewall, nomad_node, nomad_security, security_audit
from luma.config import NodeConfig
from luma.errors import LumaError
from luma.local import LocalResult
from luma.nomad_api import NomadApi


def workload(*, image="alpine:3.21", mounts=None, network="bridge", name="example"):
    config = {"image": image, "args": ["sleep", "30"], "network_mode": network}
    if mounts:
        config["mount"] = mounts
    return {"ID": name, "Type": "service", "TaskGroups": [{"Name": "app", "Count": 1, "Tasks": [{"Name": "app", "Driver": "docker", "Config": config}]}]}


def nomad_test_ports():
    # Serf binds TCP and UDP to the same number. An available TCP port alone
    # can still collide with a host's UDP socket. Reserve both until all three
    # distinct ports are selected, then release them immediately before spawn.
    reserved, ports = [], []
    try:
        for _ in range(3):
            for attempt in range(50):
                tcp = socket.socket()
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    tcp.bind(("127.0.0.1", 0))
                    port = tcp.getsockname()[1]
                    udp.bind(("127.0.0.1", port))
                except OSError:
                    tcp.close()
                    udp.close()
                    if attempt == 49:
                        raise
                else:
                    reserved.extend((tcp, udp))
                    ports.append(port)
                    break
        return ports
    finally:
        for sock in reserved:
            sock.close()


class SecurityFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="luma-security-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime, self.admin = self.root / "runtime", self.root / "admin"
        self.env = mock.patch.dict(os.environ, {"LUMA_NOMAD_SECURITY_DIR": str(self.runtime), "LUMA_NOMAD_AGENT_TOKEN_FILE": str(self.admin / "agent.token"), "LUMA_SECURITY_AUDIT_FILE": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def prepare(self):
        return nomad_acl.prepare_acl(admin_root=self.admin, service_root=self.runtime)

    def enforce(self):
        policy = {"schema": 1, "mode": "enforce", "grants": {}}
        nomad_security.private_write(self.runtime / "workloads.json", json.dumps(policy))
        return policy


class SecurityFiles(SecurityFixture):
    def test_preparation_is_private_idempotent_and_excludes_management_from_consumers(self):
        first = self.prepare()
        management = nomad_security.private_read(self.admin / "management.token").strip()
        self.assertEqual(first, self.prepare())
        for path in self.runtime.rglob("*"):
            if path.is_file():
                self.assertNotIn(management, path.read_text())
                self.assertFalse(path.stat().st_mode & 0o077)
        self.assertEqual(len({nomad_security.private_read(nomad_security.service_token_path(self.runtime, r)) for r in nomad_security.ROLES}), 5)

    def test_management_directory_cannot_be_inside_control_mount(self):
        with self.assertRaisesRegex(LumaError, "Control"):
            nomad_acl.prepare_acl(admin_root=Path("/opt/luma/admin"), service_root=self.runtime)

    def test_active_cluster_never_falls_back_to_state_token_or_anonymous(self):
        nomad_security.private_write(self.runtime / "cluster.json", '{"aclRequired":true}')
        with self.assertRaises(LumaError):
            nomad_security.nomad_token({"nomadToken": "legacy-management"})
        path = nomad_security.service_token_path(self.runtime, "control")
        nomad_security.private_write(path, "scoped\n")
        self.assertEqual(nomad_security.nomad_token({"nomadToken": "legacy-management"}), "scoped")
        path.chmod(0o644)
        with self.assertRaises(LumaError):
            nomad_security.nomad_token({"nomadToken": "legacy-management"})

    def test_credentials_reject_symlinks_and_follow_atomic_rotations(self):
        self.prepare()
        path = nomad_security.service_token_path(self.runtime, "control")
        old = nomad_security.nomad_token()
        nomad_security.private_write(path, "replacement\n")
        self.assertNotEqual(old, nomad_security.nomad_token())
        path.unlink()
        path.symlink_to(self.admin / "management.token")
        with self.assertRaises(LumaError):
            nomad_security.nomad_token()

    def test_hostshell_and_renaming_cannot_inherit_an_observe_grant(self):
        legitimate = workload(name="luma-observe", network="host", mounts=[{"type": "bind", "source": "/srv/luma/data/observe", "target": "/data"}])
        policy = self.enforce()
        policy["grants"]["default/luma-observe"] = {"sha256": nomad_security.job_digest(legitimate), "reason": "Reviewed observer storage and host network"}
        nomad_security.admit_job(legitimate, policy=policy)
        for name in ("hostshell", "luma-observe"):
            malicious = workload(name=name, image="attacker/root-shell", network="host", mounts=[{"type": "bind", "source": "/", "target": "/host", "readonly": False}])
            malicious["Meta"] = {"luma.managed": "true"}
            with self.subTest(name=name), self.assertRaisesRegex(LumaError, "reviewed security grant"):
                nomad_security.admit_job(malicious, policy=policy)
        pending = list((self.runtime / "review-pending").glob("*.json"))
        self.assertEqual(len(pending), 2)
        self.assertTrue(all(not p.stat().st_mode & 0o077 for p in pending))

    def test_grant_covers_image_command_env_and_placement(self):
        job = workload(network="host")
        policy = self.enforce()
        policy["grants"]["default/example"] = {"sha256": nomad_security.job_digest(job), "reason": "Reviewed exact host networking task"}
        mutations = [
            lambda j: j["TaskGroups"][0]["Tasks"][0]["Config"].update(image="malware"),
            lambda j: j["TaskGroups"][0]["Tasks"][0]["Config"].update(args=["sh", "-c", "whoami"]),
            lambda j: j["TaskGroups"][0]["Tasks"][0].update(Env={"LD_PRELOAD": "/host/library.so"}),
            lambda j: j.update(NodePool="all"),
        ]
        for change in mutations:
            candidate = copy.deepcopy(job)
            change(candidate)
            with self.assertRaises(LumaError):
                nomad_security.admit_job(candidate, policy=policy)

    def test_devices_capabilities_and_docker_socket_require_grants(self):
        self.enforce()
        for config in ({"devices": [{"host_path": "/dev/sda"}]}, {"privileged": True}, {"cap_add": ["SYS_ADMIN"]}, {"pid_mode": "host"}, {"mounts": [{"type": "bind", "source": "/var/run/docker.sock", "target": "/run/docker.sock"}]}):
            candidate = workload()
            candidate["TaskGroups"][0]["Tasks"][0]["Config"].update(config)
            with self.subTest(config=config), self.assertRaises(LumaError):
                nomad_security.admit_job(candidate)

    def test_named_volumes_are_not_misclassified_as_isolated_workloads(self):
        job = workload(mounts=[{"type": "volume", "source": "example_database", "target": "/data"}])
        self.assertIn("persistent Docker volume", nomad_security.job_risks(job))
        with self.assertRaises(LumaError):
            nomad_security.admit_job(job, policy=self.enforce())
        nomad_security.admit_job(workload(), policy=self.enforce())

    def test_application_renderer_blocks_root_and_engine_socket_even_during_staging(self):
        for source in ("/", "/./", "/opt/luma", "/var/run/docker.sock"):
            with self.subTest(source=source), self.assertRaises(LumaError):
                nomad_security.deny_host_root(workload(mounts=[{"type": "bind", "source": source, "target": "/data"}]))
        nomad_security.deny_host_root(workload(mounts=[{"type": "bind", "source": "/srv/luma/data/database", "target": "/data"}]))

    def test_missing_active_admission_file_fails_closed(self):
        nomad_security.private_write(self.runtime / "cluster.json", '{"aclRequired":true}')
        with self.assertRaises(LumaError):
            nomad_security.admit_job(workload())

    def test_consumer_spec_contains_paths_and_no_token_values(self):
        self.prepare()
        job = workload(name="traefik", network="host")
        job["TaskGroups"][0]["Tasks"][0]["Name"] = "traefik"
        nomad_security.prepare_job(job)
        first = json.dumps(job, sort_keys=True)
        nomad_security.prepare_job(job)
        self.assertEqual(first, json.dumps(job, sort_keys=True))
        for role in nomad_security.ROLES:
            self.assertNotIn(nomad_security.private_read(nomad_security.service_token_path(self.runtime, role)).strip(), first)
        self.assertIn("NOMAD_TOKEN_FILE", first)
        self.assertNotIn(str(self.runtime / "control.d"), first)

    def test_job_revert_is_subject_to_the_same_admission_policy(self):
        self.enforce()
        api = NomadApi("http://127.0.0.1:14646", token="dummy")
        candidate = workload(network="host")
        candidate["Version"] = 3
        with mock.patch.object(api, "request", return_value={"Versions": [candidate]}), mock.patch("urllib.request.build_opener") as transport:
            with self.assertRaises(LumaError):
                api.request_text("POST", "/v1/job/example/revert", {"JobVersion": 3})
            transport.assert_not_called()

    def test_rollback_cannot_omit_the_new_storage_node_restriction(self):
        self.enforce()
        job = workload(mounts=[{"type": "bind", "source": "/srv/app-data", "target": "/data"}])
        nomad_security.authorize_application(job)
        previous = copy.deepcopy(job)
        previous.pop("Constraints")
        previous["Version"] = 3
        api = NomadApi("http://127.0.0.1:14646", token="dummy")
        with mock.patch.object(api, "request", return_value={"Versions": [previous]}), mock.patch("urllib.request.build_opener") as transport:
            with self.assertRaisesRegex(LumaError, "predates the current security requirements"):
                api.request_text("POST", "/v1/job/example/revert", {"JobVersion": 3})
            transport.assert_not_called()

    def test_parallel_deployments_keep_each_others_authority_grants(self):
        from concurrent.futures import ThreadPoolExecutor
        self.enforce()
        barrier = threading.Barrier(8)
        jobs = [workload(name=f"app-{i}", network="host") for i in range(8)]
        def authorize(job):
            barrier.wait(timeout=10)
            nomad_security.authorize_application(job)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(authorize, jobs))
        for job in jobs:
            nomad_security.admit_job(job)

    def test_failed_firewall_install_never_starts_nomad(self):
        executor = mock.Mock()
        executor.sudo_result.return_value = LocalResult(1, "")
        node = NodeConfig(name="worker", host="worker", region="cn", roles=["cn"], raw={})
        settings = {"schema": 1, "aclRequired": True, "nomadVersion": nomad_node.NOMAD_VERSION}
        with mock.patch.object(bootstrap, "LocalExecutor", return_value=executor), mock.patch.object(bootstrap, "setup_tailscale", return_value=["ready"]), mock.patch.object(bootstrap, "_tailscale_ip", return_value="100.64.0.2"), mock.patch.object(nomad_node, "detect_os", return_value="linux"), mock.patch.object(nomad_firewall, "install_guard", side_effect=LumaError("guard failed")):
            with self.assertRaisesRegex(LumaError, "guard failed"):
                bootstrap.install_nomad_node(node, role="client", region="cn", node_name="worker", server_addrs=["100.64.0.1:4647"], security=settings, install_docker_first=False)
        self.assertFalse(any("restart nomad" in str(c) for c in executor.mock_calls))


class NetworkAndTransportTests(unittest.TestCase):
    def test_configuration_scopes_listeners_drivers_and_host_access(self):
        for os_name in ("linux", "darwin"):
            config = nomad_node.render_agent_config(os_name=os_name, role="client", tailscale_ip="100.64.0.2", region="home", node_name="worker", server_addrs=["100.64.0.1:4647"])
            self.assertNotIn('"0.0.0.0"', config)
            self.assertIn('http = "127.0.0.1 100.64.0.2"', config)
            self.assertIn('"driver.allowlist" = "docker"', config)
            self.assertIn("allow_privileged = false", config)
            self.assertIn("volumes {\n      enabled = false", config)
        for peer in ("203.0.113.1:4647", '100.64.0.1"; dangerous', "100.64.0.0/10"):
            with self.assertRaises(LumaError):
                nomad_security.rpc_host(peer)

    def test_linux_guard_is_dual_stack_and_precedes_forward_and_nat(self):
        policy = nomad_firewall.network_policy(os_name="linux", address="100.64.0.1", rpc_peers=["100.64.0.2", "fd7a:115c:a1e0::2"], http_peers=[])
        rules = nomad_firewall.render_nftables(policy)
        self.assertIn("hook prerouting priority -310", rules)
        self.assertIn("hook forward priority -310", rules)
        self.assertIn('iifname "tailscale0" ip saddr 100.64.0.2 tcp dport 4647 return', rules)
        self.assertIn('ip6 saddr fd7a:115c:a1e0::2 tcp dport 4647 return', rules)
        self.assertNotIn("flush ruleset", rules)
        self.assertNotIn("eth0", rules)
        self.assertNotIn("ct state established", rules)
        self.assertIn("transit tcp dport { 4646, 4647, 4648 } counter drop", rules)

    def test_mac_guard_needs_real_mesh_interface_and_has_no_tailnet_wide_pass(self):
        with self.assertRaises(LumaError):
            nomad_firewall.network_policy(os_name="darwin", address="100.64.0.1", rpc_peers=[], http_peers=[])
        policy = nomad_firewall.network_policy(os_name="darwin", address="100.64.0.1", rpc_peers=["100.64.0.2"], http_peers=[], interface="utun6")
        rules = nomad_firewall.render_pf(policy)
        self.assertIn("on utun6 proto tcp from 100.64.0.2", rules)
        self.assertNotIn("100.64.0.0/10", rules)
        self.assertIn("block drop in quick proto tcp", rules)

    def test_nomad_redirect_does_not_forward_secret_to_another_origin(self):
        seen = []
        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("X-Nomad-Token"))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            def log_message(self, *args): pass
        sink = HTTPServer(("127.0.0.1", 0), Sink)
        class Redirect(Sink):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/steal")
                self.end_headers()
        redirect = HTTPServer(("127.0.0.1", 0), Redirect)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (sink, redirect)]
        for thread in threads: thread.start()
        try:
            with self.assertRaises(LumaError):
                NomadApi(f"http://127.0.0.1:{redirect.server_port}", token="sensitive-role-token").request("GET", "/v1/jobs")
            from luma.control.client import ControlClient
            control = ControlClient("https://control.example", "sensitive-agent-token")
            # Exercise the actual urllib redirect handler independently of TLS,
            # which is verified by the agent/PKI protocol tests.
            with mock.patch.object(control, "_request_url", return_value=f"http://127.0.0.1:{redirect.server_port}/lease"), self.assertRaises(LumaError):
                control.request("GET", "/v1/node-agent/lease")
            self.assertEqual(seen, [])
        finally:
            for server in (redirect, sink): server.shutdown(); server.server_close()
            for thread in threads: thread.join(timeout=2)


class EnrollmentOwnershipTests(SecurityFixture):
    def setUp(self):
        super().setUp()
        from luma.control import server
        self.server = server
        self.state = {"clusterId": "test", "deployToken": "administrator", "joinToken": "shared-join", "nodes": {"worker": {"nodeId": "node-1", "region": "cn"}, "manager": {"nodeId": "node-manager", "region": "cn"}}}
        self.agent_token = server._issue_node_agent_token(self.state, "worker", node_id="node-1")
        for name in ("mutate_state", "_mutate_control_state"):
            patcher = mock.patch.object(server, name, side_effect=lambda f: f(self.state))
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(server, "load_state", return_value=self.state)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_shared_join_token_cannot_take_over_existing_agent_by_public_id(self):
        before = copy.deepcopy(self.state)
        with self.assertRaisesRegex(LumaError, "unauthorized"):
            self.server.handle_node_agent_token("shared-join", {"nodeName": "worker", "nodeId": "node-1"})
        self.assertEqual(before, self.state)

    def test_existing_agent_can_refresh_itself_but_cannot_refresh_other_nodes(self):
        with self.assertRaisesRegex(LumaError, "unauthorized"):
            self.server.handle_node_agent_token(self.agent_token, {"nodeName": "manager", "nodeId": "node-manager"})
        result = self.server.handle_node_agent_token(self.agent_token, {"nodeName": "worker", "nodeId": "node-1"})
        self.assertNotEqual(result["agentToken"], self.agent_token)
        with self.assertRaisesRegex(LumaError, "unauthorized"):
            self.server._require_node_agent_token(self.state, self.agent_token, "worker", node_id="node-1")

    def test_shared_join_cannot_reregister_a_known_node(self):
        before = copy.deepcopy(self.state)
        with self.assertRaisesRegex(LumaError, "unauthorized"):
            self.server.handle_node_register("shared-join", {"nodeName": "worker", "region": "cn", "tailscaleIP": "100.64.0.2"})
        self.assertEqual(before, self.state)

    def test_enrollment_proof_is_bound_to_node_and_consumed_once(self):
        self.prepare()
        nomad_security.private_write(self.runtime / "cluster.json", '{"aclRequired":true}')
        self.state["nodes"]["new-worker"] = {"region": "cn", "pendingEnrollment": {"hash": self.server._hash_agent_token("one-time-proof"), "expiresAt": time.time() + 60, "tailscaleIP": "100.64.0.3"}}
        identity = {"Name": "new-worker", "Meta": {"luma_node_name": "new-worker", "luma_tailscale_ip": "100.64.0.3"}}
        body = {"nodeName": "new-worker", "nodeId": "node-3", "region": "cn", "tailscaleIP": "100.64.0.3"}
        with mock.patch.object(self.server, "load_config", return_value=mock.Mock(defaults={})), mock.patch.object(self.server, "NomadApi", return_value=mock.Mock(request=mock.Mock(return_value=identity))):
            with self.assertRaisesRegex(LumaError, "enrollment proof"):
                self.server.handle_node_label("shared-join", body)
            result = self.server.handle_node_label("shared-join", {**body, "enrollmentToken": "one-time-proof"})
            self.assertTrue(result["agentToken"])
            self.assertNotIn("pendingEnrollment", self.state["nodes"]["new-worker"])
            with self.assertRaises(LumaError):
                self.server.handle_node_label("shared-join", {**body, "enrollmentToken": "one-time-proof"})


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_untrusted_forwarding_headers_do_not_replace_socket_peer_or_leak_token(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"LUMA_SECURITY_AUDIT_FILE": str(Path(directory) / "audit.jsonl")}):
            async def app(scope, receive, send):
                security_audit.authenticated("deploy", "credential-id")
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"{}"})
            messages = []
            async def send(message): messages.append(message)
            await security_audit.SecurityAuditMiddleware(app)({"type": "http", "method": "POST", "path": "/v1/deploy", "client": ("192.0.2.3", 1234), "headers": [(b"authorization", b"Bearer SECRET"), (b"x-forwarded-for", b"127.0.0.1")]}, None, send)
            raw = (Path(directory) / "audit.jsonl").read_text()
            self.assertNotIn("SECRET", raw)
            self.assertNotIn("127.0.0.1", raw)
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(records[-1]["peer"], "192.0.2.3")
            self.assertTrue(records[-1]["authenticated"])
            self.assertEqual(records[0]["requestId"], records[-1]["requestId"])

    async def test_audit_failure_blocks_dispatch_and_returns_503(self):
        dispatched = []
        async def app(scope, receive, send): dispatched.append(True)
        messages = []
        async def send(message): messages.append(message)
        with mock.patch.object(security_audit, "record", side_effect=LumaError("unavailable")):
            await security_audit.SecurityAuditMiddleware(app)({"type": "http", "method": "POST", "path": "/v1/deploy", "headers": []}, None, send)
        self.assertFalse(dispatched)
        self.assertEqual(messages[0]["status"], 503)


@unittest.skipUnless(os.environ.get("LUMA_TEST_NOMAD_BINARY"), "set LUMA_TEST_NOMAD_BINARY for the isolated real Nomad ACL protocol check")
class NomadACLProtocolTests(SecurityFixture):
    """Runs only a loopback server with no client, Docker driver or host tasks."""
    def setUp(self):
        super().setUp()
        binary = os.environ["LUMA_TEST_NOMAD_BINARY"]
        ports = nomad_test_ports()
        config = self.root / "nomad.hcl"
        config.write_text(f'''data_dir = {json.dumps(str(self.root / 'data'))}
bind_addr = "127.0.0.1"
advertise {{ http = "127.0.0.1" rpc = "127.0.0.1" serf = "127.0.0.1" }}
ports {{ http = {ports[0]} rpc = {ports[1]} serf = {ports[2]} }}
server {{ enabled = true bootstrap_expect = 1 }}
client {{ enabled = false }}
acl {{ enabled = true }}
disable_update_check = true
''')
        self.log = open(self.root / "nomad.log", "w")
        self.process = subprocess.Popen([binary, "agent", "-config", str(config)], stdout=self.log, stderr=subprocess.STDOUT, env={k: v for k, v in os.environ.items() if not k.startswith("NOMAD_")})
        def close():
            self.process.terminate()
            try: self.process.wait(timeout=10)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
            self.log.close()
        self.addCleanup(close)
        self.address = f"http://127.0.0.1:{ports[0]}"
        api = NomadApi(self.address)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                if api.request("GET", "/v1/status/leader"): break
            except LumaError: pass
            time.sleep(0.2)
        if not api.request("GET", "/v1/status/leader"):
            self.fail("isolated Nomad server did not become leader: " + (self.root / "nomad.log").read_text()[-1500:])

    def test_registered_job_matches_saved_spec_after_nomad_adds_constraints(self):
        from luma.nomad_upgrade import _matches_expected_job

        self.prepare()
        nomad_acl.activate_acl(address=self.address, admin_root=self.admin, service_root=self.runtime)
        api = NomadApi(self.address, token=nomad_security.private_read(nomad_security.service_token_path(self.runtime, "control")).strip())
        expected = workload(name="implicit-constraints")
        expected["Constraints"] = [{"LTarget": "${meta.luma_region}", "Operand": "is_set"}]
        group = expected["TaskGroups"][0]
        group["Count"] = 0
        group["Networks"] = [{"Mode": "bridge", "DynamicPorts": [{"Label": "http", "To": 8080}]}]
        group["Services"] = [{"Name": "implicit-constraints", "Provider": "nomad", "PortLabel": "http",
                              "Checks": [{"Name": "health", "Type": "http", "Path": "/", "Interval": 10_000_000_000, "Timeout": 2_000_000_000}]}]
        group["Tasks"][0]["Secrets"] = [{"Name": "luma", "Provider": "nomad", "Path": "nomad/jobs/implicit-constraints/app/app", "Config": {"namespace": "default"}}]
        # Nomad applies the secret-provider constraint to every group, including
        # groups without a secret, network or service of their own.
        other = workload(network="host")["TaskGroups"][0]
        other.update(Name="worker", Count=0)
        expected["TaskGroups"].append(other)
        api.request("POST", "/v1/jobs", {"Job": expected})
        live = api.request("GET", "/v1/job/implicit-constraints")
        self.assertEqual(len(live["TaskGroups"][0]["Constraints"]), 8)
        self.assertEqual(len(live["TaskGroups"][1]["Constraints"]), 1)
        self.assertTrue(_matches_expected_job(live, expected))

    def test_acl_bootstrap_scopes_rotation_and_restart_recovery(self):
        self.prepare()
        result = nomad_acl.activate_acl(address=self.address, admin_root=self.admin, service_root=self.runtime)
        self.assertTrue(result["verification"]["ok"])
        # Calling activation again must reuse the same bootstrap identity.
        self.assertTrue(nomad_acl.activate_acl(address=self.address, admin_root=self.admin, service_root=self.runtime)["verification"]["ok"])
        for role in ("traefik", "exporter", "logs", "agent"):
            api = NomadApi(self.address, token=nomad_security.private_read(nomad_security.service_token_path(self.runtime, role)).strip())
            with self.subTest(role=role), self.assertRaisesRegex(LumaError, "403"):
                api.request("POST", "/v1/jobs", {"Job": workload()})
        path = nomad_security.service_token_path(self.runtime, "exporter")
        old = nomad_security.private_read(path).strip()
        rotated = nomad_acl.rotate_role("exporter", address=self.address, admin_root=self.admin, service_root=self.runtime)
        NomadApi(self.address, token=old).request("GET", "/v1/jobs")
        nomad_acl.revoke_accessor(rotated["previousAccessor"], address=self.address, admin_root=self.admin)
        with self.assertRaisesRegex(LumaError, "403"):
            NomadApi(self.address, token=old).request("GET", "/v1/jobs")
        self.assertTrue(nomad_acl.verify_acl(address=self.address, service_root=self.runtime)["ok"])


if __name__ == "__main__":
    unittest.main()
