import json
import tempfile
import unittest
from pathlib import Path

from luma.config import LumaConfig
from luma.errors import LumaError
from luma.compose import load_compose_deployment
from luma.nomad_render import render_compose_job, _resolve_env_value


def cfg():
    return LumaConfig(
        {"defaults": {"engine": "nomad", "entrypoint": "websecure", "certResolver": "letsencrypt"}},
        None,
    )


def write_deployment(sidecar: str, compose: str):
    d = tempfile.mkdtemp()
    (Path(d) / "docker-compose.yml").write_text(compose)
    sc = Path(d) / "luma.compose.yml"
    sc.write_text(sidecar)
    return load_compose_deployment(sc)


GRANARY_COMPOSE = """
services:
  mysql:
    image: mysql:8.4.9
    environment:
      MYSQL_ROOT_PASSWORD: ${DB_PW}
      MYSQL_DATABASE: granary
    volumes:
      - mysql_data:/var/lib/mysql
    deploy:
      resources:
        limits: { cpus: "0.50", memory: 512M }
        reservations: { cpus: "0.10", memory: 256M }
  app:
    image: registry.example.com/app:latest
    environment:
      DSN: root:${DB_PW}@tcp(mysql:3306)/granary
volumes:
  mysql_data:
"""

GRANARY_SIDECAR = """
name: granary
compose: docker-compose.yml
region: home
services:
  mysql:
    node: lab
    exposure: tcp-relay
    domain: granary-db.example.com
    port: 3306
    publishPort: 3306
  app:
    node: lab
    exposure: tailscale-relay
    domain: api.example.com
    port: 8888
    publishPort: 8888
"""

REAL_GRANARY_COMPOSE = """
services:
  mysql:
    image: mysql:8.4.9@sha256:c36050afdca850f23cef85703f84c7531a5ae155a11b5ee1c60acb09937c4084
    environment:
      MYSQL_DATABASE: granary
      MYSQL_ROOT_PASSWORD: ${GRANARY_MYSQL_ROOT_PASSWORD}
      TZ: Asia/Shanghai
    volumes:
      - granary_mysql_data:/var/lib/mysql
    deploy:
      resources:
        limits: { cpus: "0.50", memory: 512M }
        reservations: { cpus: "0.10", memory: 256M }
  granary:
    image: gcode.gaojiua.com:3000/gaojiuatech/granary:latest
    environment:
      GRANARY_ADMIN_EMAIL: admin@gaojiua.com
      GRANARY_ADMIN_PASSWORD: ${GRANARY_ADMIN_PASSWORD}
      GRANARY_JWT_SECRET: ${GRANARY_JWT_SECRET}
      GRANARY_MYSQL_DSN: root:${GRANARY_MYSQL_ROOT_PASSWORD}@tcp(mysql:3306)/granary?charset=utf8mb4&parseTime=true&loc=Local
      TZ: Asia/Shanghai
  granary-frontend:
    image: gcode.gaojiua.com:3000/gaojiuatech/granary-frontend:latest
  adminer:
    image: adminer:4.8.1
    environment:
      ADMINER_DEFAULT_SERVER: mysql
volumes:
  granary_mysql_data:
"""

REAL_GRANARY_SIDECAR = """
name: granary
compose: docker-compose.yml
region: home
services:
  mysql:
    node: lab
    exposure: tcp-relay
    domain: granary-db.itool.tech
    port: 3306
    publishPort: 3306
  granary:
    node: lab
    exposure: tailscale-relay
    domain: api-granary.itool.tech
    port: 8888
    publishPort: 8888
  granary-frontend:
    node: lab
    exposure: tailscale-relay
    domain: granary.itool.tech
    port: 80
    publishPort: 8081
  adminer:
    node: lab
    exposure: tailscale-relay
    domain: granary-db.itool.tech
    port: 8080
    publishPort: 8080
"""


class ComposeRenderTests(unittest.TestCase):
    def render(self, secrets=None):
        dep = write_deployment(GRANARY_SIDECAR, GRANARY_COMPOSE)
        if secrets is None:
            secrets = {"DB_PW": "s3cr3t"}
        return render_compose_job(cfg(), dep, as_json=False, secrets=secrets)["Job"]

    def test_multi_service_single_group(self):
        job = self.render()
        self.assertEqual(job["ID"], "granary")
        groups = job["TaskGroups"]
        self.assertEqual(len(groups), 1)  # one group so tasks share netns
        names = {t["Name"] for t in groups[0]["Tasks"]}
        self.assertEqual(names, {"mysql", "app"})

    def test_secret_resolved_from_env(self):
        job = self.render()
        mysql = next(t for t in job["TaskGroups"][0]["Tasks"] if t["Name"] == "mysql")
        self.assertEqual(mysql["Env"]["MYSQL_ROOT_PASSWORD"], "s3cr3t")
        app = next(t for t in job["TaskGroups"][0]["Tasks"] if t["Name"] == "app")
        # ${DB_PW} substituted, no leftover placeholder
        self.assertIn("s3cr3t", app["Env"]["DSN"])
        self.assertNotIn("${", app["Env"]["DSN"])

    def test_extra_hosts_preserve_service_name_dsn(self):
        job = self.render()
        app = next(t for t in job["TaskGroups"][0]["Tasks"] if t["Name"] == "app")
        # the compose DSN references `mysql:3306` by service name — must survive
        self.assertIn("tcp(mysql:3306)", app["Env"]["DSN"])
        # and extra_hosts maps that name to loopback in the shared netns
        self.assertIn("mysql:127.0.0.1", app["Config"]["extra_hosts"])

    def test_named_volume_uses_mount_block(self):
        job = self.render()
        mysql = next(t for t in job["TaskGroups"][0]["Tasks"] if t["Name"] == "mysql")
        self.assertNotIn("volumes", mysql["Config"])
        mnt = mysql["Config"]["mount"][0]
        self.assertEqual(mnt["type"], "volume")
        self.assertEqual(mnt["source"], "mysql_data")
        self.assertEqual(mnt["target"], "/var/lib/mysql")

    def test_reserved_ports_and_resources(self):
        job = self.render()
        ports = {p["Label"]: (p["Value"], p["To"]) for p in job["TaskGroups"][0]["Networks"][0]["ReservedPorts"]}
        self.assertEqual(ports["mysql"], (3306, 3306))
        self.assertEqual(ports["app"], (8888, 8888))
        mysql = next(t for t in job["TaskGroups"][0]["Tasks"] if t["Name"] == "mysql")
        self.assertEqual(mysql["Resources"]["CPU"], 500)            # 0.50 cores -> 500 MHz
        self.assertEqual(mysql["Resources"]["MemoryMB"], 256)        # reservation
        self.assertEqual(mysql["Resources"]["MemoryMaxMB"], 512)     # limit

    def test_node_pin_and_region_constraints(self):
        job = self.render()
        cons = {(c["LTarget"], c["RTarget"]) for c in job["Constraints"]}
        self.assertIn(("${meta.region}", "home"), cons)
        self.assertIn(("${meta.luma_node_name}", "lab"), cons)

    def test_missing_secret_raises(self):
        with self.assertRaises(LumaError):
            self.render(secrets={})

    def test_cloudflare_tunnel_exposure_rejected_at_load(self):
        # compose has no cloudflared sidecar path; a tunnel service would deploy
        # "successfully" yet render unreachable. Must fail fast at load time.
        compose = """
services:
  app:
    image: registry.example.com/app:latest
"""
        sidecar = """
name: tool
compose: docker-compose.yml
region: home
services:
  app:
    exposure: cloudflare-tunnel
    domain: tool.example.com
    port: 8080
"""
        with self.assertRaises(LumaError) as ctx:
            write_deployment(sidecar, compose)
        self.assertIn("cloudflare-tunnel", str(ctx.exception))

    def test_conflicting_service_regions_raise(self):
        compose = """
services:
  mysql:
    image: mysql:8.4.9
    environment:
      MYSQL_ROOT_PASSWORD: ${DB_PW}
  app:
    image: registry.example.com/app:latest
"""
        sidecar = """
name: granary
compose: docker-compose.yml
region: home
services:
  mysql:
    region: home
    exposure: none
  app:
    region: cn
    exposure: none
"""
        dep = write_deployment(sidecar, compose)
        with self.assertRaises(LumaError):
            render_compose_job(cfg(), dep, as_json=False)

    def test_invalid_volume_spec_raises(self):
        compose = """
services:
  mysql:
    image: mysql:8.4.9
    environment:
      MYSQL_ROOT_PASSWORD: ${DB_PW}
    volumes:
      - "/var/lib/mysql:"
"""
        sidecar = """
name: granary
compose: docker-compose.yml
region: home
services:
  mysql:
    node: lab
    exposure: none
"""
        dep = write_deployment(sidecar, compose)
        with self.assertRaises(LumaError):
            render_compose_job(cfg(), dep, as_json=False)

    def test_anonymous_volume_is_dropped_not_rejected(self):
        # docker-compose short syntax: a bare container path declares an
        # anonymous volume. It has no host source, so it cannot become a Nomad
        # mount, but it must NOT abort the deploy (regression guard).
        compose = """
services:
  mysql:
    image: mysql:8.4.9
    environment:
      MYSQL_ROOT_PASSWORD: ${DB_PW}
    volumes:
      - /var/lib/mysql
"""
        sidecar = """
name: granary
compose: docker-compose.yml
region: home
services:
  mysql:
    node: lab
    exposure: none
"""
        dep = write_deployment(sidecar, compose)
        job = render_compose_job(cfg(), dep, as_json=False, secrets={"DB_PW": "x"})["Job"]
        mysql = next(t for t in job["TaskGroups"][0]["Tasks"] if t["Name"] == "mysql")
        # anonymous volume dropped: no mount block referencing it
        self.assertNotIn("mount", mysql["Config"])

    def test_long_form_dict_volume_renders_correct_mount(self):
        # docker-compose long/expanded syntax (what `docker compose config`
        # emits). render must honor the dict form, not stringify+split it.
        compose = """
services:
  mysql:
    image: mysql:8.4.9
    environment:
      MYSQL_ROOT_PASSWORD: ${DB_PW}
    volumes:
      - type: volume
        source: mysql_data
        target: /var/lib/mysql
      - type: bind
        source: /srv/conf
        target: /etc/mysql/conf.d
        read_only: true
"""
        sidecar = """
name: granary
compose: docker-compose.yml
region: home
services:
  mysql:
    node: lab
    exposure: none
"""
        dep = write_deployment(sidecar, compose)
        job = render_compose_job(cfg(), dep, as_json=False, secrets={"DB_PW": "x"})["Job"]
        mysql = next(t for t in job["TaskGroups"][0]["Tasks"] if t["Name"] == "mysql")
        mounts = mysql["Config"]["mount"]
        named = next(m for m in mounts if m["type"] == "volume")
        self.assertEqual(named["source"], "mysql_data")
        self.assertEqual(named["target"], "/var/lib/mysql")
        self.assertFalse(named["readonly"])
        bind = next(m for m in mounts if m["type"] == "bind")
        self.assertEqual(bind["source"], "/srv/conf")
        self.assertEqual(bind["target"], "/etc/mysql/conf.d")
        self.assertTrue(bind["readonly"])

    def test_long_form_anonymous_volume_dropped(self):
        # dict form with no source (anonymous/tmpfs) must drop, not crash
        compose = """
services:
  mysql:
    image: mysql:8.4.9
    environment:
      MYSQL_ROOT_PASSWORD: ${DB_PW}
    volumes:
      - type: volume
        target: /var/lib/mysql
"""
        sidecar = """
name: granary
compose: docker-compose.yml
region: home
services:
  mysql:
    node: lab
    exposure: none
"""
        dep = write_deployment(sidecar, compose)
        job = render_compose_job(cfg(), dep, as_json=False, secrets={"DB_PW": "x"})["Job"]
        mysql = next(t for t in job["TaskGroups"][0]["Tasks"] if t["Name"] == "mysql")
        self.assertNotIn("mount", mysql["Config"])


    def test_secret_placeholder_can_be_kept_for_validation(self):
        dep = write_deployment(GRANARY_SIDECAR, GRANARY_COMPOSE)
        job = render_compose_job(cfg(), dep, as_json=False, resolve_secrets=False)["Job"]
        mysql = next(t for t in job["TaskGroups"][0]["Tasks"] if t["Name"] == "mysql")
        app = next(t for t in job["TaskGroups"][0]["Tasks"] if t["Name"] == "app")
        self.assertEqual(mysql["Env"]["MYSQL_ROOT_PASSWORD"], "${DB_PW}")
        self.assertIn("${DB_PW}", app["Env"]["DSN"])

    def test_resolve_env_passthrough_non_secret(self):
        self.assertEqual(_resolve_env_value("plain-value"), "plain-value")

    def test_real_granary_shape_renders_ports_volume_and_private_auth(self):
        dep = write_deployment(REAL_GRANARY_SIDECAR, REAL_GRANARY_COMPOSE)
        auth = {"username": "deploy", "password": "token", "serveraddress": "gcode.gaojiua.com:3000"}
        job = render_compose_job(
            cfg(),
            dep,
            as_json=False,
            registry_auth_resolver=lambda image: auth if image.startswith("gcode.gaojiua.com:3000/") else None,
            secrets={
                "GRANARY_MYSQL_ROOT_PASSWORD": "mysql-secret",
                "GRANARY_ADMIN_PASSWORD": "admin-secret",
                "GRANARY_JWT_SECRET": "jwt-secret",
            },
        )["Job"]

        group = job["TaskGroups"][0]
        self.assertEqual({task["Name"] for task in group["Tasks"]}, {"mysql", "granary", "granary-frontend", "adminer"})
        constraints = {(c["LTarget"], c["RTarget"]) for c in job["Constraints"]}
        self.assertIn(("${meta.region}", "home"), constraints)
        self.assertIn(("${meta.luma_node_name}", "lab"), constraints)
        ports = {p["Label"]: (p["Value"], p["To"]) for p in group["Networks"][0]["ReservedPorts"]}
        self.assertEqual(ports["mysql"], (3306, 3306))
        self.assertEqual(ports["granary"], (8888, 8888))
        self.assertEqual(ports["granary_frontend"], (8081, 80))
        self.assertEqual(ports["adminer"], (8080, 8080))

        mysql = next(t for t in group["Tasks"] if t["Name"] == "mysql")
        self.assertEqual(mysql["Config"]["mount"][0]["source"], "granary_mysql_data")
        self.assertEqual(mysql["Config"]["mount"][0]["type"], "volume")
        self.assertEqual(mysql["Env"]["MYSQL_ROOT_PASSWORD"], "mysql-secret")
        app = next(t for t in group["Tasks"] if t["Name"] == "granary")
        self.assertEqual(app["Config"]["auth"]["server_address"], "gcode.gaojiua.com:3000")
        self.assertIn("root:mysql-secret@tcp(mysql:3306)", app["Env"]["GRANARY_MYSQL_DSN"])
        frontend = next(t for t in group["Tasks"] if t["Name"] == "granary-frontend")
        self.assertEqual(frontend["Config"]["auth"]["username"], "deploy")


if __name__ == "__main__":
    unittest.main()
