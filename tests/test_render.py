import tempfile
import unittest
from pathlib import Path

import yaml

from luma.config import LumaConfig
from luma.compose import load_compose_deployment, render_compose_routes
from luma.errors import LumaError
from luma.render import render_tailscale_route, render_tcp_route, route_path, stack_path
from luma.service import load_service


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
name: ledger-mysql
image: mysql:8
region: home
exposure: tcp-relay
domain: ledger-db.example.com
port: 3306
publishPort: 13306
tcp:
  address: 100.64.0.20:3306
"""
        )
        rendered = yaml.safe_load(render_tcp_route(self.config(), service))
        router = rendered["tcp"]["routers"]["ledger-mysql"]
        servers = rendered["tcp"]["services"]["ledger-mysql"]["loadBalancer"]["servers"]
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
        self.assertEqual(stack_path(self.config(), service), Path("stacks/cn/api/api.nomad.json"))
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
                        "name": "ledger",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "mysql": {
                                "region": "home",
                                "exposure": "tcp-relay",
                                "domain": "ledger-db.example.com",
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
            route["tcp"]["services"]["ledger-mysql"]["loadBalancer"]["servers"],
            [{"address": "100.64.0.20:3306"}],
        )

    def test_compose_tailscale_relay_route_matches_native_priority_and_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}}),
                encoding="utf-8",
            )
            (root / "luma.compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "site",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "web": {
                                "region": "home",
                                "exposure": "tailscale-relay",
                                "domain": "site.example.com",
                                "port": 8080,
                                "relay": {"urls": ["http://100.64.0.20:8080", "http://100.64.0.21:8080"]},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(root / "luma.compose.yml")
            route = yaml.safe_load(render_compose_routes(self.config(), deployment)["web"])
        router = route["http"]["routers"]["site-web"]
        self.assertEqual(router["priority"], 1000)
        self.assertEqual(
            route["http"]["services"]["site-web"]["loadBalancer"]["servers"],
            [{"url": "http://100.64.0.20:8080"}, {"url": "http://100.64.0.21:8080"}],
        )

    def test_storage_classes_and_service_storage_are_rejected(self):
        from luma.agent import node_agent_capabilities

        for os_name in ("linux", "darwin"):
            caps = node_agent_capabilities(os_name)
            self.assertNotIn("nfs-host", caps)
            self.assertNotIn("nfs-client", caps)
            self.assertIn("managed-volume-path", caps)

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
            (root / "with-classes.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "app",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "storageClasses": {
                            "cn-shared": {"provider": "nfs", "mode": "managed", "node": "aly", "path": "/srv/luma"}
                        },
                        "services": {"app": {"region": "cn", "exposure": "none"}},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(LumaError, "storageClasses are not supported"):
                load_compose_deployment(root / "with-classes.yml")

            (root / "with-vol-class.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "app",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"data": {"storageClass": "cn-shared", "path": "app/data"}},
                        "services": {"app": {"region": "cn", "exposure": "none"}},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(LumaError, "storageClasses are not supported"):
                load_compose_deployment(root / "with-vol-class.yml")

            (root / "service.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": "db",
                        "image": "postgres:17",
                        "region": "cn",
                        "exposure": "none",
                        "volumes": ["pg-data:/var/lib/postgresql/data"],
                        "storage": {"pg-data": {"storageClass": "cn-shared"}},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(LumaError, "storage classes are not supported"):
                load_service(root / "service.yml")


class BuildBlockTests(unittest.TestCase):
    def load_service(self, content: str):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            tmp.write(content)
            tmp.close()
            return load_service(Path(tmp.name))
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_build_block_allows_missing_image(self):
        service = self.load_service(
            """
name: app
region: cn
build:
  context: .
  dockerfile: Dockerfile
  platform: linux/arm64
"""
        )
        self.assertEqual(service.image, "")
        self.assertIsNotNone(service.build)
        self.assertEqual(service.build.platform, "linux/arm64")
        self.assertEqual(service.build.dockerfile, "Dockerfile")

    def test_build_block_defaults(self):
        service = self.load_service(
            """
name: app
region: cn
build: {}
"""
        )
        self.assertEqual(service.build.context, ".")
        self.assertEqual(service.build.dockerfile, "Dockerfile")
        self.assertEqual(service.build.platform, "linux/amd64")

    def test_missing_image_and_build_rejected(self):
        with self.assertRaisesRegex(LumaError, "requires string field: image"):
            self.load_service(
                """
name: app
region: cn
"""
            )

    def test_build_block_rejects_unknown_field(self):
        with self.assertRaisesRegex(LumaError, "unsupported build field"):
            self.load_service(
                """
name: app
region: cn
build:
  bogus: 1
"""
            )

    def test_image_still_works_without_build(self):
        service = self.load_service(
            """
name: app
image: ghcr.io/acme/app:latest
region: cn
"""
        )
        self.assertEqual(service.image, "ghcr.io/acme/app:latest")
        self.assertIsNone(service.build)


if __name__ == "__main__":
    unittest.main()
