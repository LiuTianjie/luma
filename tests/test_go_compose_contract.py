"""Compose artifact parity with Python; no production Control or Nomad required."""
import copy
import json
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml
from luma.compose import load_compose_deployment, render_compose_routes
from luma.config import LumaConfig
from luma.nomad_render import render_compose_job
from tests.test_nomad_compose import GRANARY_COMPOSE, GRANARY_SIDECAR, REAL_GRANARY_COMPOSE, REAL_GRANARY_SIDECAR

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("go"), "Go toolchain required")
class GoComposeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.binary = cls.directory / "luma-go"
        subprocess.run(["go", "build", "-o", str(cls.binary), "./cmd/luma"], cwd=ROOT, check=True, capture_output=True)

    def write(self, sidecar, compose, config):
        source = self.directory / "luma.compose.yml"
        source.write_text(sidecar if isinstance(sidecar, str) else yaml.safe_dump(sidecar, sort_keys=False))
        (self.directory / "docker-compose.yml").write_text(compose if isinstance(compose, str) else yaml.safe_dump(compose, sort_keys=False))
        config_path = self.directory / "config.yml"
        config_path.write_text(yaml.safe_dump(config))
        return source, config_path

    def compare(self, sidecar, compose, config=None):
        config = config or {}
        source, config_path = self.write(sidecar, compose, config)
        deployment = load_compose_deployment(source)
        cfg = LumaConfig(config, config_path)
        expected = render_compose_job(cfg, deployment, as_json=False, resolve_secrets=False, node_records=config.get("nodes", {}))
        # Intentional correction: Python silently omitted a publishPort on an
        # internal Compose service. Go must actually reserve and map that port.
        group = expected["Job"]["TaskGroups"][0]
        for name, service in deployment.services.items():
            if service.exposure == "none" and service.publish_port:
                label = re.sub(r"[^a-zA-Z0-9_]", "_", name)
                network = group.setdefault("Networks", [{"Mode": "bridge"}])[0]
                network.setdefault("ReservedPorts", []).append(dict(Label=label, Value=service.publish_port, To=service.port, HostNetwork="default"))
                next(t for t in group["Tasks"] if t["Name"] == name)["Config"]["ports"] = [label]
        completed = subprocess.run([str(self.binary), "compose", "validate", str(source), "--config", str(config_path), "--format", "json"],
                                   cwd=self.directory, text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)["result"]
        self.assertEqual(json.loads(result["artifacts"][0]["content"]), expected)
        expected_routes = render_compose_routes(cfg, deployment)
        self.assertEqual(len(result["artifacts"]), 1 + len(expected_routes))
        from luma.service import slugify
        routes = {Path(a["path"]).name: yaml.safe_load(a["content"]) for a in result["artifacts"][1:]}
        for name, route in expected_routes.items():
            self.assertEqual(routes[f"{deployment.slug}-{slugify(name)}.yml"], yaml.safe_load(route))
        return result

    def test_existing_granary_corpus(self):
        self.compare(GRANARY_SIDECAR, GRANARY_COMPOSE)
        self.compare(REAL_GRANARY_SIDECAR, REAL_GRANARY_COMPOSE)

    def test_repository_observe_stack(self):
        self.compare((ROOT / "observe/luma.compose.yml").read_text(), (ROOT / "observe/docker-compose.yml").read_text())

    def test_exposure_network_health_and_rollout(self):
        for region, exposure in (("cn", "none"), ("cn", "cn-edge"), ("global", "external-edge"), ("home", "tailscale-relay"), ("cn", "tcp-relay")):
            for stateful in (False, True):
                for fixed in (False, True):
                    with self.subTest(exposure=exposure, stateful=stateful, fixed=fixed):
                        body = dict(image="nginx", command=["serve", "--port", 8080], environment={"SECRET": "${TOKEN}", "FLAG": True},
                                    healthcheck=dict(test=["CMD-SHELL", "curl http://localhost:8080/ready%2Ffull?test=1"], interval="2.5s", timeout="250ms"), stop_grace_period="45s")
                        if stateful:
                            body["volumes"] = ["data:/app/data", "/srv/config:/etc/app:ro"]
                        override = dict(exposure=exposure, domain="app.acme.test", port=8080)
                        if fixed:
                            override["publishPort"] = 18080
                        self.compare(dict(name="app-stack", region=region, services={"web-api": override}),
                                     dict(services={"web-api": body, "cache": dict(image="redis", environment=["EMPTY", "MESSAGE=a=b"], command="redis-server --save ''")}),
                                     dict(defaults={"acmeDomains": ["acme.test"]}))

    def test_internal_shared_and_host_network(self):
        for count in (1, 2):
            for mode in ("bridge", "host"):
                self.compare(dict(name="internal", region="cn"), dict(services={f"svc{i}": dict(image="app", network_mode=mode) for i in range(count)}))

    def test_mount_forms_and_local_storage(self):
        mounts = ["/anonymous", {"type": "volume", "target": "/anonymous2"}, "data:/db",
                  {"type": "bind", "source": "/srv/cfg", "target": "/cfg", "read_only": True}, "./file:/file:ro"]
        sidecar = dict(name="local", region="cn", services={"db": dict(node="dbnode")},
                       volumes={"data": dict(local=dict(path="/srv/app/db"), adopted=True)})
        self.compare(sidecar, dict(services={"db": dict(image="postgres", volumes=mounts)}))

    def test_removed_nfs_rejected_before_submission(self):
        for managed, overlay, cross_region in ((False, False, False), (True, False, False), (True, True, False), (True, True, True)):
            with self.subTest(managed=managed, overlay=overlay, cross=cross_region):
                cls = dict(provider="nfs", mode="managed" if managed else "external", regions=["cn"], nodes=["appnode"])
                cls.update(dict(node="storage", path="/exports/apps") if managed else dict(endpoint="nfs://external:/exports/apps"))
                node = dict(region="global" if cross_region else "cn", hostname="real-storage")
                if overlay:
                    node["tailscaleIP"] = "100.64.0.2"
                sidecar = dict(name="database", region="cn", storageClasses={"shared": cls},
                               volumes={"data": dict(storageClass="shared", path="tenant/db", initialize="empty")}, services={"db": dict(node="appnode")})
                source, config = self.write(sidecar, dict(services={"db": dict(image="postgres", volumes=["data:/db"])}), dict(nodes={"storage": node}))
                for command in ("validate", "deploy"):
                    args = [str(self.binary), "compose", command, str(source), "--config", str(config), "--format", "json"]
                    if command == "deploy":
                        args += ["--control-url", "https://127.0.0.1:1", "--token", "unused", "--no-env"]
                    completed = subprocess.run(args, cwd=self.directory, text=True, capture_output=True, timeout=10)
                    self.assertEqual(completed.returncode, 1)
                    self.assertIn("removed", completed.stdout)
                    self.assertFalse(json.loads(completed.stdout)["ok"])

    def test_discovery_addresses_and_disabled_checks(self):
        for address in ({"localIngress": True}, {"tailscaleIP": "100.64.0.9"}, {}):
            for check in ({"test": ["NONE"]}, {"test": ["CMD", "pg_isready"], "disable": True}, {"test": ["pg_isready"]}):
                self.compare(dict(name="web", region="cn", services={"web": dict(node="edge", exposure="cn-edge", domain="web.test", port=80)}),
                             dict(services={"web": dict(image="nginx", healthcheck=check)}), dict(nodes={"edge": address}))

    def test_rejects_invalid_compose_before_network(self):
        base = dict(name="app", region="cn", services={"web": dict(exposure="cn-edge", port=80, domain="app.test")})
        compose = dict(services={"web": dict(image="nginx"), "db": dict(image="postgres")})
        cases = []
        def case(change_sidecar=None, change_compose=None):
            sidecar, body = copy.deepcopy(base), copy.deepcopy(compose)
            if change_sidecar: change_sidecar(sidecar)
            if change_compose: change_compose(body)
            cases.append((sidecar, body))
        case(lambda s: s["services"].update(db=dict(node="b")) or s["services"]["web"].update(node="a"))
        case(lambda s: s["services"].update(missing={}))
        case(lambda s: s["services"]["web"].update(exposure="cloudflare-tunnel"))
        case(change_compose=lambda c: c["services"]["web"].update(network_mode="host"))
        case(change_compose=lambda c: c["services"]["web"].update(volumes=["bad:"]))
        case(lambda s: s.update(volumes={"data": dict(local=dict(path="/srv/../data"))}))
        case(lambda s: s.update(volumes={"data": dict(storageClass="missing")}))
        case(lambda s: s.update(volumes={"data": dict(accessMode="ReadWriteMany")}))
        case(change_compose=lambda c: c.update(volumes={"remote": dict(driver="local", driver_opts={"type": "nfs", "device": ":/data"})}))
        case(change_compose=lambda c: c["services"]["web"].update(environment={"COMPOSITE": [1, 2]}))
        case(change_compose=lambda c: c["services"]["web"].update(image=None, build="."))
        case(lambda s: s["services"]["web"].update(port=65536))
        for sidecar, body in cases:
            with self.subTest(sidecar=sidecar, compose=body):
                source, config = self.write(sidecar, body, {})
                result = subprocess.run([str(self.binary), "compose", "validate", str(source), "--config", str(config), "--format", "json"],
                                        cwd=self.directory, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertFalse(json.loads(result.stdout)["ok"])
