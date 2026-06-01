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
        self.assertIn("traefik.swarm.network=public", app["deploy"]["labels"])

    def test_global_worker_uses_region_constraint_only_by_default(self):
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
        self.assertNotIn("node.labels.external_net == true", constraints)

    def test_service_can_pin_to_swarm_node_hostname(self):
        service = self.load(
            """
name: pinned worker
image: ghcr.io/acme/worker:latest
region: home
node: orbstack
exposure: none
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        constraints = rendered["services"]["pinned-worker"]["deploy"]["placement"]["constraints"]
        self.assertEqual(
            constraints,
            [
                "node.labels.region == home",
                "node.hostname == orbstack",
            ],
        )

    def test_node_pin_rejects_blank_value(self):
        with self.assertRaisesRegex(Exception, "node must be a non-empty string"):
            self.load(
                """
name: bad worker
image: ghcr.io/acme/worker:latest
region: home
node: ""
exposure: none
"""
            )

    def test_proxy_service_adds_egress_network_and_env_without_node_constraint(self):
        service = self.load(
            """
name: proxy worker
image: ghcr.io/acme/proxy-worker:latest
region: cn
proxy: true
env:
  HTTP_PROXY: http://custom-proxy:7890
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        worker = rendered["services"]["proxy-worker"]
        constraints = worker["deploy"]["placement"]["constraints"]
        self.assertIn("node.labels.region == cn", constraints)
        self.assertNotIn("node.labels.egress == true", constraints)
        self.assertIn("egress", worker["networks"])
        self.assertEqual(worker["environment"]["HTTP_PROXY"], "http://custom-proxy:7890")
        self.assertEqual(worker["environment"]["HTTPS_PROXY"], "http://egress_mihomo:7890")

    def test_public_proxy_service_pins_traefik_to_public_network(self):
        service = self.load(
            """
name: proxied api
image: ghcr.io/acme/proxied-api:latest
region: cn
exposure: cn-edge
domain: proxied.example.com
port: 8080
proxy: true
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        app = rendered["services"]["proxied-api"]
        self.assertEqual(app["networks"], ["public", "egress"])
        self.assertIn("traefik.swarm.network=public", app["deploy"]["labels"])

    def test_service_resources_render_to_swarm_deploy_resources(self):
        service = self.load(
            """
name: bounded worker
image: ghcr.io/acme/bounded-worker:latest
region: cn
resources:
  limits:
    cpus: "0.50"
    memory: 256M
  reservations:
    cpus: "0.10"
    memory: 64M
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        resources = rendered["services"]["bounded-worker"]["deploy"]["resources"]
        self.assertEqual(resources["limits"]["cpus"], "0.50")
        self.assertEqual(resources["limits"]["memory"], "256M")
        self.assertEqual(resources["reservations"]["memory"], "64M")

    def test_named_volumes_are_rendered_on_service_and_stack(self):
        service = self.load(
            """
name: stateful app
image: ghcr.io/acme/stateful-app:latest
region: cn
exposure: none
volumes:
  - stateful_data:/data
  - stateful_home:/codex-home
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        app = rendered["services"]["stateful-app"]
        self.assertEqual(app["volumes"], ["stateful_data:/data", "stateful_home:/codex-home"])
        self.assertIn("stateful_data", rendered["volumes"])
        self.assertIn("stateful_home", rendered["volumes"])

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

    def test_tailscale_relay_can_preview_route_from_node_pin(self):
        service = self.load(
            """
name: home panel
image: ghcr.io/acme/panel:latest
region: home
node: orbstack
exposure: tailscale-relay
domain: panel.example.com
port: 8080
"""
        )
        route = yaml.safe_load(render_tailscale_route(self.config(), service))
        self.assertEqual(
            route["http"]["services"]["home-panel"]["loadBalancer"]["servers"][0]["url"],
            "http://orbstack:8080",
        )

    def test_tailscale_relay_can_preview_auto_home_node_route(self):
        service = self.load(
            """
name: home panel
image: ghcr.io/acme/panel:latest
region: home
exposure: tailscale-relay
domain: panel.example.com
port: 8080
"""
        )
        route = yaml.safe_load(render_tailscale_route(self.config(), service))
        self.assertEqual(
            route["http"]["services"]["home-panel"]["loadBalancer"]["servers"][0]["url"],
            "http://auto-home-node:8080",
        )

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
