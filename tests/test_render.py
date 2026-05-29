import tempfile
import unittest
from pathlib import Path

import yaml

from luma.config import LumaConfig
from luma.render import render_stack, render_tailscale_route, route_path, stack_path
from luma.service import load_service


class RenderStackTests(unittest.TestCase):
    def config(self):
        return LumaConfig(
            {
                "defaults": {
                    "stackRoot": "stacks",
                    "publicNetwork": "public",
                    "entrypoint": "websecure",
                    "certResolver": "letsencrypt",
                }
            },
            None,
        )

    def load(self, content: str):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            tmp.write(content)
            tmp.close()
            return load_service(Path(tmp.name))
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_public_cn_service_gets_traefik_labels(self):
        service = self.load(
            """
name: app
image: ghcr.io/acme/app:latest
region: cn
public: true
domain: app.example.com
port: 3000
replicas: 2
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        app = rendered["services"]["app"]
        self.assertEqual(app["deploy"]["replicas"], 2)
        self.assertIn("public", app["networks"])
        self.assertIn("node.labels.region == cn", app["deploy"]["placement"]["constraints"])
        self.assertIn(
            "traefik.http.routers.app.rule=Host(`app.example.com`)",
            app["deploy"]["labels"],
        )

    def test_global_worker_gets_external_net_constraint(self):
        service = self.load(
            """
name: worker
image: ghcr.io/acme/worker:latest
region: global
public: false
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        constraints = rendered["services"]["worker"]["deploy"]["placement"]["constraints"]
        self.assertIn("node.labels.region == global", constraints)
        self.assertIn("node.labels.external_net == true", constraints)

    def test_default_stack_path_uses_region_and_slug(self):
        service = self.load(
            """
name: AI Gateway
image: ghcr.io/acme/ai-gateway:latest
region: global
public: false
"""
        )
        self.assertEqual(stack_path(self.config(), service), Path("stacks/global/ai-gateway/stack.yml"))

    def test_tailscale_relay_writes_port_and_route(self):
        service = self.load(
            """
name: home panel
image: ghcr.io/acme/panel:latest
region: home
public: true
exposure: tailscale-relay
domain: panel.example.com
port: 8080
relay:
  host: home-1.tailnet.ts.net
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        app = rendered["services"]["home-panel"]
        self.assertEqual(app["ports"][0]["published"], 8080)
        self.assertNotIn("networks", rendered)
        route = yaml.safe_load(render_tailscale_route(self.config(), service))
        self.assertEqual(
            route["http"]["services"]["home-panel"]["loadBalancer"]["servers"][0]["url"],
            "http://home-1.tailnet.ts.net:8080",
        )
        self.assertEqual(route_path(self.config(), service), Path("routes/home-panel.yml"))

    def test_cloudflare_tunnel_adds_cloudflared_service(self):
        service = self.load(
            """
name: home tool
image: ghcr.io/acme/tool:latest
region: home
public: true
exposure: cloudflare-tunnel
domain: tool.example.com
port: 8080
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        self.assertIn("home-tool", rendered["services"])
        self.assertIn("cloudflared", rendered["services"])
        self.assertNotIn("networks", rendered)


if __name__ == "__main__":
    unittest.main()
