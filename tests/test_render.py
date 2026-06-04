import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from luma.config import LumaConfig
from luma.compose import load_compose_deployment, render_compose_stack
from luma.errors import LumaError
from luma.render import render_stack, render_tailscale_route, route_path, stack_path
from luma.service import load_service
from luma.storage import storage_check_plan


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

    def test_compose_storage_class_renders_nfs_volume_and_traefik_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "services": {
                            "app": {
                                "image": "nginx:alpine",
                                "volumes": ["pg-data:/data"],
                            }
                        },
                        "volumes": {"pg-data": {}},
                    }
                ),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "storageClasses": {
                            "home-nfs": {
                                "provider": "nfs",
                                "mode": "external",
                                "endpoint": "home-nas:/srv/luma",
                                "regions": ["cn"],
                            }
                        },
                        "volumes": {
                            "pg-data": {
                                "storageClass": "home-nfs",
                                "path": "postgres/pg-data",
                            }
                        },
                        "services": {
                            "app": {
                                "exposure": "cn-edge",
                                "domain": "app.example.com",
                                "port": 80,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
            rendered = yaml.safe_load(render_compose_stack(self.config(), deployment))
        app = rendered["services"]["app"]
        self.assertIn("node.labels.region == cn", app["deploy"]["placement"]["constraints"])
        self.assertIn("traefik.http.routers.app-stack-app.rule=Host(`app.example.com`)", app["deploy"]["labels"])
        self.assertIn("luma.storage.pg-data=storageClass", app["deploy"]["labels"])
        self.assertEqual(rendered["volumes"]["pg-data"]["driver_opts"]["type"], "nfs")
        self.assertEqual(rendered["volumes"]["pg-data"]["driver_opts"]["device"], ":/srv/luma/postgres/pg-data")

    def test_managed_compose_storage_uses_swarm_hostname_for_same_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}}),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "storageClasses": {
                            "home-nfs": {"provider": "nfs", "mode": "managed", "node": "home-nas", "path": "/srv/luma"}
                        },
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "postgres/pg-data"}},
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
            rendered = yaml.safe_load(
                render_compose_stack(
                    self.config(),
                    deployment,
                    node_records={"home-nas": {"region": "cn", "swarmHostname": "storage-host.internal"}},
                )
            )
        opts = rendered["volumes"]["pg-data"]["driver_opts"]
        self.assertIn("addr=storage-host.internal", opts["o"])
        self.assertEqual(opts["device"], ":/srv/luma/postgres/pg-data")

    def test_compose_storage_class_rejects_invalid_new_shape(self):
        cases = (
            (
                {"provider": "nfs", "mode": "managed", "node": "home-nas", "path": "/srv/luma", "endpoint": "home-nas:/srv/luma"},
                "endpoint is resolved automatically",
            ),
            (
                {"provider": "nfs", "mode": "external", "endpoint": "home-nas:/srv/luma", "path": "/srv/luma", "regions": ["cn"]},
                "external nfs cannot set node or path",
            ),
        )
        for storage_class, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "docker-compose.yml").write_text(
                    yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}}),
                    encoding="utf-8",
                )
                (root / "luma.compose.yml").write_text(
                    yaml.safe_dump(
                        {
                            "name": "app-stack",
                            "compose": "docker-compose.yml",
                            "region": "cn",
                            "storageClasses": {"home-nfs": storage_class},
                            "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "postgres/pg-data"}},
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(LumaError, message):
                    load_compose_deployment(root / "luma.compose.yml")

    def test_storage_check_enforces_storage_class_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}}),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "storageClasses": {
                            "company-nfs": {"provider": "nfs", "mode": "external", "endpoint": "nfs.example.com:/srv/luma", "regions": ["cn"]}
                        },
                        "volumes": {"pg-data": {"storageClass": "company-nfs", "path": "postgres/pg-data"}},
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
            with self.assertRaisesRegex(LumaError, "region home is not allowed"):
                storage_check_plan(deployment, node_records={})

    def test_managed_compose_storage_uses_tailscale_for_cross_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}}),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "storageClasses": {
                            "home-nfs": {"provider": "nfs", "mode": "managed", "node": "home-nas", "path": "/srv/luma"}
                        },
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "postgres/pg-data"}},
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
            rendered = yaml.safe_load(
                render_compose_stack(self.config(), deployment, node_records={"home-nas": {"region": "home", "tailscaleIP": "100.64.0.50"}})
            )
        self.assertIn("addr=100.64.0.50", rendered["volumes"]["pg-data"]["driver_opts"]["o"])

    def test_managed_compose_storage_requires_tailscale_for_cross_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}}),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "storageClasses": {
                            "home-nfs": {"provider": "nfs", "mode": "managed", "node": "home-nas", "path": "/srv/luma"}
                        },
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "postgres/pg-data"}},
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
            with self.assertRaisesRegex(LumaError, "has no tailscaleIP"):
                render_compose_stack(self.config(), deployment, node_records={"home-nas": {"region": "home"}})

    def test_shared_compose_volume_cannot_need_different_storage_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "services": {
                            "app-cn": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]},
                            "app-home": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]},
                        },
                        "volumes": {"pg-data": {}},
                    }
                ),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "storageClasses": {
                            "home-nfs": {"provider": "nfs", "mode": "managed", "node": "home-nas", "path": "/srv/luma"}
                        },
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "postgres/pg-data"}},
                        "services": {"app-home": {"region": "home"}},
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
            with self.assertRaisesRegex(LumaError, "split it into region-specific volumes"):
                render_compose_stack(self.config(), deployment, node_records={"home-nas": {"region": "home", "tailscaleIP": "100.64.0.50"}})

    def test_compose_local_volume_pins_service_to_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump({"services": {"cache": {"image": "redis:7", "volumes": ["cache-data:/data"]}}, "volumes": {"cache-data": {}}}),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "cache-stack",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "volumes": {
                            "cache-data": {
                                "local": {
                                    "node": "home-mac-mini",
                                    "path": "/opt/luma/state/cache-data",
                                }
                            }
                        },
                        "services": {"cache": {"region": "home"}},
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
            rendered = yaml.safe_load(render_compose_stack(self.config(), deployment))
        cache = rendered["services"]["cache"]
        self.assertIn("node.labels.luma.node.name == home-mac-mini", cache["deploy"]["placement"]["constraints"])
        self.assertEqual(cache["volumes"], ["/opt/luma/state/cache-data:/data"])

    def test_compose_unmanaged_volume_warns_but_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump({"services": {"db": {"image": "postgres:16", "volumes": ["pg-data:/var/lib/postgresql/data"]}}, "volumes": {"pg-data": {}}}),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump({"name": "db-stack", "compose": "docker-compose.yml", "region": "cn"}),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
            rendered = yaml.safe_load(render_compose_stack(self.config(), deployment))
        self.assertTrue(any("pg-data is unmanaged" in warning for warning in deployment.warnings))
        self.assertIn("luma.storage.pg-data=unmanaged", rendered["services"]["db"]["deploy"]["labels"])

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

    def test_service_can_pin_to_luma_node_name_for_preview(self):
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
                "node.labels.luma.node.name == orbstack",
            ],
        )

    def test_service_can_pin_to_resolved_swarm_node_id(self):
        service = self.load(
            """
name: pinned worker
image: ghcr.io/acme/worker:latest
region: home
node: mac-mini-gaojiu
exposure: none
"""
        )
        service = replace(service, node_id="3ve5sy2mn3n16a7yhu9tavhrm")
        rendered = yaml.safe_load(render_stack(self.config(), service))
        constraints = rendered["services"]["pinned-worker"]["deploy"]["placement"]["constraints"]
        self.assertEqual(
            constraints,
            [
                "node.labels.region == home",
                "node.labels.luma.node.id == 3ve5sy2mn3n16a7yhu9tavhrm",
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

    def test_home_tailscale_relay_service_can_use_runtime_proxy(self):
        service = self.load(
            """
name: home ai panel
image: ghcr.io/acme/home-ai-panel:latest
region: home
exposure: tailscale-relay
domain: ai-home.example.com
port: 8080
proxy: true
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        app = rendered["services"]["home-ai-panel"]
        self.assertEqual(app["networks"], ["egress"])
        self.assertEqual(rendered["networks"]["egress"]["external"], True)
        self.assertEqual(app["environment"]["HTTP_PROXY"], "http://egress_mihomo:7890")
        self.assertEqual(app["environment"]["HTTPS_PROXY"], "http://egress_mihomo:7890")
        self.assertEqual(app["ports"][0]["mode"], "host")
        self.assertIn("node.labels.region == home", app["deploy"]["placement"]["constraints"])
        self.assertNotIn("labels", app["deploy"])

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

    def test_service_healthcheck_renders_to_stack(self):
        service = self.load(
            """
name: app
image: ghcr.io/acme/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
healthcheck:
  test:
    - CMD
    - wget
    - -qO-
    - http://127.0.0.1:3000/healthz
  interval: 10s
  timeout: 3s
  retries: 3
"""
        )
        rendered = yaml.safe_load(render_stack(self.config(), service))
        healthcheck = rendered["services"]["app"]["healthcheck"]
        self.assertEqual(healthcheck["test"], ["CMD", "wget", "-qO-", "http://127.0.0.1:3000/healthz"])
        self.assertEqual(healthcheck["interval"], "10s")
        self.assertEqual(healthcheck["timeout"], "3s")
        self.assertEqual(healthcheck["retries"], 3)

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
