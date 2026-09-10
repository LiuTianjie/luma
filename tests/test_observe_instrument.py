import unittest
from pathlib import Path

from luma.nomad_render import render_compose_job, render_nomad_job
from luma.observe_instrument import OBSERVE_STACK, apply_env, manager_tailscale_ip, otlp_mesh_endpoint, pin_observe_placement
from luma.service import load_service
try:
    from test_nomad_compose import cfg
except ImportError:
    from tests.test_nomad_compose import cfg


ROOT = Path(__file__).resolve().parents[1]


class ObserveInstrumentTests(unittest.TestCase):
    def test_manager_tailscale_ip_prefers_manager_node(self):
        ip = manager_tailscale_ip({
            "worker": {"tailscaleIP": "100.1.1.1"},
            "manager": {
                "status": "manager",
                "tailscaleIP": "100.66.177.70",
                "labels": {"role.nomad-manager": "true"},
            },
        })
        self.assertEqual(ip, "100.66.177.70")
        self.assertEqual(otlp_mesh_endpoint(ip), "http://100.66.177.70:4319")

    def test_apply_env_injects_collector_vars_for_observe_stack(self):
        env = apply_env(
            {},
            stack=OBSERVE_STACK,
            task="collector",
            region="cn",
            observe_otlp={"token": "observe-token-observe-token-observe", "mesh_bind": "100.66.177.70"},
        )
        self.assertEqual(env["LUMA_OTLP_TOKEN"], "observe-token-observe-token-observe")
        self.assertEqual(env["LUMA_OTLP_MESH_BIND"], "100.66.177.70")
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", env)
        self.assertNotIn("GF_SERVER_DOMAIN", env)

    def test_grafana_public_url_comes_from_control_domain(self):
        env = apply_env(
            {"GF_SERVER_DOMAIN": "stale.example", "GF_SERVER_ROOT_URL": "https://stale.example/grafana"},
            stack=OBSERVE_STACK,
            task="grafana",
            region="cn",
            observe_otlp={
                "token": "observe-token-observe-token-observe",
                "mesh_bind": "100.66.177.70",
                "grafana_domain": "luma.example.com",
            },
        )
        self.assertEqual(env["GF_SERVER_DOMAIN"], "luma.example.com")
        self.assertEqual(env["GF_SERVER_ROOT_URL"], "https://luma.example.com/grafana")

    def test_apply_env_does_not_override_app_endpoint(self):
        env = apply_env(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"},
            stack="granary",
            task="web",
            region="cn",
            observe_otlp={
                "token": "observe-token-observe-token-observe",
                "mesh_bind": "100.66.177.70",
                "endpoint": "http://100.66.177.70:4319",
            },
        )
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://127.0.0.1:4318")
        self.assertIn("Authorization=Bearer ", env["OTEL_EXPORTER_OTLP_HEADERS"])

    def test_compose_observe_job_gets_collector_env(self):
        from luma.compose import load_compose_deployment

        dep = load_compose_deployment(ROOT / "observe" / "luma.compose.yml", allow_build_services=True)
        job = render_compose_job(
            cfg(),
            dep,
            as_json=False,
            resolve_secrets=False,
            observe_otlp={
                "token": "observe-token-observe-token-observe",
                "mesh_bind": "100.66.177.70",
                "endpoint": "http://100.66.177.70:4319",
                "grafana_domain": "luma.example.com",
            },
        )["Job"]
        names = {task["Name"] for task in job["TaskGroups"][0]["Tasks"]}
        self.assertIn("tempo", names)
        collector = next(task for task in job["TaskGroups"][0]["Tasks"] if task["Name"] == "collector")
        self.assertEqual(collector["Env"]["LUMA_OTLP_MESH_BIND"], "100.66.177.70")
        grafana = next(task for task in job["TaskGroups"][0]["Tasks"] if task["Name"] == "grafana")
        self.assertEqual(grafana["Env"]["GF_SERVER_DOMAIN"], "luma.example.com")
        self.assertEqual(grafana["Env"]["GF_SERVER_ROOT_URL"], "https://luma.example.com/grafana")
        tempo = next(task for task in job["TaskGroups"][0]["Tasks"] if task["Name"] == "tempo")
        self.assertEqual(tempo["Config"]["mount"][0]["source"], "/srv/luma/data/luma-observe/tempo")
        self.assertEqual(dep.volumes["tempo-data"].initialize, "empty")

    def test_observe_pins_to_registered_manager_not_sidecar_name(self):
        from luma.compose import load_compose_deployment

        dep = load_compose_deployment(ROOT / "observe" / "luma.compose.yml", allow_build_services=True)
        self.assertEqual(dep.region, "cn")
        pinned = pin_observe_placement(dep, {
            "home": {
                "status": "manager",
                "region": "global",
                "labels": {"role.nomad-manager": "true"},
            },
        })
        self.assertEqual(pinned.region, "global")
        self.assertTrue(all(svc.node == "home" for svc in pinned.services.values()))
        job = render_compose_job(
            cfg(),
            dep,
            as_json=False,
            resolve_secrets=False,
            render_storage=False,
            node_records={
                "home": {
                    "status": "manager",
                    "region": "global",
                    "labels": {"role.nomad-manager": "true"},
                },
            },
        )["Job"]
        self.assertIn({"LTarget": "${meta.luma_node_name}", "RTarget": "home", "Operand": "="}, job["Constraints"])
        self.assertIn({"LTarget": "${meta.region}", "RTarget": "global", "Operand": "="}, job["Constraints"])

    def test_app_job_gets_otlp_when_observe_is_on(self):
        service = load_service_from_text(
            """
name: demo
image: nginx:alpine
region: cn
exposure: none
"""
        )
        job = render_nomad_job(
            cfg(),
            service,
            as_json=False,
            observe_otlp={
                "token": "observe-token-observe-token-observe",
                "mesh_bind": "100.66.177.70",
                "endpoint": "http://100.66.177.70:4319",
            },
        )["Job"]
        env = job["TaskGroups"][0]["Tasks"][0]["Env"]
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://100.66.177.70:4319")
        self.assertEqual(env["OTEL_SERVICE_NAME"], "demo")
        self.assertIn("luma.stack=demo", env["OTEL_RESOURCE_ATTRIBUTES"])


def load_service_from_text(text: str):
    import tempfile
    from pathlib import Path as P

    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    try:
        tmp.write(text)
        tmp.close()
        return load_service(P(tmp.name))
    finally:
        P(tmp.name).unlink(missing_ok=True)
