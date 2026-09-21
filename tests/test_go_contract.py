"""Cross-language contracts. Python is the reference, never a Go runtime dependency."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import yaml

from luma.config import LumaConfig
from luma.nomad_render import render_nomad_job
from luma.render import render_tailscale_route, render_tcp_route
from luma.service import load_service
from luma.deploy_workflow import validate_recipe

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("go"), "Go toolchain required for migration contracts")
class GoDeploymentContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.binary = cls.directory / "luma-go"
        subprocess.run(["go", "build", "-o", str(cls.binary), "./cmd/luma"], cwd=ROOT, check=True, capture_output=True)

    def compare(self, raw, config=None):
        config = config or {}
        source = self.directory / "service.yaml"
        config_path = self.directory / "config.yaml"
        source.write_text(raw if isinstance(raw, str) else yaml.safe_dump(raw))
        config_path.write_text(yaml.safe_dump(config))
        expected_service = load_service(source)
        expected_config = LumaConfig(config, config_path)
        expected = render_nomad_job(expected_config, expected_service, as_json=False, resolve_secrets=False)
        if expected_service.exposure == "cloudflare-tunnel" and not expected_service.publish_port:
            # Intentional Go correction: the legacy host-mode group separated
            # app/cloudflared Docker namespaces. Tunnel origins use localhost
            # within one bridge allocation and do not need a public host port.
            group = expected["Job"]["TaskGroups"][0]
            group["Networks"] = [{"Mode": "bridge"}]
            group["Tasks"][0]["Config"].pop("ports", None)
        completed = subprocess.run(
            [str(self.binary), "validate", str(source), "--config", str(config_path), "--format", "json"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)["result"]
        self.assertEqual(json.loads(result["artifacts"][0]["content"]), expected)
        if expected_service.exposure in {"tailscale-relay", "tcp-relay"}:
            render = render_tailscale_route if expected_service.exposure == "tailscale-relay" else render_tcp_route
            self.assertEqual(yaml.safe_load(result["artifacts"][1]["content"]), yaml.safe_load(render(expected_config, expected_service)))
        else:
            self.assertEqual(len(result["artifacts"]), 1)
        return result

    def test_repository_templates_and_examples(self):
        files = sorted((ROOT / "templates").glob("*.yml")) + sorted((ROOT / "examples").glob("*.yaml"))
        self.assertGreaterEqual(len(files), 14)
        for source in files:
            with self.subTest(source=source.name):
                self.compare(source.read_text())

    def test_exposure_network_and_rollout_matrix(self):
        for region, exposure in (("cn", "none"), ("cn", "cn-edge"), ("global", "external-edge"),
                                 ("home", "tailscale-relay"), ("cn", "tcp-relay"), ("global", "cloudflare-tunnel")):
            for fixed in (False, True):
                for stateful in (False, True):
                    with self.subTest(region=region, exposure=exposure, fixed=fixed, stateful=stateful):
                        spec = dict(name="Matrix Service", image="nginx:alpine", region=region, exposure=exposure,
                                    domain="test.acme.test", port=8080,
                                    healthcheck={"test": ["CMD-SHELL", "curl -f http://localhost:8080/ready?full=1"], "interval": "2.5s", "timeout": "500ms"})
                        if fixed:
                            spec["publishPort"] = 18080
                        if stateful:
                            spec["volumes"] = ["database:/var/lib/db", "/srv/config:/etc/app:ro"]
                        self.compare(spec)

    def test_resources_constraints_secrets_and_empty_command(self):
        for resources in ({}, {"limits": {"memory": "128MiB", "cpus": 2}},
                          {"limits": {"memory": "2G"}, "reservations": {"cpus": 0.1005, "memory": "512M"}}):
            with self.subTest(resources=resources):
                self.compare(dict(name="worker", image="busybox", region="batch-a", replicas="3", command=[],
                                  resources=resources, node="builder",
                                  constraints=["node.labels.disk == ssd", "${attr.cpu.arch} != arm64"],
                                  env={"SECRET": "prefix-${TOKEN}", "COUNT": 42, "FLAG": True, "EMPTY": None}))
        self.compare("name: worker\nimage: busybox\nregion: cn\nenv:\n  FEATURE: yes\n  DISABLED: off\n  TEXT: 'yes'\n  NUMBER: 1.0\n")

    def test_pinned_ingress_and_wildcard_certificates(self):
        spec = dict(name="api", image="app:1", region="cn", exposure="cn-edge", port=3000, domain="api.acme.test", node="edge", labels=["custom=value"])
        for node in ({"lumaLocalIngress": True}, {"tailscaleIP": "100.64.0.2"},
                     {"publicIp": "https://[fd00::1]:443"}, {}):
            with self.subTest(node=node):
                self.compare(spec, dict(nodes={"edge": node}, providers={"dns": {"type": "cloudflare", "zone": "acme.test"}},
                                        defaults={"entrypoint": "secure", "certResolver": "cf"}))

    def test_relay_overrides_build_and_local_volumes(self):
        self.compare(dict(name="relay", image="app:1", region="home", exposure="tailscale-relay", domain="relay.test", port=8080,
                          relay={"urls": ["http://a:8080", "http://b:8080"], "priority": 2000}))
        self.compare(dict(name="db", image="postgres:16", region="cn", exposure="tcp-relay", domain="db.test", port=5432,
                          tcp={"addresses": ["a:5432", "b:5432"]}))
        self.compare(dict(name="builder", region="cn", build={"context": "."}, volumes=["data:/app/data"],
                          requirements={"capabilities": ["registry"], "secrets": ["TOKEN"]}))

    def test_healthcheck_keeps_encoded_path(self):
        self.compare(dict(name="api", image="nginx", region="cn", exposure="cn-edge", domain="api.test", port=80,
                          healthcheck={"test": ["CMD", "curl", "http://localhost/a%2Fb/ready"]}))

    def test_rejections_and_stricter_migration_validation(self):
        invalid = [
            "name: worker\nname: other\nimage: app\nregion: cn\n",
            "name: worker\nimage: app\nregion: cn\n---\nname: ignored\n",
            "name: worker\nimage: app\nregion: none\n",
            "name: worker\nimage: app\nregion: cn\nproxy: 'false'\n",
            "name: worker\nimage: app\nregion: cn\nport: 70000\n",
            "name: worker\nimage: app\nregion: cn\nenv: {NESTED: [one, two]}\n",
            "name: worker\nimage: app\nregion: cn\nreplicas: 1.5\n",
            "name: worker\nimage: app\nregion: cn\nresources: {limits: {memory: 0}}\n",
            "name: worker\nimage: app\nregion: cn\nresources: {limits: {memory: 128M}, reservations: {memory: 256M}}\n",
            "name: worker\nimage: app\nregion: cn\nrequirements: {capabilities: [typo]}\n",
            "name: worker\nimage: app\nregion: cn\nstorage: {'': {storageClass: shared}}\n",
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                source = self.directory / "invalid.yml"
                source.write_text(raw)
                result = subprocess.run([str(self.binary), "validate", str(source), "--format", "json"], cwd=self.directory, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse(json.loads(result.stdout)["ok"])

    def test_go_recipe_is_accepted_by_existing_control(self):
        # The portable recipe format emitted by makeWorkflow remains Python-parseable.
        recipe = {"schemaVersion": 1, "method": "image-deploy",
                  "argv": ["--config", "luma.yaml", "--no-env", "deploy", "templates/hello-world.yml", "--timeout", "1800"]}
        self.assertEqual(validate_recipe(recipe), recipe)

    @unittest.skipUnless(shutil.which("openssl"), "openssl required for loopback TLS fixture")
    def test_go_cli_against_python_asgi_workflow_and_stream(self):
        self.check_asgi_deployment(compose=False)

    @unittest.skipUnless(shutil.which("openssl"), "openssl required for loopback TLS fixture")
    def test_go_compose_cli_against_python_asgi_and_workflow_replay(self):
        self.check_asgi_deployment(compose=True)

    def check_asgi_deployment(self, *, compose):
        """Real TLS + production ASGI routes; only the Nomad side effect is fake."""
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Route
        from luma.control import server as control
        from luma.control.state import init_state, load_state, require_token

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            certificate, key = root / "cert.pem", root / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(certificate)],
                           check=True, capture_output=True)
            source, config = root / "app.yml", root / "config.yml"
            source.write_text("name: contract-app\nimage: busybox:1\nregion: cn\n")
            if compose:
                source.write_text("name: contract-app\ncompose: docker-compose.yml\nregion: cn\n")
                (root / "docker-compose.yml").write_text("services:\n  web:\n    image: busybox:1\n    environment:\n      TOKEN: '${TOKEN}'\n")
            (root / "prod.env").write_text("TOKEN=scoped-secret\nUNUSED=never-send\n")
            config.write_text("{}\n")
            with patch.dict(os.environ, LUMA_CONTROL_STATE_DIR=str(root / "state"), LUMA_CONTROL_CONFIG=str(config)):
                state = init_state(domain="fixture.test", cluster_id="fixture", overwrite=True)
                submitted = []

                def deploy(token, body, *, progress=None):
                    require_token(load_state(), token, token_type="deploy")
                    submitted.append(body)
                    progress({"status": "ok", "message": "fixture rollout ready"})
                    return {"deployment" if compose else "service": "contract-app", "revision": "3", "image": "busybox:1"}

                app = Starlette(routes=[Route("/v1/{path:path}", control._asgi_authenticated_post, methods=["POST"]),
                                        Route("/v1/{path:path}", control._asgi_authenticated_get, methods=["GET"])])
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                endpoint = f"https://127.0.0.1:{sock.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off",
                                                      ssl_keyfile=str(key), ssl_certfile=str(certificate)))
                thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
                with patch.object(control, "handle_compose_deployment" if compose else "handle_deployment", side_effect=deploy), \
                     patch.object(control, "handle_control_status", return_value={"nodes": {"items": []}}):
                    thread.start()
                    try:
                        deadline = time.monotonic() + 5
                        while not server.started and thread.is_alive() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(server.started, "ASGI fixture did not start")
                        completed = subprocess.run(
                            [str(self.binary), *(["compose"] if compose else []), "deploy", str(source), "--config", str(config), "--no-env", "--env", str(root / "prod.env"),
                             "--control-url", endpoint, "--insecure", "--format", "json"],
                            env={**os.environ, "LUMA_CONFIG_HOME": str(root / "contexts"), "LUMA_DEPLOY_TOKEN": state["deployToken"]},
                            cwd=root, capture_output=True, text=True, timeout=15,
                        )
                        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                        self.assertNotIn("Warning:", completed.stderr)
                        self.assertEqual(json.loads(completed.stdout)["result"]["deployment" if compose else "service"], "contract-app")
                        self.assertEqual(len(submitted), 1)
                        self.assertEqual(submitted[0]["manifest"], source.read_text())
                        workflow = load_state()["deploymentWorkflows"]["contract-app"]
                        self.assertEqual(workflow["revision"], 1)
                        self.assertEqual(workflow["recipe"]["method"], "compose-deploy" if compose else "image-deploy")
                        self.assertEqual(validate_recipe(workflow["recipe"]), workflow["recipe"])
                        self.assertEqual(workflow["lastSuccess"]["revision"], "3")
                        if compose:
                            self.assertEqual(submitted[0]["composeContent"], (root / "docker-compose.yml").read_text())
                            self.assertEqual(submitted[0]["envSecrets"], {"TOKEN": "scoped-secret"})
                            replay = subprocess.run(
                                [str(self.binary), "workflow", "run", "contract-app", "--path", str(root),
                                 "--no-env", "--control-url", endpoint, "--insecure", "--format", "json"],
                                env={**os.environ, "LUMA_CONFIG_HOME": str(root / "contexts"), "LUMA_DEPLOY_TOKEN": state["deployToken"]},
                                cwd=ROOT, capture_output=True, text=True, timeout=15,
                            )
                            self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
                            self.assertNotIn("Warning:", replay.stderr)
                            self.assertEqual(len(submitted), 2)
                            self.assertEqual(submitted[1]["composeContent"], submitted[0]["composeContent"])
                            self.assertEqual(load_state()["deploymentWorkflows"]["contract-app"]["revision"], 2)
                    finally:
                        server.should_exit = True
                        thread.join(timeout=5)
                        sock.close()
                        self.assertFalse(thread.is_alive(), "ASGI fixture did not stop")


if __name__ == "__main__":
    unittest.main()
