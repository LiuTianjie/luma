import tempfile
import unittest
from pathlib import Path

import yaml

from luma.config import LumaConfig
from luma.compose import load_compose_deployment, render_compose_routes
from luma.errors import LumaError
from luma.render import render_tailscale_route, render_tcp_route, route_path, stack_path
from luma.service import load_service
from luma.storage import storage_check_plan


class RenderRouteTests(unittest.TestCase):
    def config(self):
        return LumaConfig(
            {
                "defaults": {
                    "stackRoot": "stacks",
                    "routesRoot": "routes",
                    "entrypoint": "websecure",
                    "certResolver": "letsencrypt",
                }
            },
            None,
        )

    def load_service(self, content: str):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            tmp.write(content)
            tmp.close()
            return load_service(Path(tmp.name))
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_service_manifest_rejects_swarm_engine(self):
        with self.assertRaisesRegex(LumaError, "engine must be one of"):
            self.load_service(
                """
name: app
image: ghcr.io/acme/app:latest
region: cn
engine: swarm
"""
            )

    def test_service_rejects_public_field(self):
        with self.assertRaisesRegex(LumaError, "public is no longer supported"):
            self.load_service(
                """
name: app
image: ghcr.io/acme/app:latest
region: cn
public: true
domain: app.example.com
port: 3000
"""
            )

    def test_tailscale_route_renders_static_upstream(self):
        service = self.load_service(
            """
name: home-api
image: ghcr.io/acme/api:latest
region: home
exposure: tailscale-relay
domain: api.example.com
port: 8080
relay:
  url: http://100.64.0.10:8080
"""
        )
        rendered = yaml.safe_load(render_tailscale_route(self.config(), service))
        router = rendered["http"]["routers"]["home-api"]
        servers = rendered["http"]["services"]["home-api"]["loadBalancer"]["servers"]
        self.assertEqual(router["rule"], "Host(`api.example.com`)")
        self.assertEqual(servers, [{"url": "http://100.64.0.10:8080"}])

    def test_tcp_route_renders_publish_port_entrypoint(self):
        service = self.load_service(
            """
name: granary-mysql
image: mysql:8
region: home
exposure: tcp-relay
domain: granary-db.example.com
port: 3306
publishPort: 13306
tcp:
  address: 100.64.0.20:3306
"""
        )
        rendered = yaml.safe_load(render_tcp_route(self.config(), service))
        router = rendered["tcp"]["routers"]["granary-mysql"]
        servers = rendered["tcp"]["services"]["granary-mysql"]["loadBalancer"]["servers"]
        self.assertEqual(router["entryPoints"], ["tcp-13306"])
        self.assertEqual(servers, [{"address": "100.64.0.20:3306"}])

    def test_paths_follow_config_roots(self):
        service = self.load_service(
            """
name: api
image: ghcr.io/acme/api:latest
region: cn
"""
        )
        self.assertEqual(stack_path(self.config(), service), Path("stacks/cn/api/stack.yml"))
        self.assertEqual(route_path(self.config(), service), Path("routes/api.yml"))

    def test_compose_routes_still_render_without_stack_renderer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump({"services": {"mysql": {"image": "mysql:8"}}}),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "granary",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "mysql": {
                                "region": "home",
                                "exposure": "tcp-relay",
                                "domain": "granary-db.example.com",
                                "port": 3306,
                                "publishPort": 3306,
                                "tcp": {"address": "100.64.0.20:3306"},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
            routes = render_compose_routes(self.config(), deployment)
        route = yaml.safe_load(routes["mysql"])
        self.assertEqual(
            route["tcp"]["services"]["granary-mysql"]["loadBalancer"]["servers"],
            [{"address": "100.64.0.20:3306"}],
        )

    def test_storage_check_plan_uses_nomad_sidecar_storage_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "services": {"app": {"image": "nginx:alpine", "volumes": ["data:/data"]}},
                        "volumes": {"data": {}},
                    }
                ),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "app",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "storageClasses": {
                            "cn-nfs": {
                                "provider": "nfs",
                                "mode": "managed",
                                "node": "aly",
                                "path": "/srv/luma",
                                "regions": ["cn"],
                            }
                        },
                        "volumes": {"data": {"storageClass": "cn-nfs", "path": "app/data"}},
                        "services": {"app": {"region": "cn", "exposure": "none"}},
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
        plan = storage_check_plan(
            deployment,
            node_records={"aly": {"region": "cn", "hostname": "aly.internal"}},
        )
        self.assertEqual(plan["mounts"][0]["endpoint"], "aly.internal:/srv/luma")


if __name__ == "__main__":
    unittest.main()
