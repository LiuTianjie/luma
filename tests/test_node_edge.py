"""node-edge exposure: manifests, rendering, DNS targets and node ingress lifecycle."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from luma.compose import load_compose_deployment
from luma.config import LumaConfig
from luma.control import node_ingress
from luma.control.server import _dashboard_traffic_paths, _dns_config, handle_node_public_ingress
from luma.control.state import init_state, load_state, mutate_state
from luma.dependencies import service_requirements
from luma.errors import LumaError
from luma.nomad_render import NODE_EDGE_TAG, render_compose_job, render_node_traefik_job, render_nomad_job, render_traefik_job
from luma.regions import validate_region_exposure
from luma.service import load_service

MANIFEST = """
name: api
image: ghcr.io/acme/api:1
region: cn
node: tx-sh-1
exposure: node-edge
domain: api.acme.dev
port: 8080
redirectFrom: [old.acme.dev]
"""


def _config(**dns):
    base = {"provider": "cloudflare", "zone": "acme.dev", "zoneId": "z1", "edgeTarget": "198.51.100.1"}
    base.update(dns)
    return LumaConfig({"providers": {"dns": base}, "defaults": {"acmeEmail": "ops@acme.dev"}}, None)


def _load(text: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "app.yaml"
        path.write_text(text, encoding="utf-8")
        return load_service(path)


class NodeEdgeManifestTests(unittest.TestCase):
    def test_requires_node(self):
        with self.assertRaisesRegex(LumaError, "requires node"):
            _load(MANIFEST.replace("node: tx-sh-1\n", ""))

    def test_rejects_explicit_dns_target(self):
        with self.assertRaisesRegex(LumaError, "dns.target is not allowed"):
            _load(MANIFEST + "dns:\n  target: 203.0.113.9\n")

    def test_builtin_regions_allow_node_edge(self):
        validate_region_exposure("cn", "node-edge")
        validate_region_exposure("global", "node-edge")
        with self.assertRaisesRegex(LumaError, "does not allow"):
            validate_region_exposure("home", "node-edge")

    def test_compose_service_requires_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "compose.yml").write_text("services:\n  web:\n    image: nginx\n", encoding="utf-8")
            Path(tmp, "luma.compose.yml").write_text(
                "name: shop\ncompose: compose.yml\nregion: cn\nservices:\n  web:\n    exposure: node-edge\n    domain: shop.acme.dev\n    port: 80\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(LumaError, "node-edge requires node"):
                load_compose_deployment(Path(tmp, "luma.compose.yml"))


class NodeEdgeRenderTests(unittest.TestCase):
    def test_service_is_tagged_for_its_node_and_uses_http01(self):
        job = render_nomad_job(_config(), _load(MANIFEST), as_json=False)["Job"]
        block = job["TaskGroups"][0]["Services"][0]
        self.assertIn(f"{NODE_EDGE_TAG}=tx-sh-1", block["Tags"])
        self.assertIn("lumaedge.http.routers.api.tls.certresolver=letsencrypt-http", block["Tags"])
        # Redirect routers are served by the same node Traefik.
        self.assertIn("lumaedge.http.routers.api-redirect.tls.certresolver=letsencrypt-http", block["Tags"])
        # The Manager Traefik reads only traefik.* tags and must see none of them.
        self.assertFalse([tag for tag in block["Tags"] if tag.startswith("traefik.")])
        # No wildcard binding: the node's Traefik cannot run DNS-01.
        self.assertFalse([tag for tag in block["Tags"] if ".tls.domains[" in tag])
        self.assertEqual(block["AddressMode"], "host")
        self.assertNotIn("Address", block)
        constraints = job["Constraints"]
        self.assertIn({"LTarget": "${meta.luma_node_name}", "RTarget": "tx-sh-1", "Operand": "="}, constraints)
        self.assertNotIn("${meta.luma_tailscale_ip}", str(constraints))
        self.assertEqual(job["TaskGroups"][0]["Networks"][0]["DynamicPorts"][0]["To"], 8080)

    def test_cn_edge_keeps_dns01_resolver_and_wildcard(self):
        service = _load(MANIFEST.replace("exposure: node-edge", "exposure: cn-edge").replace("node: tx-sh-1\n", ""))
        tags = render_nomad_job(_config(), service, as_json=False)["Job"]["TaskGroups"][0]["Services"][0]["Tags"]
        self.assertIn("traefik.http.routers.api.tls.certresolver=letsencrypt", tags)
        self.assertIn("traefik.http.routers.api.tls.domains[0].main=*.acme.dev", tags)
        self.assertFalse([tag for tag in tags if tag.startswith(NODE_EDGE_TAG)])

    def test_compose_node_edge_service_is_tagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "compose.yml").write_text("services:\n  web:\n    image: nginx\n", encoding="utf-8")
            Path(tmp, "luma.compose.yml").write_text(
                "name: shop\ncompose: compose.yml\nregion: cn\nservices:\n  web:\n    node: tx-sh-1\n    exposure: node-edge\n    domain: shop.acme.dev\n    port: 80\n",
                encoding="utf-8",
            )
            deployment = load_compose_deployment(Path(tmp, "luma.compose.yml"))
            job = render_compose_job(_config(), deployment, as_json=False)["Job"]
        block = job["TaskGroups"][0]["Services"][0]
        self.assertIn(f"{NODE_EDGE_TAG}=tx-sh-1", block["Tags"])
        self.assertIn("lumaedge.enable=true", block["Tags"])
        self.assertFalse([tag for tag in block["Tags"] if tag.startswith("traefik.")])
        self.assertEqual(block["AddressMode"], "host")

    def test_manager_traefik_ignores_node_edge_services_and_adds_http_resolver(self):
        args = render_traefik_job(
            image="traefik:v3", acme_email="ops@acme.dev", acme_dns_provider="cloudflare",
            acme_dns_token_file="/run/token", http_cert_resolver="letsencrypt-http", as_json=False,
        )["Job"]["TaskGroups"][0]["Tasks"][0]["Config"]["args"]
        # node-edge routers use another tag prefix, so the Manager job needs no
        # extra constraint and is unchanged for clusters without node-edge.
        self.assertFalse([arg for arg in args if arg.startswith(("--providers.nomad.constraints", "--providers.nomad.prefix"))])
        self.assertIn("--certificatesresolvers.letsencrypt-http.acme.httpchallenge=true", args)
        self.assertIn("--certificatesresolvers.letsencrypt.acme.dnschallenge=true", args)

    def test_manager_adds_http_resolver_only_for_non_cloudflare_zones(self):
        from luma.bootstrap import _manager_http_cert_resolver

        config = _config()
        # No zones, or Cloudflare-only zones: the Manager job is unchanged.
        self.assertEqual(_manager_http_cert_resolver(config, None), "")
        self.assertEqual(_manager_http_cert_resolver(config, {"dnsZones": {"acme.dev": {"provider": "cloudflare"}}}), "")
        self.assertEqual(
            _manager_http_cert_resolver(config, {"dnsZones": {"acme.dev": {"provider": "cloudflare"}, "acme.cn": {"provider": "alidns"}}}),
            config.http_cert_resolver,
        )

    def test_node_traefik_job(self):
        job = render_node_traefik_job(node="tx-sh-1", image="traefik:v3", acme_email="ops@acme.dev", cert_resolver="letsencrypt-http", as_json=False)["Job"]
        self.assertEqual(job["ID"], "traefik-node-tx-sh-1")
        self.assertEqual(job["Constraints"], [{"LTarget": "${meta.luma_node_name}", "RTarget": "tx-sh-1", "Operand": "="}])
        task = job["TaskGroups"][0]["Tasks"][0]
        self.assertIn("--providers.nomad.constraints=Tag(`luma.edge=tx-sh-1`)", task["Config"]["args"])
        self.assertIn("--providers.nomad.prefix=lumaedge", task["Config"]["args"])
        self.assertIn("--certificatesresolvers.letsencrypt-http.acme.httpchallenge.entrypoint=web", task["Config"]["args"])
        self.assertFalse([arg for arg in task["Config"]["args"] if "dnschallenge" in arg or "/dynamic" in arg])
        ports = {item["Label"]: item["Value"] for item in job["TaskGroups"][0]["Networks"][0]["ReservedPorts"]}
        self.assertEqual((ports["web"], ports["websecure"]), (80, 443))

    def test_node_traefik_requires_acme_email(self):
        with self.assertRaisesRegex(LumaError, "ACME email"):
            render_node_traefik_job(node="n", image="traefik", acme_email="", cert_resolver="letsencrypt-http")


class NodeEdgeTargetTests(unittest.TestCase):
    def test_target_is_the_node_and_never_the_manager(self):
        service = _load(MANIFEST)
        self.assertIsNone(_config().service_dns_target(exposure="node-edge", region="cn", node="tx-sh-1"))
        config = _config(nodeTargets={"tx-sh-1": "47.100.1.20"})
        self.assertEqual(config.service_dns_target(exposure=service.exposure, region="cn", node=service.node), "47.100.1.20")
        self.assertEqual(config.service_dns_target(exposure="cn-edge", region="cn"), "198.51.100.1")

    def test_requirements_block_until_node_ingress_is_enabled(self):
        service = _load(MANIFEST)
        state = {"nodes": {}, "secrets": {"CLOUDFLARE_API_TOKEN": "t"}}
        checks = {item["kind"]: item for item in service_requirements(service, state, _config())["checks"]}
        self.assertEqual(checks["node-ingress"]["status"], "missing")
        self.assertTrue(checks["node-ingress"]["explicit"])
        ready = {item["kind"]: item for item in service_requirements(service, state, _config(nodeTargets={"tx-sh-1": "47.100.1.20"}))["checks"]}
        self.assertEqual(ready["node-ingress"]["status"], "ready")

    def test_traffic_path_shows_the_node(self):
        paths = _dashboard_traffic_paths(
            [{"name": "api", "exposure": "node-edge", "domain": "api.acme.dev", "nodes": ["tx-sh-1"], "targetPort": 8080}],
            {}, "198.51.100.1", node_targets={"tx-sh-1": "47.100.1.20"},
        )
        self.assertEqual(paths[0]["segments"][:3], ["DNS", "47.100.1.20", "Traefik on node"])


class NodeIngressHelperTests(unittest.TestCase):
    def test_public_ip_validation(self):
        self.assertEqual(node_ingress.parse_public_ip("47.100.1.20"), "47.100.1.20")
        for value in ("10.0.0.5", "100.64.1.2", "127.0.0.1"):
            with self.subTest(value=value), self.assertRaisesRegex(LumaError, "not a public address"):
                node_ingress.parse_public_ip(value)
        with self.assertRaisesRegex(LumaError, "--public-ip"):
            node_ingress.parse_public_ip("")

    def test_probe_summary_explains_security_group_and_icp(self):
        status, detail = node_ingress.probe_summary("47.100.1.20", {80: "timed out", 443: ""})
        self.assertEqual(status, "unreachable")
        self.assertIn("security group", detail)
        self.assertIn("ICP", detail)
        self.assertEqual(node_ingress.probe_summary("47.100.1.20", {80: "", 443: ""})[0], "ready")


class NodeIngressHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": self.tmp.name, "TRAEFIK_ACME_EMAIL": "ops@acme.dev"})
        self.env.start()
        state = init_state(domain="luma.acme.dev", cluster_id="test", overwrite=True)
        self.token = state["deployToken"]

        def seed(current):
            current["nodes"] = {
                "tx-sh-1": {"region": "cn", "status": "ready", "labels": {"luma.node.name": "tx-sh-1"}, "agent": {"os": "linux"}},
                "manager": {"region": "cn", "status": "manager", "labels": {"luma.node.name": "manager"}},
            }

        mutate_state(seed)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _enable(self, body, probe=None):
        with patch("luma.control.server.deploy_to_nomad", return_value="traefik-node-tx-sh-1 healthy") as deploy, patch(
            "luma.control.node_ingress.probe_ports", return_value=probe or {80: "", 443: ""},
        ):
            result = handle_node_public_ingress(self.token, body)
        return result, deploy

    def test_enable_records_address_and_deploys_node_traefik(self):
        result, deploy = self._enable({"node": "tx-sh-1", "publicIp": "47.100.1.20"})
        self.assertEqual(result["publicIngress"]["status"], "ready")
        job = deploy.call_args.args[1]
        self.assertIn("traefik-node-tx-sh-1", job)
        stored = load_state()["nodes"]["tx-sh-1"]["publicIngress"]
        self.assertEqual((stored["enabled"], stored["publicIp"], stored["nodeName"]), (True, "47.100.1.20", "tx-sh-1"))
        config = _dns_config(_config(), load_state())
        self.assertEqual(config.node_edge_target("tx-sh-1"), "47.100.1.20")

    def test_unreachable_ports_still_enable_with_a_clear_status(self):
        result, _deploy = self._enable({"node": "tx-sh-1", "publicIp": "47.100.1.20"}, probe={80: "timed out", 443: "timed out"})
        self.assertEqual(result["publicIngress"]["status"], "unreachable")
        self.assertTrue(load_state()["nodes"]["tx-sh-1"]["publicIngress"]["enabled"])

    def test_manager_and_private_addresses_are_rejected(self):
        with self.assertRaisesRegex(LumaError, "is the Manager"):
            self._enable({"node": "manager", "publicIp": "47.100.1.20"})
        with self.assertRaisesRegex(LumaError, "not a public address"):
            self._enable({"node": "tx-sh-1", "publicIp": "10.0.0.5"})

    def test_disable_refuses_while_a_deployment_uses_the_node(self):
        self._enable({"node": "tx-sh-1", "publicIp": "47.100.1.20"})

        def deploy(current):
            current.setdefault("deployments", {}).setdefault("services", {})["api"] = {"name": "api", "manifest": yaml.safe_dump(yaml.safe_load(MANIFEST))}

        mutate_state(deploy)
        with patch("luma.control.server.NomadApi") as api, self.assertRaisesRegex(LumaError, "still serves node-edge deployments: api"):
            handle_node_public_ingress(self.token, {"node": "tx-sh-1", "enabled": False})
        api.return_value.request.assert_not_called()

    def test_disable_stops_the_job_and_clears_the_record(self):
        self._enable({"node": "tx-sh-1", "publicIp": "47.100.1.20"})
        with patch("luma.control.server.NomadApi") as api:
            result = handle_node_public_ingress(self.token, {"node": "tx-sh-1", "enabled": False})
        self.assertFalse(result["publicIngress"]["enabled"])
        api.return_value.request.assert_called_once_with("DELETE", "/v1/job/traefik-node-tx-sh-1?purge=true")
        self.assertNotIn("publicIngress", load_state()["nodes"]["tx-sh-1"])

    def test_rerun_reuses_the_saved_address(self):
        self._enable({"node": "tx-sh-1", "publicIp": "47.100.1.20"})
        result, _deploy = self._enable({"node": "tx-sh-1"})
        self.assertEqual(result["publicIngress"]["publicIp"], "47.100.1.20")


if __name__ == "__main__":
    unittest.main()
