"""node-edge exposure: manifests, rendering, DNS targets and node ingress lifecycle."""

import base64
import copy
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import yaml

from luma import nomad_secrets, nomad_upgrade, secret_storage
from luma.compose import load_compose_deployment
from luma.config import LumaConfig
from luma.control import node_ingress
from luma.control.server import _dashboard_traffic_paths, _dns_config, handle_node_public_ingress
from luma.control.state import init_state, load_state, mutate_state
from luma.dependencies import service_requirements
from luma.errors import LumaError
from luma.nomad_migration_node import CAPABILITY as SECURITY_UPGRADE_CAPABILITY
from luma.nomad_render import NODE_EDGE_TAG, render_compose_job, render_node_traefik_job, render_nomad_job, render_traefik_job
from luma.nomad_security import admit_job, private_write
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
        self.assertIn("--certificatesresolvers.letsencrypt.acme.dnschallenge=true", args)
        self.assertIn("--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json", args)
        self.assertIn("--certificatesresolvers.letsencrypt-http.acme.httpchallenge=true", args)
        self.assertIn("--certificatesresolvers.letsencrypt-http.acme.storage=/letsencrypt/acme-letsencrypt-http.json", args)
        storage_flags = [arg for arg in args if ".acme.storage=" in arg]
        self.assertEqual(len(storage_flags), len({arg.split("=", 1)[1] for arg in storage_flags}))

    def test_manager_always_exposes_http_resolver_for_added_zones(self):
        from luma.bootstrap import _manager_http_cert_resolver

        config = _config()
        self.assertEqual(_manager_http_cert_resolver(config, None), config.http_cert_resolver)
        self.assertEqual(_manager_http_cert_resolver(config, {"dnsZones": {"acme.dev": {"provider": "cloudflare"}}}), config.http_cert_resolver)

    def test_node_traefik_job(self):
        job = render_node_traefik_job(node="tx-sh-1", image="traefik:v3", acme_email="ops@acme.dev", cert_resolver="letsencrypt-http", as_json=False)["Job"]
        self.assertEqual(job["ID"], "traefik-node-tx-sh-1")
        self.assertEqual(job["Constraints"], [{"LTarget": "${meta.luma_node_name}", "RTarget": "tx-sh-1", "Operand": "="}])
        task = job["TaskGroups"][0]["Tasks"][0]
        self.assertIn("--providers.nomad.constraints=Tag(`luma.edge=tx-sh-1`)", task["Config"]["args"])
        self.assertIn("--providers.nomad.prefix=lumaedge", task["Config"]["args"])
        self.assertIn("--certificatesresolvers.letsencrypt-http.acme.httpchallenge.entrypoint=web", task["Config"]["args"])
        self.assertIn("--certificatesresolvers.letsencrypt-http.acme.storage=/letsencrypt/acme.json", task["Config"]["args"])
        self.assertEqual(task["Config"]["mount"], [{"type": "volume", "target": "/letsencrypt", "source": "traefik-node-tx-sh-1_letsencrypt"}])
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


class _FakeSecuredNomad:
    """In-memory Nomad API simulating nodes, encrypted Variables and jobs."""

    def __init__(self):
        self.variables = {}
        self.jobs = {}
        self.index = 0
        self.host_access = "isolated"

    def _error(self, code):
        cause = urllib.error.HTTPError("http://fixture", code, "fixture", {}, io.BytesIO())
        raise LumaError(f"Nomad API error {code}: fixture") from cause

    def request(self, method, route, body=None, **_kwargs):
        parsed = urllib.parse.urlsplit(route)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        namespace = query.get("namespace", ["default"])[0]
        if path == "/v1/nodes":
            return [{"ID": "node-tx-1", "Name": "tx-sh-1", "Status": "ready"}]
        if path == "/v1/node/node-tx-1":
            return {
                "ID": "node-tx-1",
                "Name": "tx-sh-1",
                "Status": "ready",
                "SchedulingEligibility": "eligible",
                "Drain": False,
                "NodePool": "default",
                "Datacenter": "dc1",
                "Drivers": {"docker": {"Detected": True, "Healthy": True}},
                "Meta": {
                    "luma_node_name": "tx-sh-1",
                    "luma_tailscale_ip": "100.64.0.20",
                    "luma_host_access": self.host_access,
                },
            }
        if path.startswith("/v1/var/"):
            var_path = urllib.parse.unquote(path.removeprefix("/v1/var/"))
            key = (namespace, var_path)
            previous = self.variables.get(key)
            if method == "GET":
                if previous is None:
                    self._error(404)
                return copy.deepcopy(previous)
            if method == "PUT":
                if int(query.get("cas", ["0"])[0]) != int((previous or {}).get("ModifyIndex") or 0):
                    self._error(409)
                self.index += 1
                self.variables[key] = {
                    "Namespace": namespace, "Path": var_path,
                    "Items": copy.deepcopy(body["Items"]),
                    "ModifyIndex": self.index, "ModifyTime": time.time_ns(),
                }
                return copy.deepcopy(self.variables[key])
            if method == "DELETE":
                if previous is None:
                    self._error(404)
                if int(query.get("cas", ["0"])[0]) != int(previous.get("ModifyIndex") or 0):
                    self._error(409)
                del self.variables[key]
                return {}
        if path == "/v1/vars":
            prefix = query.get("prefix", [""])[0]
            return [{"Path": v["Path"]} for (ns, p), v in self.variables.items() if ns == namespace and p.startswith(prefix)]
        if path == "/v1/jobs":
            if method == "GET":
                return [{"ID": jid, "Namespace": "default", "Status": "running", "Stop": False} for jid in self.jobs]
            if method == "POST":
                job = copy.deepcopy(body["Job"])
                self.jobs[job["ID"]] = job
                return {"EvalID": "eval-1", "JobModifyIndex": 1}
        if path == "/v1/allocations":
            return [{"JobID": jid, "NodeID": "node-tx-1", "ClientStatus": "running"} for jid in self.jobs]
        if path.startswith("/v1/job/"):
            jid = urllib.parse.unquote(path.removeprefix("/v1/job/"))
            if method == "GET":
                if jid not in self.jobs:
                    self._error(404)
                return copy.deepcopy(self.jobs[jid])
            if method == "DELETE":
                self.jobs.pop(jid, None)
                return {}
        raise AssertionError(f"unexpected Nomad request: {method} {route}")


class NodeIngressHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.security = Path(self.tmp.name, "security")
        self.stacks = Path(self.tmp.name, "stacks")
        self.config_path = Path(self.tmp.name, "luma.yaml")
        self.config_path.write_text(
            yaml.safe_dump({
                "defaults": {"stackRoot": str(self.stacks), "acmeEmail": "ops@acme.dev"},
                "providers": {"dns": {"provider": "cloudflare", "zone": "acme.dev", "zoneId": "z1", "edgeTarget": "198.51.100.1"}},
            }),
            encoding="utf-8",
        )
        self.env = patch.dict(os.environ, {
            "LUMA_CONTROL_STATE_DIR": self.tmp.name,
            "LUMA_CONTROL_CONFIG": str(self.config_path),
            "TRAEFIK_ACME_EMAIL": "ops@acme.dev",
            "LUMA_NOMAD_SECURITY_DIR": str(self.security),
        })
        self.env.start()
        secret_storage._keys.clear()
        state = init_state(domain="luma.acme.dev", cluster_id="test", overwrite=True)
        self.token = state["deployToken"]

        def seed(current):
            current["nodes"] = {
                "tx-sh-1": {
                    "region": "cn", "status": "ready", "nomadNodeId": "node-tx-1", "tailscaleIP": "100.64.0.20",
                    "labels": {"luma.node.name": "tx-sh-1"},
                    "agent": {"os": "linux", "status": "ready", "lastSeen": int(time.time()), "capabilities": [SECURITY_UPGRADE_CAPABILITY]},
                },
                "manager": {"region": "cn", "status": "manager", "nomadNodeId": "node-mgr-1", "tailscaleIP": "100.64.0.1", "labels": {"luma.node.name": "manager"}},
            }

        mutate_state(seed)

    def tearDown(self):
        secret_storage._keys.clear()
        self.env.stop()
        self.tmp.cleanup()

    def _enable(self, body, probe=None):
        with patch("luma.control.server.deploy_to_nomad", return_value="traefik-node-tx-sh-1 healthy") as deploy, patch(
            "luma.control.node_ingress.probe_ports", return_value=probe or {80: "", 443: ""},
        ):
            result = handle_node_public_ingress(self.token, body)
        return result, deploy

    def _stage_security(self, fake_nomad):
        self.security.mkdir(mode=0o700, exist_ok=True)
        private_write(self.security / "cluster.json", json.dumps({"schema": 1, "aclRequired": True, "tlsRequired": True}))
        private_write(self.security / "transport.json", json.dumps({"http": True, "address": "100.64.0.1"}))
        private_write(self.security / "credentials.json", json.dumps({"schema": 1, "stage": "active"}))
        private_write(self.security / "workloads.json", json.dumps({"schema": 1, "mode": "enforce", "grants": {}}))
        private_write(self.security / "control.d" / "token", "control-role-secret-token\n", mode=0o600)
        for fname, content in (
            ("token", "traefik-role-secret-token\n"),
            ("ca.pem", "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n"),
            ("cert.pem", "-----BEGIN CERTIFICATE-----\nCERT\n-----END CERTIFICATE-----\n"),
            ("key.pem", "-----BEGIN PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----\n"),
        ):
            private_write(self.security / "traefik.d" / fname, content, mode=0o600)
        key_doc = {"schema": 1, "id": "fixture-key", "key": base64.b64encode(bytes(range(32))).decode()}
        fake_nomad.variables["default", secret_storage.KEY_VARIABLE] = {
            "Namespace": "default", "Path": secret_storage.KEY_VARIABLE,
            "Items": {"id": key_doc["id"], "key": key_doc["key"]}, "ModifyIndex": 1,
        }
        private_write(
            secret_storage.profile_path(),
            json.dumps({"schema": 1, "format": secret_storage.FORMAT, "keyId": key_doc["id"], "keyVariable": secret_storage.KEY_VARIABLE}),
        )

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

    def test_enable_is_refused_during_running_migration_or_unprovisioned_secrets(self):
        self.security.mkdir(mode=0o700, exist_ok=True)
        private_write(self.security / "upgrade-status.json", json.dumps({"status": "running"}))
        with self.assertRaisesRegex(LumaError, "Nomad security migration"):
            self._enable({"node": "tx-sh-1", "publicIp": "47.100.1.20"})
        (self.security / "upgrade-status.json").unlink()
        private_write(self.security / "cluster.json", json.dumps({"schema": 1, "aclRequired": True}))
        with self.assertRaisesRegex(LumaError, "encrypted task secrets"):
            self._enable({"node": "tx-sh-1", "publicIp": "47.100.1.20"})
        self.assertNotIn("publicIngress", load_state()["nodes"]["tx-sh-1"])

    def test_security_profile_encrypts_credentials_promotes_node_and_cleans_up_on_disable(self):
        fake = _FakeSecuredNomad()
        self._stage_security(fake)
        storage_calls = []

        def fake_agent_task(_state, node_name, task_name, payload, **_kwargs):
            storage_calls.append((node_name, task_name, payload))
            fake.host_access = "trusted"
            return {"verified": True, "nodeId": "node-tx-1", "trustedHost": True}

        def fake_deploy(_config, job_json, _state, *, slug):
            job = json.loads(job_json)["Job"]
            fake.jobs[slug] = copy.deepcopy(job)
            return f"{slug} healthy"

        with patch.object(secret_storage, "_client", return_value=fake), patch(
            "luma.control.server.NomadApi", return_value=fake
        ), patch(
            "luma.nomad_upgrade.NomadApi", return_value=fake
        ), patch(
            "luma.control.server._run_node_agent_task", side_effect=fake_agent_task
        ), patch(
            "luma.control.server.deploy_to_nomad", side_effect=fake_deploy
        ), patch(
            "luma.control.node_ingress.probe_ports", return_value={80: "", 443: ""}
        ):
            result = handle_node_public_ingress(self.token, {"node": "tx-sh-1", "publicIp": "47.100.1.20"})
            self.assertEqual(result["publicIngress"]["status"], "ready")
            self.assertEqual(len(storage_calls), 1)
            self.assertEqual(storage_calls[0][1], "nomad-security")
            self.assertEqual(storage_calls[0][2]["op"], "storage")
            submitted = fake.jobs["traefik-node-tx-sh-1"]
            raw_job = json.dumps(submitted)
            self.assertNotIn("traefik-role-secret-token", raw_job)
            self.assertNotIn("BEGIN PRIVATE KEY", raw_job)
            nomad_secrets.assert_protected_job(submitted)
            admit_job(submitted)
            var_items = fake.variables["default", "nomad/jobs/traefik-node-tx-sh-1/traefik/traefik"]["Items"]
            decoded = [json.loads(val) for val in var_items.values()]
            self.assertIn("traefik-role-secret-token", decoded)
            self.assertTrue(any("BEGIN PRIVATE KEY" in item for item in decoded))
            self.assertTrue(any("/secrets/nomad-tls/key.pem" in item for item in decoded))
            stack_file = self.stacks / "node-edge" / "traefik-node-tx-sh-1" / "traefik-node-tx-sh-1.nomad.json"
            self.assertTrue(stack_file.is_file())
            self.assertIn("default/traefik-node-tx-sh-1", json.loads((self.security / "workloads.json").read_text())["grants"])
            nodes_map = {
                "manager": {"nodeId": "node-mgr-1", "role": "server"},
                "tx-sh-1": {"nodeId": "node-tx-1", "role": "client"},
            }
            inventoried, mounts = nomad_upgrade.inventory(load_state(), nodes_map)
            self.assertEqual([item["Job"]["ID"] for item in inventoried], ["traefik-node-tx-sh-1"])
            self.assertTrue(mounts["tx-sh-1"]["trustedHost"])

            disabled = handle_node_public_ingress(self.token, {"node": "tx-sh-1", "enabled": False})
            self.assertFalse(disabled["publicIngress"]["enabled"])
            self.assertNotIn("traefik-node-tx-sh-1", fake.jobs)
            self.assertNotIn(("default", "nomad/jobs/traefik-node-tx-sh-1/traefik/traefik"), fake.variables)
            self.assertNotIn("default/traefik-node-tx-sh-1", json.loads((self.security / "workloads.json").read_text())["grants"])
            self.assertFalse(stack_file.exists())

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

    def test_rerun_reuses_the_saved_address_and_moves_native_plus_compose_dns_on_ip_change(self):
        self._enable({"node": "tx-sh-1", "publicIp": "47.100.1.20"})
        result, _deploy = self._enable({"node": "tx-sh-1"})
        self.assertEqual(result["publicIngress"]["publicIp"], "47.100.1.20")

        compose_manifest = yaml.safe_dump({
            "name": "eye", "compose": "docker-compose.yml", "region": "cn",
            "services": {"web": {"node": "tx-sh-1", "exposure": "node-edge", "domain": "eye.acme.dev", "port": 8080}},
        })
        compose_yml = yaml.safe_dump({"services": {"web": {"image": "nginx:1.27"}}})

        def seed_deployments(current):
            deps = current.setdefault("deployments", {})
            deps.setdefault("services", {})["api"] = {"name": "api", "manifest": yaml.safe_dump(yaml.safe_load(MANIFEST))}
            deps.setdefault("compose", {})["eye"] = {"name": "eye", "manifest": compose_manifest, "composeContent": compose_yml}

        mutate_state(seed_deployments)
        synced = []

        def fake_sync(cfg, svc, **_kwargs):
            target = cfg.service_dns_target(exposure=svc.exposure, region=svc.region, node=svc.node)
            synced.append((svc.domain, target))
            return f"DNS updated: {svc.domain} -> {target}"

        with patch("luma.control.server.deploy_to_nomad", return_value="traefik-node-tx-sh-1 healthy"), patch(
            "luma.control.node_ingress.probe_ports", return_value={80: "", 443: ""}
        ), patch("luma.control.server.sync_dns", side_effect=fake_sync):
            moved = handle_node_public_ingress(self.token, {"node": "tx-sh-1", "publicIp": "47.100.1.25"})
        self.assertEqual(moved["publicIngress"]["publicIp"], "47.100.1.25")
        self.assertEqual(
            sorted(synced),
            [("api.acme.dev", "47.100.1.25"), ("eye.acme.dev", "47.100.1.25"), ("old.acme.dev", "47.100.1.25")],
        )


if __name__ == "__main__":
    unittest.main()
