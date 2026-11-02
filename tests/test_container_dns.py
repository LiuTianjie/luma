import json
import tempfile
import unittest
from pathlib import Path

import yaml

from luma.compose import load_compose_deployment
from luma.config import LumaConfig
from luma.errors import LumaError
from luma.nomad_render import render_compose_job, render_nomad_job
from luma.service import load_service


class ContainerDnsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = LumaConfig({}, None)
        self.servers = ["100.100.2.136", "100.100.2.138"]

    def compose(self, services, *, overrides=None):
        (self.root / "compose.yml").write_text(yaml.safe_dump({"services": services}))
        path = self.root / "luma.compose.yml"
        path.write_text(yaml.safe_dump({
            "name": "workers", "compose": "compose.yml", "region": "cn",
            "services": overrides or {name: {"exposure": "none"} for name in services},
        }))
        return load_compose_deployment(path)

    def native(self, exposure, dns):
        manifest = {"name": "worker", "image": "worker:1", "region": "cn", "dns": dns}
        if exposure != "none":
            manifest.update(exposure=exposure, domain="worker.example.test", port=8080)
        path = self.root / "worker.yml"
        path.write_text(yaml.safe_dump(manifest))
        return load_service(path)

    def test_internal_worker_resolvers_reach_docker_and_default_stays_inherited(self):
        for dns, expected in ((self.servers, self.servers), ("2001:db8::53", ["2001:db8::53"]), (None, None)):
            with self.subTest(dns=dns):
                body = {"image": "worker:1"}
                if dns is not None:
                    body["dns"] = dns
                deployment = self.compose({"worker": body})
                group = render_compose_job(self.config, deployment, as_json=False)["Job"]["TaskGroups"][0]
                self.assertNotIn("Networks", group)
                self.assertEqual(group["Tasks"][0]["Config"].get("dns_servers"), expected)

    def test_host_network_keeps_service_resolvers_independent(self):
        deployment = self.compose({
            "worker": {"image": "worker:1", "network_mode": "host", "dns": self.servers},
            "other": {"image": "other:1", "network_mode": "host"},
        })
        group = render_compose_job(self.config, deployment, as_json=False)["Job"]["TaskGroups"][0]
        tasks = {task["Name"]: task for task in group["Tasks"]}
        self.assertEqual(tasks["worker"]["Config"]["dns_servers"], self.servers)
        self.assertNotIn("dns_servers", tasks["other"]["Config"])

    def test_shared_cni_network_owns_resolver_configuration(self):
        deployment = self.compose({
            name: {"image": f"{name}:1", "dns": self.servers} for name in ("worker", "database")
        })
        group = render_compose_job(self.config, deployment, as_json=False)["Job"]["TaskGroups"][0]
        self.assertEqual(group["Networks"][0]["DNS"], {"Servers": self.servers})
        for task in group["Tasks"]:
            self.assertNotIn("dns_servers", task["Config"])

    def test_shared_network_rejects_different_or_partially_declared_dns(self):
        for other in (None, ["1.1.1.1"]):
            with self.subTest(other=other):
                deployment = self.compose({
                    "worker": {"image": "worker:1", "dns": self.servers},
                    "database": {"image": "database:1", "dns": other},
                })
                with self.assertRaisesRegex(LumaError, "must declare the same dns"):
                    render_compose_job(self.config, deployment)

    def test_native_service_routes_dns_to_the_network_owner(self):
        for exposure in ("none", "tailscale-relay", "cn-edge"):
            with self.subTest(exposure=exposure):
                service = self.native(exposure, {"servers": self.servers})
                group = render_nomad_job(self.config, service, as_json=False)["Job"]["TaskGroups"][0]
                task = group["Tasks"][0]
                if exposure == "cn-edge":
                    self.assertEqual(group["Networks"][0]["DNS"], {"Servers": self.servers})
                    self.assertNotIn("dns_servers", task["Config"])
                else:
                    self.assertEqual(task["Config"]["dns_servers"], self.servers)

    def test_invalid_resolvers_fail_during_manifest_validation(self):
        for dns in ({"servers": self.servers}, 53, [True], ["dns.example.test"], ["100.100.2.999"]):
            with self.subTest(dns=dns):
                with self.assertRaisesRegex(LumaError, "DNS server IP address|list of IP addresses"):
                    self.compose({"worker": {"image": "worker:1", "dns": dns}})
                with self.assertRaisesRegex(LumaError, "DNS server IP address|list of IP addresses"):
                    self.native("none", {"servers": dns})

    def test_mac_shared_network_passes_dns_to_namespace_owner(self):
        from luma.compose_network import NETWORK_TASKS

        deployment = self.compose(
            {name: {"image": f"{name}:1", "dns": self.servers} for name in ("worker", "database")},
            overrides={"worker": {"node": "mac"}},
        )
        nodes = {"mac": {"name": "mac", "region": "cn", "agent": {"os": "darwin"}}}
        group = render_compose_job(self.config, deployment, as_json=False, node_records=nodes)["Job"]["TaskGroups"][0]
        tasks = {task["Name"]: task for task in group["Tasks"]}
        args = tasks[NETWORK_TASKS[0]]["Config"]["args"]
        self.assertEqual(json.loads(args[args.index("--dns-servers") + 1]), self.servers)
        for name in ("worker", "database"):
            self.assertNotIn("dns_servers", tasks[name]["Config"])
        self.assertNotIn("DNS", group["Networks"][0])
