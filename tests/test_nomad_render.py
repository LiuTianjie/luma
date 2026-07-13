import json
import tempfile
import unittest
from pathlib import Path

from luma.config import LumaConfig
from luma.errors import LumaError
from luma.nomad_render import render_nomad_job, render_control_job, render_traefik_job, render_egress_job, _healthcheck_url
from luma.service import load_service


class NomadRenderTests(unittest.TestCase):
    def config(self):
        return LumaConfig(
            {
                "defaults": {
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

    def render(self, content: str, secrets=None):
        service = self.load(content)
        return render_nomad_job(self.config(), service, as_json=False, secrets=secrets)["Job"]

    def test_proxy_injects_real_egress_url_not_consul_name(self):
        # proxy: true must inject the caller-provided real gateway address, and
        # NOT a *.service.consul name (Luma runs no Consul -> never resolves).
        service = self.load(
            """
name: app
image: ghcr.io/acme/app:latest
region: home
exposure: tailscale-relay
domain: app.example.com
port: 8080
proxy: true
"""
        )
        job = render_nomad_job(
            self.config(), service, as_json=False, egress_proxy_url="http://100.64.0.1:7890"
        )["Job"]
        env = job["TaskGroups"][0]["Tasks"][0]["Env"]
        self.assertEqual(env["HTTP_PROXY"], "http://100.64.0.1:7890")
        self.assertEqual(env["HTTPS_PROXY"], "http://100.64.0.1:7890")
        self.assertNotIn("consul", json.dumps(job))

    def test_proxy_without_resolved_url_injects_nothing(self):
        # No resolvable gateway -> inject no proxy env (better than a dead name)
        service = self.load(
            """
name: app
image: ghcr.io/acme/app:latest
region: home
exposure: tailscale-relay
domain: app.example.com
port: 8080
proxy: true
"""
        )
        job = render_nomad_job(self.config(), service, as_json=False)["Job"]
        env = job["TaskGroups"][0]["Tasks"][0].get("Env") or {}
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)

    def test_cn_edge_emits_traefik_nomad_tags_and_dynamic_port(self):
        job = self.render(
            """
name: app
image: ghcr.io/acme/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
"""
        )
        self.assertEqual(job["ID"], "app")
        group = job["TaskGroups"][0]
        self.assertEqual(group["Count"], 2)
        # region constraint
        self.assertIn(
            {"LTarget": "${meta.region}", "RTarget": "cn", "Operand": "="},
            job["Constraints"],
        )
        # dynamic port for edge
        self.assertEqual(group["Networks"][0]["DynamicPorts"][0]["To"], 3000)
        # traefik nomad-provider service
        svc = group["Services"][0]
        self.assertEqual(svc["Provider"], "nomad")
        self.assertEqual(svc["Address"], "${meta.luma_tailscale_ip}")
        self.assertNotIn("AddressMode", svc)
        self.assertIn(
            {"LTarget": "${meta.luma_tailscale_ip}", "Operand": "is_set"},
            job["Constraints"],
        )
        self.assertIn("traefik.enable=true", svc["Tags"])
        self.assertIn(
            "traefik.http.routers.app.rule=Host(`app.example.com`)", svc["Tags"]
        )

    def test_cn_edge_with_pinned_node_advertises_tailscale_address(self):
        service = self.load(
            """
name: app
image: ghcr.io/acme/app:latest
region: cn
node: tecent
exposure: cn-edge
domain: app.example.com
port: 3000
"""
        )
        config = LumaConfig(
            {
                "defaults": {
                    "entrypoint": "websecure",
                    "certResolver": "letsencrypt",
                },
                "nodes": {
                    "tecent": {
                        "host": "tecent",
                        "tailscaleIP": "100.64.29.91",
                    }
                },
            },
            None,
        )
        job = render_nomad_job(config, service, as_json=False)["Job"]
        svc = job["TaskGroups"][0]["Services"][0]
        self.assertEqual(svc["Address"], "100.64.29.91")
        self.assertNotIn("AddressMode", svc)
        # auto_revert is on (the new capability)
        self.assertTrue(job["Update"]["AutoRevert"])
        self.assertEqual(job["Update"]["MaxParallel"], 1)

    def test_cn_edge_pinned_to_local_ingress_manager_advertises_host_address(self):
        service = self.load(
            """
name: registry
image: registry:2
region: cn
node: manager
exposure: cn-edge
domain: registry.example.com
port: 5000
"""
        )
        config = LumaConfig(
            {
                "nodes": {
                    "manager": {
                        "host": "manager",
                        "region": "cn",
                        "tailscaleIP": "100.106.154.3",
                        "lumaLocalIngress": True,
                    }
                }
            },
            None,
        )

        job = render_nomad_job(config, service, as_json=False)["Job"]
        svc = job["TaskGroups"][0]["Services"][0]

        self.assertNotIn("Address", svc)
        self.assertEqual(svc["AddressMode"], "host")
        self.assertNotIn(
            {"LTarget": "${meta.luma_tailscale_ip}", "Operand": "is_set"},
            job["Constraints"],
        )

    def test_single_replica_dynamic_edge_uses_canary_before_promoting(self):
        job = self.render(
            """
name: app
image: ghcr.io/acme/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
healthcheck:
  test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:3000/healthz || exit 1"]
  interval: 10s
  timeout: 2s
"""
        )

        self.assertEqual(job["TaskGroups"][0]["Count"], 1)
        update = job["Update"]
        self.assertEqual(update["Canary"], 1)
        self.assertEqual(update["MaxParallel"], 1)
        self.assertTrue(update["AutoPromote"])
        self.assertTrue(update["AutoRevert"])
        self.assertEqual(update["HealthCheck"], "checks")

    def test_single_replica_fixed_port_skips_canary_to_avoid_port_conflict(self):
        job = self.render(
            """
name: gateway
image: ghcr.io/acme/gateway:latest
region: cn
exposure: cn-edge
domain: gateway.example.com
port: 8787
publishPort: 8787
"""
        )

        update = job["Update"]
        self.assertNotIn("Canary", update)
        self.assertNotIn("AutoPromote", update)
        self.assertEqual(update["MaxParallel"], 1)

    def test_cn_edge_publish_port_uses_static_reserved_port(self):
        job = self.render(
            """
name: gateway
image: ghcr.io/acme/gateway:latest
region: cn
exposure: cn-edge
domain: gateway.example.com
port: 8787
publishPort: 8787
"""
        )
        group = job["TaskGroups"][0]
        net = group["Networks"][0]
        self.assertEqual(net["Mode"], "bridge")
        self.assertNotIn("DynamicPorts", net)
        self.assertEqual(net["ReservedPorts"][0]["Value"], 8787)
        self.assertEqual(net["ReservedPorts"][0]["To"], 8787)
        self.assertEqual(group["Tasks"][0]["Config"]["ports"], ["http"])

    def test_secret_placeholder_can_be_kept_for_validation(self):
        service = self.load(
            """
name: app
image: ghcr.io/acme/app:latest
region: cn
exposure: none
env:
  API_TOKEN: ${API_TOKEN}
"""
        )
        job = render_nomad_job(self.config(), service, as_json=False, resolve_secrets=False)["Job"]
        task = job["TaskGroups"][0]["Tasks"][0]
        self.assertEqual(task["Env"]["API_TOKEN"], "${API_TOKEN}")

    def test_tailscale_relay_uses_docker_host_mode(self):
        job = self.render(
            """
name: home-panel
image: ghcr.io/acme/panel:latest
region: home
exposure: tailscale-relay
domain: panel.example.com
port: 8080
replicas: 1
relay:
  host: home-1.ts.net
"""
        )
        group = job["TaskGroups"][0]
        task = group["Tasks"][0]
        # docker host networking: required on macOS/OrbStack, exposes the
        # container's real port. No Nomad port block, no Services.
        self.assertEqual(task["Config"]["network_mode"], "host")
        self.assertNotIn("Networks", group)
        self.assertNotIn("Services", group)
        self.assertNotIn("ports", task["Config"])

    def test_tailscale_relay_publish_port_uses_bridge_mapping(self):
        job = self.render(
            """
name: lab-panel
image: ghcr.io/acme/panel:latest
region: home
node: lab
exposure: tailscale-relay
domain: panel.example.com
port: 8080
publishPort: 18080
replicas: 1
relay:
  host: 100.64.0.10
"""
        )
        group = job["TaskGroups"][0]
        task = group["Tasks"][0]
        port = group["Networks"][0]["ReservedPorts"][0]
        self.assertEqual(group["Networks"][0]["Mode"], "bridge")
        self.assertEqual(port["Value"], 18080)
        self.assertEqual(port["To"], 8080)
        self.assertEqual(task["Config"]["ports"], ["http"])
        self.assertNotIn("network_mode", task["Config"])

    def test_node_pin_uses_stable_meta_not_swarm_id(self):
        job = self.render(
            """
name: kato
image: ghcr.io/acme/kato:latest
region: home
node: lab
exposure: none
"""
        )
        self.assertIn(
            {"LTarget": "${meta.luma_node_name}", "RTarget": "lab", "Operand": "="},
            job["Constraints"],
        )

    def test_volumes_use_mount_blocks_not_relative_path(self):
        # CRITICAL regression guard: named volumes MUST render as mount blocks
        # (type=volume), never the docker `volumes` shorthand — that shorthand
        # makes Nomad bind an empty alloc dir, silently running on empty data.
        job = self.render(
            """
name: gitea
image: gitea/gitea:latest
region: home
node: home-mac-mini
exposure: none
volumes:
  - gitea_data:/data
  - gitea_cache:/cache
  - /opt/host/cfg:/etc/cfg:ro
"""
        )
        cfg = job["TaskGroups"][0]["Tasks"][0]["Config"]
        self.assertNotIn("volumes", cfg)  # never the dangerous shorthand
        mounts = cfg["mount"]
        named = [m for m in mounts if m["type"] == "volume"]
        binds = [m for m in mounts if m["type"] == "bind"]
        self.assertEqual({m["source"] for m in named}, {"gitea_data", "gitea_cache"})
        self.assertEqual(named[0]["target"], "/data")
        self.assertEqual(binds[0]["source"], "/opt/host/cfg")
        self.assertTrue(binds[0]["readonly"])

    def test_resources_convert_cpus_to_mhz_and_memory_to_mb(self):
        job = self.render(
            """
name: app
image: ghcr.io/acme/app:latest
region: cn
exposure: none
resources:
  limits:
    cpus: "0.5"
    memory: 256M
  reservations:
    cpus: "0.25"
    memory: 128M
"""
        )
        res = job["TaskGroups"][0]["Tasks"][0]["Resources"]
        # limit cpus 0.5 -> 500 MHz
        self.assertEqual(res["CPU"], 500)
        # reservation memory 128M -> MemoryMB, limit 256M -> MemoryMaxMB
        self.assertEqual(res["MemoryMB"], 128)
        self.assertEqual(res["MemoryMaxMB"], 256)

    def test_non_numeric_cpus_raises_named_error(self):
        with self.assertRaises(LumaError) as ctx:
            self.render(
                """
name: app
image: ghcr.io/acme/app:latest
region: cn
exposure: none
resources:
  limits:
    cpus: "half"
"""
            )
        # The error must name the offending field rather than surfacing a raw
        # ValueError from float("half").
        self.assertIn("cpus", str(ctx.exception))

    def test_cloudflare_tunnel_adds_sidecar_task(self):
        job = self.render(
            """
name: home-tool
image: ghcr.io/acme/tool:latest
region: home
exposure: cloudflare-tunnel
domain: tool.example.com
port: 8080
tunnel:
  tokenEnv: CLOUDFLARE_TUNNEL_TOKEN
""",
            secrets={"CLOUDFLARE_TUNNEL_TOKEN": "tunnel-secret"},
        )
        tasks = job["TaskGroups"][0]["Tasks"]
        names = {t["Name"] for t in tasks}
        self.assertEqual(names, {"home-tool", "cloudflared"})
        cf = next(t for t in tasks if t["Name"] == "cloudflared")
        self.assertEqual(cf["Env"]["TUNNEL_TOKEN"], "tunnel-secret")

    def test_worker_none_exposure_has_no_service_or_port(self):
        job = self.render(
            """
name: worker
image: ghcr.io/acme/worker:latest
region: global
exposure: none
"""
        )
        group = job["TaskGroups"][0]
        self.assertNotIn("Services", group)
        self.assertNotIn("Networks", group)

    def test_command_string_wrapped_in_shell(self):
        job = self.render(
            """
name: worker
image: alpine:3.20
region: global
exposure: none
command: "while true; do date; sleep 300; done"
"""
        )
        args = job["TaskGroups"][0]["Tasks"][0]["Config"]["args"]
        self.assertEqual(args[0], "sh")
        self.assertEqual(args[1], "-c")

    def test_registry_auth_injected_into_docker_config(self):
        service = self.load(
            """
name: priv
image: gcode.example.com:3000/team/app:latest
region: home
node: lab
exposure: none
"""
        )
        auth = {"username": "u", "password": "p", "serveraddress": "https://gcode.example.com:3000"}
        job = render_nomad_job(self.config(), service, as_json=False, registry_auth=auth)["Job"]
        cfg = job["TaskGroups"][0]["Tasks"][0]["Config"]
        self.assertEqual(cfg["auth"]["username"], "u")
        self.assertEqual(cfg["auth"]["password"], "p")
        self.assertEqual(cfg["auth"]["server_address"], "https://gcode.example.com:3000")

    def test_registry_auth_accepts_camelcase_server_address(self):
        service = self.load(
            """
name: priv
image: gcode.example.com:3000/team/app:latest
region: home
node: lab
exposure: none
"""
        )
        auth = {"username": "u", "password": "p", "serverAddress": "https://gcode.example.com:3000"}
        job = render_nomad_job(self.config(), service, as_json=False, registry_auth=auth)["Job"]
        cfg = job["TaskGroups"][0]["Tasks"][0]["Config"]
        self.assertEqual(cfg["auth"]["server_address"], "https://gcode.example.com:3000")

    def test_no_auth_block_when_no_credentials(self):
        service = self.load(
            """
name: pub
image: nginx:latest
region: cn
exposure: none
"""
        )
        job = render_nomad_job(self.config(), service, as_json=False)["Job"]
        self.assertNotIn("auth", job["TaskGroups"][0]["Tasks"][0]["Config"])

    def test_healthcheck_url_preserves_path_containing_letter_s(self):
        self.assertEqual(
            _healthcheck_url(["CMD-SHELL", "curl -fsS http://localhost:8080/status || exit 1"]),
            "http://localhost:8080/status",
        )

    def test_output_is_valid_json(self):
        service = self.load(
            """
name: app
image: ghcr.io/acme/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
"""
        )
        text = render_nomad_job(self.config(), service)
        parsed = json.loads(text)
        self.assertIn("Job", parsed)

    def test_control_job_pins_manager_and_binds_state(self):
        job = render_control_job(
            image="ghcr.io/acme/luma-control:v1",
            node_name="manager-1",
            as_json=False,
        )["Job"]
        self.assertEqual(job["ID"], "luma-control")
        # pinned to the manager node
        self.assertIn(
            {"LTarget": "${meta.luma_node_name}", "RTarget": "manager-1", "Operand": "="},
            job["Constraints"],
        )
        task = job["TaskGroups"][0]["Tasks"][0]
        self.assertEqual(
            task["Resources"],
            {"CPU": 500, "MemoryMB": 1024, "MemoryMaxMB": 0},
        )
        # bridge + port 8080 (reachable by Traefik, see migration notes)
        self.assertEqual(job["TaskGroups"][0]["Networks"][0]["Mode"], "bridge")
        self.assertEqual(job["TaskGroups"][0]["Networks"][0]["ReservedPorts"][0]["Value"], 8080)
        self.assertEqual(job["Update"]["HealthCheck"], "checks")
        service = job["TaskGroups"][0]["Services"][0]
        self.assertEqual(service["Provider"], "nomad")
        self.assertEqual(service["PortLabel"], "http")
        self.assertEqual(service["AddressMode"], "host")
        self.assertEqual(
            service["Checks"],
            [{
                "Name": "luma-control-health",
                "Type": "http",
                "PortLabel": "http",
                "Path": "/v1/health",
                "Interval": 10_000_000_000,
                "Timeout": 2_000_000_000,
            }],
        )
        # host binds via mount blocks, never the dangerous volumes shorthand
        self.assertNotIn("volumes", task["Config"])
        sources = {m["source"] for m in task["Config"]["mount"]}
        self.assertIn("/var/run/docker.sock", sources)
        self.assertIn("/opt/luma", sources)
        self.assertNotIn("/opt/luma/control", sources)
        self.assertNotIn("/opt/luma/routes", sources)

    def test_control_job_only_forwards_allowlisted_lae_file_url_and_timeout_values(self):
        canary = "inline-control-secret-must-not-enter-job"
        configured = {
            "LUMA_LAE_SERVICE_PRINCIPALS_FILE": "/opt/luma/control/lae-builder-principals.json",
            "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE": "/opt/luma/control/lae-runtime-principals.json",
            "LUMA_CREDENTIAL_BROKER_URL": "https://broker.internal/v1/redeem",
            "LUMA_CREDENTIAL_BROKER_TIMEOUT_SECONDS": "5",
            "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": "/opt/luma/control/credential-broker.token",
            "LUMA_OBJECT_SOURCE_BROKER_URL": "https://broker.internal/v1/objects",
            "LUMA_OBJECT_SOURCE_BROKER_TIMEOUT_SECONDS": "6.5",
            "LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE": "/opt/luma/control/object-broker.token",
            "LUMA_LAE_ADMIN_API_URL": "https://lae-api.internal/",
            "LUMA_LAE_ADMIN_TIMEOUT_SECONDS": "8",
            "LUMA_LAE_ADMIN_TOKEN_FILE": "/opt/luma/control/lae-admin.token",
            "LUMA_LAE_PLAN_SIGNING_KEYS_FILE": "/opt/luma/control/lae-plan-signing.json",
            "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": "registry.internal/lae/agent@sha256:" + "a" * 64,
            "LUMA_LAE_BUILDER_ALLOW_ANONYMOUS_REGISTRY": "1",
            "LUMA_LAE_BUILDER_ALLOW_BASIC_REGISTRY": "0",
            "LUMA_LAE_BUILDER_REGISTRY_INSECURE": "1",
            "LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON": '["docker.io","ghcr.io"]',
            "LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON": '["tecent"]',
            "LUMA_LAE_RUNTIME_STORAGE_CLASS": "cn-nfs",
            "LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS": "600",
            "LUMA_LAE_SERVICE_TOKEN": canary,
            "LUMA_LAE_SERVICE_PRINCIPALS_JSON": json.dumps({"token": canary}),
            "LUMA_CREDENTIAL_BROKER_TOKEN": canary,
            "UNRELATED_SECRET": canary,
        }
        job = render_control_job(
            image="ghcr.io/acme/luma-control:v1",
            node_name="manager-1",
            control_environment=configured,
            as_json=False,
        )["Job"]
        environment = job["TaskGroups"][0]["Tasks"][0]["Env"]
        self.assertEqual(
            environment["LUMA_LAE_ADMIN_API_URL"],
            "https://lae-api.internal",
        )
        for name in (
            "LUMA_LAE_SERVICE_PRINCIPALS_FILE",
            "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE",
            "LUMA_CREDENTIAL_BROKER_URL",
            "LUMA_CREDENTIAL_BROKER_TIMEOUT_SECONDS",
            "LUMA_CREDENTIAL_BROKER_TOKEN_FILE",
            "LUMA_OBJECT_SOURCE_BROKER_URL",
            "LUMA_OBJECT_SOURCE_BROKER_TIMEOUT_SECONDS",
            "LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE",
            "LUMA_LAE_ADMIN_API_URL",
            "LUMA_LAE_ADMIN_TIMEOUT_SECONDS",
            "LUMA_LAE_ADMIN_TOKEN_FILE",
            "LUMA_LAE_PLAN_SIGNING_KEYS_FILE",
            "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST",
            "LUMA_LAE_BUILDER_ALLOW_ANONYMOUS_REGISTRY",
            "LUMA_LAE_BUILDER_ALLOW_BASIC_REGISTRY",
            "LUMA_LAE_BUILDER_REGISTRY_INSECURE",
            "LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON",
            "LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON",
            "LUMA_LAE_RUNTIME_STORAGE_CLASS",
            "LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS",
        ):
            self.assertIn(name, environment)
        for name in (
            "LUMA_LAE_SERVICE_TOKEN",
            "LUMA_LAE_SERVICE_PRINCIPALS_JSON",
            "LUMA_CREDENTIAL_BROKER_TOKEN",
            "UNRELATED_SECRET",
        ):
            self.assertNotIn(name, environment)
        self.assertNotIn(canary, json.dumps(job, sort_keys=True))

    def test_control_job_rejects_paths_outside_mount_and_open_urls_or_timeouts(self):
        invalid = (
            {"LUMA_LAE_ADMIN_TOKEN_FILE": "/tmp/admin.token"},
            {"LUMA_LAE_ADMIN_TOKEN_FILE": "/opt/luma/control"},
            {"LUMA_LAE_ADMIN_TOKEN_FILE": "/opt/luma/control/../admin.token"},
            {"LUMA_LAE_ADMIN_TOKEN_FILE": "admin.token"},
            {"LUMA_CREDENTIAL_BROKER_URL": "http://broker.internal/redeem"},
            {"LUMA_CREDENTIAL_BROKER_URL": "https://user@broker.internal/redeem"},
            {"LUMA_CREDENTIAL_BROKER_URL": "https://broker.internal/redeem?token=x"},
            {"LUMA_LAE_ADMIN_API_URL": "https://lae-api.internal/admin"},
            {"LUMA_CREDENTIAL_BROKER_TIMEOUT_SECONDS": "0"},
            {"LUMA_OBJECT_SOURCE_BROKER_TIMEOUT_SECONDS": "31"},
            {"LUMA_LAE_ADMIN_TIMEOUT_SECONDS": "0.5"},
            {"LUMA_LAE_ADMIN_TIMEOUT_SECONDS": "nan"},
            {"LUMA_LAE_PLAN_SIGNING_KEYS_FILE": "/tmp/signing.json"},
            {"LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": "lae-agent:latest"},
            {"LUMA_LAE_BUILDER_ALLOW_ANONYMOUS_REGISTRY": "yes"},
            {"LUMA_LAE_BUILDER_ALLOW_BASIC_REGISTRY": "yes"},
            {"LUMA_LAE_BUILDER_REGISTRY_INSECURE": "2"},
            {"LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON": '["GHCR.IO"]'},
            {"LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON": '["ghcr.io","docker.io"]'},
            {"LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON": '["ghcr.io/path"]'},
            {"LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON": "[]"},
            {"LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON": '["tecent","aly"]'},
            {"LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON": '["tecent","tecent"]'},
            {"LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON": '["tecent secret"]'},
            {"LUMA_LAE_RUNTIME_STORAGE_CLASS": "../cn-nfs"},
            {"LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS": "29"},
            {"LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS": "3601"},
        )
        for environment in invalid:
            with self.subTest(environment=environment), self.assertRaises(LumaError):
                render_control_job(
                    image="ghcr.io/acme/luma-control:v1",
                    node_name="manager-1",
                    control_environment=environment,
                    as_json=False,
                )

    def test_traefik_job_persists_certs_via_named_volume_mount(self):
        job = render_traefik_job(
            image="traefik:v3.6",
            acme_email="ops@example.com",
            tcp_entrypoints=[3306],
            as_json=False,
        )["Job"]
        task = job["TaskGroups"][0]["Tasks"][0]
        self.assertEqual(task["Config"]["network_mode"], "host")
        # CRITICAL: letsencrypt must be a named-volume mount, never the volumes
        # shorthand (which would re-request certs every restart -> ACME limit).
        self.assertNotIn("volumes", task["Config"])
        le = [m for m in task["Config"]["mount"] if m["target"] == "/letsencrypt"][0]
        self.assertEqual(le["type"], "volume")
        self.assertEqual(le["source"], "traefik_traefik_letsencrypt")
        # tcp entrypoint + acme + nomad provider present
        args = task["Config"]["args"]
        self.assertIn("--entrypoints.tcp-3306.address=:3306", args)
        self.assertTrue(any("acme.email=ops@example.com" in a for a in args))
        self.assertTrue(any("providers.nomad=true" in a for a in args))
        self.assertIn("--providers.nomad.watch=true", args)
        self.assertIn("--accesslog=true", args)
        self.assertIn("--accesslog.format=json", args)
        self.assertFalse(any("accesslog.filepath" in a for a in args))
        self.assertFalse(any("providers.swarm" in a for a in args))

    def test_egress_job_static_proxy_port_and_config_bind(self):
        job = render_egress_job(image="metacubex/mihomo:latest", as_json=False)["Job"]
        net = job["TaskGroups"][0]["Networks"][0]
        self.assertEqual(net["ReservedPorts"][0]["Value"], 7890)
        task = job["TaskGroups"][0]["Tasks"][0]
        self.assertEqual(task["Config"]["mount"][0]["source"], "/opt/luma/egress-gateway")
        self.assertEqual(task["Config"]["args"], ["-d", "/opt/luma/egress-gateway"])

    def test_traefik_job_pins_to_ingress_meta(self):
        job = render_traefik_job(image="traefik:v3.6", as_json=False)["Job"]
        self.assertIn(
            {"LTarget": "${meta.ingress}", "RTarget": "true", "Operand": "="},
            job["Constraints"],
        )


class InternalFixedPortTests(unittest.TestCase):
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

    def load(self, content: str):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            tmp.write(content)
            tmp.close()
            return load_service(Path(tmp.name))
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_internal_service_with_publish_port_uses_static_reserved_port(self):
        service = self.load(
            """
name: luma-registry
image: registry:2
region: cn
exposure: none
node: build-1
port: 5000
publishPort: 5000
volumes:
  - luma-registry-data:/var/lib/registry
"""
        )
        job = render_nomad_job(self.config(), service, as_json=False)["Job"]
        net = job["TaskGroups"][0]["Networks"][0]
        self.assertEqual(net["Mode"], "bridge")
        reserved = net["ReservedPorts"][0]
        self.assertEqual(reserved["Value"], 5000)
        self.assertEqual(reserved["To"], 5000)
        # named volume bind for registry data
        mounts = job["TaskGroups"][0]["Tasks"][0]["Config"]["mount"]
        self.assertTrue(any(m["target"] == "/var/lib/registry" and m["type"] == "volume" for m in mounts))

    def test_internal_service_without_publish_port_stays_dynamic(self):
        service = self.load(
            """
name: worker
image: ghcr.io/acme/worker:latest
region: global
exposure: none
port: 8080
"""
        )
        job = render_nomad_job(self.config(), service, as_json=False)["Job"]
        net = job["TaskGroups"][0]["Networks"][0]
        self.assertEqual(net["Mode"], "host")
        self.assertEqual(net["DynamicPorts"][0]["To"], 8080)


if __name__ == "__main__":
    unittest.main()
