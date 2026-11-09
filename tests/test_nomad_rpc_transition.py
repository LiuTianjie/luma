"""Real client RPC continuity through the server-first TLS migration."""
import json
import os
import subprocess
import time
import unittest

from luma import nomad_node, nomad_pki
from luma.errors import LumaError
from luma.nomad_api import NomadApi
from tests.test_nomad_security import SecurityFixture, nomad_test_ports


@unittest.skipUnless(os.environ.get("LUMA_TEST_NOMAD_BINARY"), "set the pinned Nomad binary for real RPC migration verification")
class NomadRPCTransition(SecurityFixture):
    def setUp(self):
        super().setUp()
        if os.uname().sysname == "Linux":
            self.assertEqual(os.geteuid(), 0, "the isolated Linux Nomad client requires root")
        self.processes, self.logs, self.ports = {}, {}, {}
        self.addCleanup(self.close)
        self.prepare()
        nomad_pki.prepare_manager("100.64.0.1", admin_root=self.admin, service_root=self.runtime)
        self.server_tls = nomad_pki.tls_paths(self.admin / "tls")
        client_root = self.root / "client-tls"
        bundle = nomad_pki.sign_csr(nomad_pki.create_csr(client_root), role="client", address="100.64.0.2", admin_root=self.admin)
        nomad_pki.install_certificate(bundle, root=client_root)
        self.client_tls = nomad_pki.tls_paths(client_root)

    def stop(self, name):
        process = self.processes.pop(name, None)
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        for name in list(self.processes):
            self.stop(name)
        for log in self.logs.values():
            log.close()

    def start(self, name, *, tls=None, transition=False):
        self.stop(name)
        if name not in self.ports:
            self.ports[name] = nomad_test_ports()
            self.logs[name] = open(self.root / (name + ".log"), "w")
        server = name == "server"
        address = "100.64.0.1" if server else "100.64.0.2"
        rendered = nomad_node.render_agent_config(
            os_name="linux" if os.uname().sysname == "Linux" else "darwin",
            role="server" if server else "client", tailscale_ip=address, region="cn", node_name="isolated-rpc-" + name,
            server_addrs=None if server else ["100.64.0.1:4647"],
            client_enabled=not server, acl_enabled=False, tls=tls, tls_http=False,
            rpc_upgrade_mode=transition, introduction_strict=False,
        )
        rendered = rendered.replace('"/opt/nomad/data"', json.dumps(str(self.root / (name + "-data"))))
        rendered = rendered.replace('"127.0.0.1 ' + address + '"', '"127.0.0.1"')
        if not server:
            rendered = rendered.replace("100.64.0.1:4647", "127.0.0.1:" + str(self.ports["server"][1]))
        rendered = rendered.replace("100.64.0.1", "127.0.0.1").replace("100.64.0.2", "127.0.0.1")
        http, rpc, serf = self.ports[name]
        path = self.root / (name + ".hcl")
        path.write_text(rendered + f"\nports {{ http = {http} rpc = {rpc} serf = {serf} }}\n")
        self.processes[name] = subprocess.Popen(
            [os.environ["LUMA_TEST_NOMAD_BINARY"], "agent", "-config", str(path)],
            stdout=self.logs[name], stderr=subprocess.STDOUT,
            env={key: value for key, value in os.environ.items() if not key.startswith("NOMAD_")},
        )
        api = NomadApi(f"http://127.0.0.1:{http}", managed_transport=False)
        self.wait_for(lambda: api.request("GET", "/v1/status/leader" if server else "/v1/agent/self", timeout=2), name + " startup")
        return api

    def wait_for(self, callback, description):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in self.processes.values()):
                break
            try:
                value = callback()
                if value:
                    return value
            except LumaError:
                pass
            time.sleep(0.2)
        logs = "\n".join((self.root / (name + ".log")).read_text()[-1800:] for name in self.logs)
        self.fail("timed out waiting for " + description + "\n" + logs)

    def test_plain_client_keeps_rpc_during_transition_then_requires_tls(self):
        server = self.start("server")
        self.start("client")
        nodes = self.wait_for(lambda: server.request("GET", "/v1/nodes", timeout=2), "client registration")
        self.assertEqual(len(nodes), 1)
        node_id = nodes[0]["ID"]
        path = "/v1/client/stats?node_id=" + node_id
        self.wait_for(lambda: server.request("GET", path, timeout=2), "initial client RPC")

        server = self.start("server", tls=self.server_tls, transition=True)
        self.wait_for(lambda: server.request("GET", path, timeout=2), "plaintext client RPC after server TLS rollout")
        live = server.request("GET", "/v1/agent/self")["config"]["TLSConfig"]
        self.assertTrue(live["EnableRPC"] and live["RPCUpgradeMode"])

        self.start("client", tls=self.client_tls)
        self.wait_for(lambda: server.request("GET", path, timeout=2), "TLS client RPC")
        server = self.start("server", tls=self.server_tls)
        self.wait_for(lambda: server.request("GET", path, timeout=2), "strict TLS client RPC")
        live = server.request("GET", "/v1/agent/self")["config"]["TLSConfig"]
        self.assertFalse(live["RPCUpgradeMode"])
        self.assertTrue(live["VerifyServerHostname"])

        # Reusing the same node identity without its certificate must no longer
        # establish the callback RPC path once the server's transition is over.
        self.start("client")
        with self.assertRaises(LumaError):
            server.request("GET", path, timeout=3)
        self.start("client", tls=self.client_tls)
        self.wait_for(lambda: server.request("GET", path, timeout=2), "restored authenticated client RPC")
        self.assertEqual([node["ID"] for node in server.request("GET", "/v1/nodes")], [node_id])


if __name__ == "__main__":
    unittest.main()
