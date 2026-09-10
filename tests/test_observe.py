import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.errors import LumaError
from luma.nomad_render import render_compose_job, render_traefik_job
try:
    from test_nomad_compose import cfg, write_deployment
except ImportError:
    from tests.test_nomad_compose import cfg, write_deployment


ROOT = Path(__file__).resolve().parents[1]


def load_observe(filename: str, name: str):
    path = ROOT / "observe" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TraefikObserveRenderTests(unittest.TestCase):
    def test_traefik_exposes_loopback_prometheus_and_otlp(self):
        job = render_traefik_job(image="traefik:v3.6", as_json=False)["Job"]
        args = job["TaskGroups"][0]["Tasks"][0]["Config"]["args"]
        self.assertIn("--entrypoints.metrics.address=127.0.0.1:8082", args)
        self.assertIn("--metrics.prometheus=true", args)
        self.assertIn("--metrics.prometheus.addRoutersLabels=true", args)
        self.assertIn("--metrics.prometheus.buckets=0.05,0.1,0.25,0.5,1,2.5,5,10", args)
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
        names = {task["Name"] for task in job["TaskGroups"][0]["Tasks"]}
        self.assertIn("grafana", names)
        self.assertIn("host-gateway", names)
        self.assertIn("tempo", names)
        self.assertIn("victoria-logs", names)
        self.assertIn("log-shipper", names)
        self.assertTrue(all(task["Config"].get("network_mode") == "host" for task in job["TaskGroups"][0]["Tasks"]))
        victoria = next(task for task in job["TaskGroups"][0]["Tasks"] if task["Name"] == "victoria")
        self.assertEqual(victoria["Config"]["mount"][0]["source"], "/srv/luma/data/luma-observe/victoria")
        logs = next(task for task in job["TaskGroups"][0]["Tasks"] if task["Name"] == "victoria-logs")
        self.assertEqual(logs["Config"]["mount"][0]["source"], "/srv/luma/data/luma-observe/victorialogs")


class NomadExporterTests(unittest.TestCase):
    def test_collects_failed_running_and_restarts(self):
        exporter = load_observe("nomad_exporter.py", "nomad_exporter")
        jobs = [
            {
                "ID": "granary",
                "Name": "granary",
                "Status": "running",
                "Meta": {"luma.managed": "true", "luma.compose": "true"},
                "JobSummary": {"Summary": {"granary": {"Running": 1, "Failed": 21, "Queued": 0}}},
            }
        ]
        allocations = [
            {
                "ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "JobID": "granary",
                "TaskGroup": "granary",
                "ClientStatus": "running",
                "DesiredStatus": "run",
                "TaskStates": {"app": {"Restarts": 6}, "mysql": {"Restarts": 0}},
            },
            {
                "ID": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee",
                "JobID": "granary",
                "TaskGroup": "granary",
                "ClientStatus": "failed",
                "DesiredStatus": "stop",
                "TaskStates": {"app": {"Restarts": 3}},
            },
        ]

        def fake_fetch(addr, path, token, timeout=4.0):
            self.assertEqual(addr, "http://127.0.0.1:4646")
            if path.startswith("/v1/jobs"):
                self.assertIn("meta=true", path)
                return jobs
            if path.startswith("/v1/nodes"):
                return []
            return allocations

        with patch.object(exporter, "fetch_json", side_effect=fake_fetch):
            text = exporter.collect("http://127.0.0.1:4646", "", routes_dir=Path("/tmp/missing-luma-routes"))
        self.assertIn("luma_observe_nomad_up 1", text)
        self.assertIn('luma_observe_job_failed{app="granary",job="granary",task_group="granary"} 0', text)
        self.assertIn('luma_observe_job_running{app="granary",job="granary",task_group="granary"} 1', text)
        self.assertIn('luma_observe_job_active{app="granary",job="granary",task_group="granary"} 1', text)
        self.assertIn('luma_observe_allocs{app="granary",job="granary",task_group="granary",status="failed"} 1', text)
        self.assertIn('luma_observe_alloc_restarts{app="granary",job="granary",task_group="granary",alloc="aaaaaaaa",task="app"} 6', text)
        self.assertIn('luma_observe_router_app{app="granary",service="app",router="granary-app@nomad"} 1', text)
        self.assertIn('luma_observe_router_app{app="granary",service="granary",router="granary@nomad"} 1', text)
        self.assertNotIn('alloc="bbbbbbbb"', text)

    def test_current_failed_counts_desired_run_only(self):
        exporter = load_observe("nomad_exporter.py", "nomad_exporter")
        jobs = [{
            "ID": "word2pdf",
            "Name": "word2pdf",
            "Status": "running",
            "Meta": {"luma.compose": "true"},
            "JobSummary": {"Summary": {"word2pdf": {"Running": 0, "Failed": 9, "Queued": 0}}},
        }]
        allocations = [{
            "ID": "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee",
            "JobID": "word2pdf",
            "TaskGroup": "word2pdf",
            "ClientStatus": "failed",
            "DesiredStatus": "run",
            "TaskStates": {"web": {"Restarts": 1}},
        }]
        def fake_fetch(addr, path, token, timeout=4.0):
            if path.startswith("/v1/jobs"):
                return jobs
            if path.startswith("/v1/nodes"):
                return []
            return allocations
        with patch.object(exporter, "fetch_json", side_effect=fake_fetch):
            text = exporter.collect("http://127.0.0.1:4646", "", routes_dir=Path("/tmp/missing-luma-routes"))
        self.assertIn('luma_observe_job_failed{app="word2pdf",job="word2pdf",task_group="word2pdf"} 1', text)
        self.assertIn('luma_observe_router_app{app="word2pdf",service="web",router="word2pdf-web@nomad"} 1', text)

    def test_nomad_down_is_explicit(self):
        exporter = load_observe("nomad_exporter.py", "nomad_exporter")
        with patch.object(exporter, "fetch_json", side_effect=TimeoutError):
            text = exporter.collect("http://127.0.0.1:4646", "")
        self.assertIn("luma_observe_nomad_up 0", text)
        self.assertNotIn("luma_observe_job_failed", text)
        self.assertNotIn("luma_observe_node_ready", text)

    def test_collects_node_ready_and_allocs_by_node(self):
        exporter = load_observe("nomad_exporter.py", "nomad_exporter")
        jobs = [{
            "ID": "word2pdf",
            "Name": "word2pdf",
            "Status": "running",
            "JobSummary": {"Summary": {"word2pdf": {"Running": 1, "Failed": 0, "Queued": 0}}},
        }]
        allocations = [{
            "ID": "dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee",
            "JobID": "word2pdf",
            "TaskGroup": "word2pdf",
            "NodeName": "manager",
            "ClientStatus": "running",
            "DesiredStatus": "run",
            "TaskStates": {"web": {"Restarts": 0}},
        }]
        nodes = [{
            "ID": "aaaa",
            "Name": "iZexample",
            "Status": "ready",
            "SchedulingEligibility": "ineligible",
            "Meta": {"luma_node_name": "aly", "region": "cn"},
        }, {
            "ID": "bbbb",
            "Name": "manager",
            "Status": "ready",
            "SchedulingEligibility": "eligible",
            "Meta": {"luma_node_name": "manager", "region": "cn"},
        }]

        def fake_fetch(addr, path, token, timeout=4.0):
            if path.startswith("/v1/jobs"):
                return jobs
            if path.startswith("/v1/nodes"):
                return nodes
            return allocations

        with patch.object(exporter, "fetch_json", side_effect=fake_fetch):
            text = exporter.collect("http://127.0.0.1:4646", "")
        self.assertIn('luma_observe_node_ready{node="aly",region="cn"} 1', text)
        self.assertIn('luma_observe_node_eligible{node="aly",region="cn"} 0', text)
        self.assertIn('luma_observe_node_eligible{node="manager",region="cn"} 1', text)
        self.assertIn('luma_observe_node_allocs{node="manager",status="running"} 1', text)


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


class HostGatewayTests(unittest.TestCase):
    def test_skips_loopback_and_unknown_ifaces(self):
        gateway = load_observe("host_gateway.py", "host_gateway")
        with patch.object(gateway, "ipv4_of_interface", side_effect=lambda name: "127.0.0.1" if name == "lo" else None):
            self.assertEqual(gateway.gateway_bind_ips(["lo", "missing"]), [])

    def test_binds_nomad_then_docker0(self):
        gateway = load_observe("host_gateway.py", "host_gateway")
        with patch.object(gateway, "ipv4_of_interface", side_effect=lambda name: {"nomad": "172.26.64.1", "docker0": "172.17.0.1"}.get(name)):
            self.assertEqual(gateway.gateway_bind_ips(["nomad", "docker0"]), ["172.26.64.1", "172.17.0.1"])


class ObserveHttpLatencyTests(unittest.TestCase):
    def test_recording_rules_join_duration_buckets_to_app(self):
        text = (ROOT / "observe" / "vmalert-rules.yml").read_text(encoding="utf-8")
        self.assertIn("luma_app:http_duration_seconds_bucket:rate5m", text)
        self.assertIn("traefik_router_request_duration_seconds_bucket", text)
        self.assertIn("* on (router) group_left(app) luma_observe_router_app", text)
        self.assertIn("histogram_quantile(0.90, luma_app:http_duration_seconds_bucket:rate5m)", text)
        self.assertIn("histogram_quantile(0.95, luma_app:http_duration_seconds_bucket:rate5m)", text)
        self.assertIn("histogram_quantile(0.99, luma_app:http_duration_seconds_bucket:rate5m)", text)

    def test_http_dashboard_plots_p90_p95_p99(self):
        dashboard = json.loads((ROOT / "observe" / "grafana" / "dashboards" / "http-apps.json").read_text(encoding="utf-8"))
        panel = next(item for item in dashboard["panels"] if item["title"] == "Latency by app")
        exprs = [target["expr"] for target in panel["targets"]]
        self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "s")
        self.assertIn('luma_app:http_duration_seconds:p90{app=~"$app"}', exprs)
        self.assertIn('luma_app:http_duration_seconds:p95{app=~"$app"}', exprs)
        self.assertIn('luma_app:http_duration_seconds:p99{app=~"$app"}', exprs)


class ObserveTraceStackTests(unittest.TestCase):
    def test_collector_writes_sampled_traces_to_tempo(self):
        text = (ROOT / "observe" / "collector.yaml").read_text(encoding="utf-8")
        self.assertIn("otlphttp/tempo", text)
        self.assertIn("http://127.0.0.1:4418", text)
        self.assertIn("tail_sampling", text)
        self.assertNotIn("probabilistic_sampler", text)
        self.assertIn("processors: [memory_limiter, tail_sampling, batch]", text)
        self.assertIn("http.response.status_code", text)
        self.assertIn("sampling_percentage: 10", text)
        self.assertIn("otlp/mesh", text)
        self.assertIn("bearertokenauth", text)
        self.assertIn("${env:LUMA_OTLP_MESH_BIND}:4319", text)

    def test_tempo_retains_fifteen_days(self):
        text = (ROOT / "observe" / "tempo.yaml").read_text(encoding="utf-8")
        self.assertIn("block_retention: 336h", text)
        self.assertIn("http_listen_port: 3200", text)
        self.assertIn("127.0.0.1:4418", text)
        self.assertIn("stream_over_http_enabled: true", text)

    def test_grafana_has_tempo_and_traces_dashboard(self):
        sources = (ROOT / "observe" / "grafana" / "provisioning" / "datasources" / "datasource.yml").read_text(encoding="utf-8")
        self.assertIn("uid: tempo", sources)
        dashboard = json.loads((ROOT / "observe" / "grafana" / "dashboards" / "traces.json").read_text(encoding="utf-8"))
        self.assertEqual(dashboard["uid"], "luma-traces")
        ingress, apps = dashboard["panels"]
        self.assertEqual(ingress["type"], "table")
        self.assertEqual(ingress["targets"][0]["tableType"], "traces")
        self.assertIn("resource.service.name = \"traefik\"", ingress["targets"][0]["query"])
        self.assertNotIn("$app", ingress["targets"][0]["query"])
        self.assertIn('resource.luma.stack =~ "$app"', apps["targets"][0]["query"])

    def test_apps_traces_tab_embeds_tempo_explore_without_kiosk(self):
        text = (ROOT / "dashboard-src" / "src" / "components" / "ObserveAppsPanel.tsx").read_text(encoding="utf-8")
        self.assertIn("/grafana/explore?orgId=1&schemaVersion=1&panes=", text)
        self.assertNotRegex(text, r"explore\?[^\"'\s]*kiosk")
        self.assertIn('queryType: "traceql"', text)
        self.assertIn('uid: "tempo"', text)
        self.assertIn("resource.luma.stack", text)
        self.assertIn("grafana-app-filter", text)
        self.assertIn('uid: "luma-nodes"', text)
        self.assertIn("autofitpanels", text)
        self.assertIn('uid: "victorialogs"', text)
        self.assertIn('app:="', text)
        page = (ROOT / "dashboard-src" / "src" / "pages" / "ObservabilityPage.tsx").read_text(encoding="utf-8")
        self.assertIn("initialView={section === \"logs\" ? \"logs\" : \"http\"}", page)
        self.assertNotIn("ServiceLogsModal", page)

    def test_nodes_dashboard_plots_nomad_node_gauges(self):
        dashboard = json.loads((ROOT / "observe" / "grafana" / "dashboards" / "nodes.json").read_text(encoding="utf-8"))
        self.assertEqual(dashboard["uid"], "luma-nodes")
        exprs = [target["expr"] for panel in dashboard["panels"] for target in panel["targets"]]
        self.assertIn("count(luma_observe_node_ready == 1) or vector(0)", exprs)
        self.assertIn("luma_node_cpu_used_ratio", exprs)
        self.assertIn("luma_node_memory_used_ratio", exprs)
        self.assertIn('luma_service_cpu_cores{service=~"$app.*"}', "".join(exprs))
        self.assertEqual(dashboard["templating"]["list"][0]["name"], "app")

    def test_collector_scrapes_control_host_metrics(self):
        text = (ROOT / "observe" / "collector.yaml").read_text(encoding="utf-8")
        self.assertIn("job_name: luma-control", text)
        self.assertIn("127.0.0.1:8080", text)
        self.assertIn("metrics_path: /v1/metrics", text)
        self.assertIn("127.0.0.1:9108", text)
        self.assertNotIn("\n    logs:\n      receivers:", text)

    def test_victorialogs_is_loopback_with_fifteen_day_retention(self):
        compose = (ROOT / "observe" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:9428", compose)
        self.assertIn("retentionPeriod=15d", compose)
        self.assertNotIn("0.0.0.0:9428", compose)
        sources = (ROOT / "observe" / "grafana" / "provisioning" / "datasources" / "datasource.yml").read_text(encoding="utf-8")
        self.assertIn("uid: victorialogs", sources)
        self.assertIn("http://127.0.0.1:9428", sources)
        dockerfile = (ROOT / "observe" / "Dockerfile.grafana").read_text(encoding="utf-8")
        self.assertIn("victoriametrics-logs-datasource", dockerfile)

    def test_grafana_lets_anonymous_viewers_open_explore(self):
        text = (ROOT / "observe" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('GF_USERS_VIEWERS_CAN_EDIT: "true"', text)
        self.assertIn('GF_AUTH_ANONYMOUS_ORG_ROLE: "Viewer"', text)
        self.assertNotIn("itool.tech", text)
        self.assertNotIn("GF_SERVER_DOMAIN", text)
        self.assertNotIn("GF_SERVER_ROOT_URL", text)


class GrafanaRouteTests(unittest.TestCase):
    def test_grafana_route_is_on_the_control_domain(self):
        from luma.observe_grafana import grafana_route_yaml
        text = grafana_route_yaml("luma.itool.tech")
        self.assertIn("Host(`luma.itool.tech`) && PathPrefix(`/grafana`)", text)
        self.assertIn("priority: 1000", text)
        self.assertIn("url: http://127.0.0.1:3100", text)
        self.assertNotIn("100.106.154.3:3000", text)
        self.assertNotIn(":3000", text.split("url:")[-1])

