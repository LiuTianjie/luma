import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.errors import LumaError
from luma.nomad_render import render_compose_job, render_traefik_job
from tests.test_nomad_compose import cfg, write_deployment


ROOT = Path(__file__).resolve().parents[1]


def load_observe(filename: str, name: str):
    path = ROOT / "observe" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TraefikObserveRenderTests(unittest.TestCase):
    def test_traefik_exposes_loopback_prometheus_and_otlp(self):
        job = render_traefik_job(image="traefik:v3.6", as_json=False)["Job"]
        args = job["TaskGroups"][0]["Tasks"][0]["Config"]["args"]
        self.assertIn("--entrypoints.metrics.address=127.0.0.1:8082", args)
        self.assertIn("--metrics.prometheus=true", args)
        self.assertIn("--metrics.prometheus.addRoutersLabels=true", args)
        self.assertIn("--metrics.otlp.http.endpoint=http://127.0.0.1:4318", args)
        self.assertIn("--tracing.otlp.http.endpoint=http://127.0.0.1:4318", args)
        self.assertFalse(any(value.startswith("--entrypoints.metrics.address=0.0.0.0") for value in args))
        self.assertFalse(any(value.startswith("--entrypoints.metrics.address=:8082") for value in args))


class ComposeHostNetworkTests(unittest.TestCase):
    def test_host_network_skips_bridge_and_pins_docker_host(self):
        compose = """
services:
  collector:
    image: otel/opentelemetry-collector-contrib:0.122.1
    network_mode: host
  victoria:
    image: victoriametrics/victoria-metrics:v1.114.0
    network_mode: host
"""
        sidecar = """
name: luma-observe
compose: docker-compose.yml
region: cn
services:
  collector:
    node: manager
    exposure: none
  victoria:
    node: manager
    exposure: none
"""
        job = render_compose_job(cfg(), write_deployment(sidecar, compose), as_json=False)["Job"]
        group = job["TaskGroups"][0]
        self.assertNotIn("Networks", group)
        for task in group["Tasks"]:
            self.assertEqual(task["Config"]["network_mode"], "host")
            self.assertNotIn("ports", task["Config"])

    def test_host_network_rejects_public_exposure(self):
        compose = """
services:
  web:
    image: traefik/whoami:latest
    network_mode: host
"""
        sidecar = """
name: bad-observe
compose: docker-compose.yml
region: cn
services:
  web:
    node: manager
    exposure: cn-edge
    domain: observe.example.com
    port: 80
"""
        with self.assertRaisesRegex(LumaError, "exposure: none"):
            render_compose_job(cfg(), write_deployment(sidecar, compose), as_json=False)

    def test_host_network_rejects_mixed_modes(self):
        compose = """
services:
  collector:
    image: otel/opentelemetry-collector-contrib:0.122.1
    network_mode: host
  victoria:
    image: victoriametrics/victoria-metrics:v1.114.0
"""
        sidecar = """
name: luma-observe
compose: docker-compose.yml
region: cn
services:
  collector:
    node: manager
    exposure: none
  victoria:
    node: manager
    exposure: none
"""
        with self.assertRaisesRegex(LumaError, "cannot mix"):
            render_compose_job(cfg(), write_deployment(sidecar, compose), as_json=False)


class RepoManifestTests(unittest.TestCase):
    def test_repo_manifest_renders_host_network(self):
        from luma.compose import load_compose_deployment
        dep = load_compose_deployment(ROOT / "observe" / "luma.compose.yml", allow_build_services=True)
        job = render_compose_job(cfg(), dep, as_json=False, resolve_secrets=False)["Job"]
        self.assertEqual(dep.name, "luma-observe")
        self.assertNotIn("Networks", job["TaskGroups"][0])
        self.assertTrue(all(task["Config"].get("network_mode") == "host" for task in job["TaskGroups"][0]["Tasks"]))
        victoria = next(task for task in job["TaskGroups"][0]["Tasks"] if task["Name"] == "victoria")
        self.assertEqual(victoria["Config"]["mount"][0]["source"], "/srv/luma/data/luma-observe/victoria")


class NomadExporterTests(unittest.TestCase):
    def test_collects_failed_running_and_restarts(self):
        exporter = load_observe("nomad_exporter.py", "nomad_exporter")
        jobs = [
            {
                "ID": "granary",
                "Name": "granary",
                "JobSummary": {"Summary": {"granary": {"Running": 0, "Failed": 21, "Queued": 0}}},
            }
        ]
        allocations = [
            {
                "ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "JobID": "granary",
                "TaskGroup": "granary",
                "ClientStatus": "failed",
                "TaskStates": {"app": {"Restarts": 6}},
            }
        ]

        def fake_fetch(addr, path, token, timeout=4.0):
            self.assertEqual(addr, "http://127.0.0.1:4646")
            if path.startswith("/v1/jobs"):
                return jobs
            return allocations

        with patch.object(exporter, "fetch_json", side_effect=fake_fetch):
            text = exporter.collect("http://127.0.0.1:4646", "")
        self.assertIn("luma_observe_nomad_up 1", text)
        self.assertIn('luma_observe_job_failed{job="granary",task_group="granary"} 21', text)
        self.assertIn('luma_observe_job_running{job="granary",task_group="granary"} 0', text)
        self.assertIn('luma_observe_allocs{job="granary",task_group="granary",status="failed"} 1', text)
        self.assertIn('luma_observe_alloc_restarts{job="granary",task_group="granary",alloc="aaaaaaaa",task="app"} 6', text)

    def test_nomad_down_is_explicit(self):
        exporter = load_observe("nomad_exporter.py", "nomad_exporter")
        with patch.object(exporter, "fetch_json", side_effect=TimeoutError):
            text = exporter.collect("http://127.0.0.1:4646", "")
        self.assertIn("luma_observe_nomad_up 0", text)
        self.assertNotIn("luma_observe_job_failed", text)


class FeishuWebhookTests(unittest.TestCase):
    def test_format_includes_router_and_summary(self):
        webhook = load_observe("feishu_webhook.py", "feishu_webhook")
        text = webhook.format_text(
            {
                "status": "firing",
                "alerts": [
                    {
                        "labels": {"alertname": "LumaAppHTTPErrorRate", "router": "word2pdf-web@nomad"},
                        "annotations": {"summary": "5xx ratio is above 5%"},
                    }
                ],
            }
        )
        self.assertIn("告警触发", text)
        self.assertIn("word2pdf-web@nomad", text)
        self.assertIn("5xx ratio is above 5%", text)

    def test_missing_credentials_are_skipped(self):
        webhook = load_observe("feishu_webhook.py", "feishu_webhook")
        with patch.dict("os.environ", {"FEISHU_WEBHOOK_URL": "", "FEISHU_APP_ID": ""}, clear=False):
            self.assertEqual(webhook.deliver({"status": "firing", "alerts": []}), "skipped")
