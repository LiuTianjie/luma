#!/usr/bin/env python3
"""Verify SDK OTLP metrics and generated dashboards against isolated official components."""
from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid

import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("observe_protocols", ROOT / "scripts/check-observe-protocols.py")
protocols = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocols)
docker = protocols.docker

PRODUCE = """
import subprocess, time
from opentelemetry.metrics import Observation
from luma_observe import configure

runtime = configure('example-worker', metric_interval_ms=300000,
                    resource_attributes={'deployment.environment.name': 'protocol'})
assert runtime.enabled, runtime.initialization_error
processed = runtime.counter('document.processed')
processed.add(0, {'document.format': 'pdf', 'outcome': 'succeeded'})
runtime.get_meter().create_observable_gauge('document.last_completion', unit='s',
    callbacks=[lambda _: [Observation(time.time())]])
for iteration in range(3):
    try:
        with runtime.task('example.fail', framework='manual', queue='example', queue_wait_seconds=0.25):
            raise ValueError('synthetic failure')
    except ValueError:
        pass
    processed.add(1, {'document.format': 'pdf', 'outcome': 'failed'})
    assert runtime.meter_provider.force_flush(timeout_millis=3000)
    time.sleep(3)
for name in ('tasks', 'custom-metrics'):
    subprocess.run(['luma-observe-dashboard', f'/opt/sdk/examples/dashboards/{name}.config.json',
                    '--output', f'/extensions/dashboards/{name}.json'], check=True)
runtime.shutdown()
print('SDK exported three cumulative samples and rendered both dashboards', flush=True)
"""

ACTIVE_PRODUCE = """
from pathlib import Path
import sys, time
from luma_observe import configure

sys.stdout = sys.stderr = open('/tmp/sdk-active-producer.log', 'w', buffering=1)
runtime = configure('example-worker', metric_interval_ms=1000,
                    resource_attributes={'deployment.environment.name': 'protocol'})
assert runtime.enabled, runtime.initialization_error
with runtime.task('example.running', framework='manual', queue='example'):
    with runtime.task('example.completed', framework='manual', queue='example'):
        assert runtime.meter_provider.force_flush(timeout_millis=3000)
    assert runtime.meter_provider.force_flush(timeout_millis=3000)
    Path('/tmp/sdk-active-ready').touch()
    deadline = time.monotonic() + 300
    while not Path('/tmp/sdk-active-stop').exists():
        if time.monotonic() > deadline:
            raise TimeoutError('active fixture was not stopped by the protocol check')
        time.sleep(1)
assert runtime.meter_provider.force_flush(timeout_millis=3000)
runtime.shutdown()
Path('/tmp/sdk-active-stopped').touch()
"""

CHURN_PRODUCE = """
from luma_observe import configure

runtime = configure('sdk-churn-worker', metric_interval_ms=300000,
                    resource_attributes={'deployment.environment.name': 'protocol'})
assert runtime.enabled, runtime.initialization_error
for iteration in range(16):
    with runtime.task('example.churn', framework='manual', queue='example', queue_wait_seconds=0.125):
        if iteration == 0:
            assert runtime.meter_provider.force_flush(timeout_millis=3000)
assert runtime.meter_provider.force_flush(timeout_millis=3000)
runtime.shutdown()
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", help="Docker platform; defaults to the Docker host architecture")
    args = parser.parse_args()
    platform = ["--platform", args.platform] if args.platform else []
    prefix = "luma-sdk-check-" + uuid.uuid4().hex[:12]
    compose = yaml.safe_load((ROOT / "observe/docker-compose.yml").read_text())
    services = ["victoria", "collector", "grafana"]
    names = {name: prefix + "-" + name for name in ["probe", *services]}
    images = {name: prefix + "-" + name + ":local" for name in ("probe", "collector", "grafana")}
    volumes = {name: prefix + "-" + name for name in ("victoria", "extensions")}
    shared_images = {"python:3.12-alpine", compose["services"]["victoria"]["image"]}
    existing_images = set(docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").splitlines())

    def request(port: int, path: str) -> dict:
        payload = {"url": f"http://127.0.0.1:{port}{path}", "method": "GET", "body": None, "headers": {}}
        return json.loads(docker("exec", "-i", names["probe"], "python", "-c", protocols.HTTP, stdin=json.dumps(payload)))

    def wait_for(description: str, check, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                print(description, flush=True)
                return
            time.sleep(2)
        raise RuntimeError(f"timed out: {description}")

    def query(expression: str) -> list:
        response = request(8428, "/api/v1/query?" + urllib.parse.urlencode({"query": expression}))
        if response["status"] != 200:
            raise RuntimeError(f"metrics query failed: {response}")
        result = json.loads(response["body"])
        if result.get("status") != "success":
            raise RuntimeError(f"metrics query failed: {result}")
        return result["data"]["result"]

    def value(expression: str):
        rows = query(expression)
        return float(rows[0]["value"][1]) if rows else None

    def fixture_marker(name: str) -> bool:
        code = f"from pathlib import Path; print(Path('/tmp/sdk-active-{name}').exists())"
        return docker("exec", names["probe"], "python", "-c", code).strip() == "True"

    def diagnose_empty_panel(expression: str) -> None:
        print("Empty panel expression: " + expression, file=sys.stderr)
        # Preserve each join component and its sample age before finally removes
        # the isolated stack. This distinguishes naming, labels and stale data.
        for selector in dict.fromkeys(re.findall(r"\b[A-Za-z_:][A-Za-z0-9_:]*\{[^{}]*\}", expression)):
            try:
                print(json.dumps({"selector": selector, "samples": query(selector),
                                  "sample_age_seconds": query("time() - timestamp(" + selector + ")")}), file=sys.stderr)
            except Exception as error:
                print(f"Panel component diagnostic failed for {selector}: {error}", file=sys.stderr)

    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            dockerfile = Path(temporary) / "Dockerfile"
            dockerfile.write_text("FROM python:3.12-alpine\nCOPY . /opt/sdk\nRUN pip install --no-cache-dir /opt/sdk\n")
            docker("build", *platform, "-t", images["probe"], "-f", str(dockerfile), str(ROOT / "sdk/python"), timeout=600)
        print("built SDK-installed probe", flush=True)
        for name in ("collector", "grafana"):
            docker("build", *platform, "-t", images[name], "-f", str(ROOT / "observe" / compose["services"][name]["build"]["dockerfile"]),
                   str(ROOT / "observe"), timeout=600)
            print(f"built {name}", flush=True)
        for volume in volumes.values():
            docker("volume", "create", volume)
        docker("run", "-d", "--name", names["probe"], *platform, "--network", "none", "--memory", "192m",
               "-v", f"{volumes['extensions']}:/extensions", "-v", f"{volumes['victoria']}:/storage",
               "-e", "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319",
               "-e", "OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20" + protocols.MESH,
               "-e", "OTEL_TRACES_EXPORTER=none", "-e", "OTEL_METRICS_EXPORTER=otlp",
               images["probe"], "python", "-c", "from pathlib import Path; import time; Path('/storage').chmod(0o777); time.sleep(3600)")
        for name in services:
            service = compose["services"][name]
            resources = protocols._resource_values(service["deploy"]["resources"])
            environment = dict(service.get("environment", {}))
            if name == "collector":
                environment.update(LUMA_OTLP_MESH_BIND="127.0.0.1", LUMA_OTLP_TOKEN=protocols.MESH)
            env = [item for key, content in environment.items() for item in ("-e", f"{key}={content}")]
            mounts = ["-v", f"{volumes['victoria']}:/data"] if name == "victoria" else ["-v", f"{volumes['extensions']}:/etc/luma-observe/extensions:ro"] if name == "grafana" else []
            docker("run", "-d", "--name", names[name], *platform, "--network", "container:" + names["probe"],
                   "--memory", f"{resources['MemoryMaxMB']}m", *env, *mounts,
                   images.get(name, service["image"]), *service.get("command", []), timeout=180)
        for port, path, name in ((8428, "/health", "VictoriaMetrics"), (8888, "/metrics", "Collector"), (3100, "/api/health", "Grafana")):
            wait_for(name + " ready", lambda p=port, u=path: request(p, u)["status"] == 200)
        print(docker("exec", names["probe"], "python", "-c", PRODUCE, timeout=60), flush=True)
        for expression, expected in (
            ('sum(task_executions_total{service_name="example-worker",outcome="failed"})', 3),
            ('sum(task_duration_seconds_count{service_name="example-worker"})', 3),
            ('sum(task_queue_wait_seconds_count{service_name="example-worker"})', 3),
            ('sum(document_processed_total{service_name="example-worker",document_format="pdf",outcome="failed"})', 3),
        ):
            wait_for(f"SDK metric verified: {expression}", lambda expr=expression, target=expected: value(expr) == target)
        docker("exec", "-d", names["probe"], "python", "-c", ACTIVE_PRODUCE)
        wait_for("SDK active-task fixture ready", lambda: fixture_marker("ready"), timeout=30)
        for _ in range(3):
            docker("exec", names["probe"], "python", "-c", CHURN_PRODUCE, timeout=30)
        wait_for("Three fresh SDK processes exported 48 same-name attempts",
                 lambda: value('sum(task_executions_total{service_name="sdk-churn-worker"})') == 48)
        response = request(8428, "/api/v1/series?" + urllib.parse.urlencode({"match[]": '{service_name="sdk-churn-worker"}'}))
        assert response["status"] == 200, f"series inventory request failed: {response}"
        inventory = json.loads(response["body"])
        assert inventory.get("status") == "success", f"series inventory failed: {inventory}"
        series = inventory["data"]
        per_instance = Counter(item.get("service_instance_id", "") for item in series)
        assert len(per_instance) == 3 and "" not in per_instance, "each SDK process must retain its unique instance identity"
        print("Isolated SDK series footprint (3 processes x 16 same-name attempts; not a production throughput benchmark): "
              + json.dumps({"total_series": len(series), "series_by_instance": dict(sorted(per_instance.items())),
                            "series_by_metric": dict(sorted(Counter(item["__name__"] for item in series).items()))}, sort_keys=True), flush=True)
        compiled = {}
        for uid in ("application-tasks", "application-processing"):
            wait_for(f"Generated dashboard provisioned: {uid}", lambda item=uid: request(3100, "/api/dashboards/uid/" + item)["status"] == 200)
            compiled[uid] = json.loads(request(3100, "/api/dashboards/uid/" + uid)["body"])["dashboard"]
        replacements = {"${service:regex}": "example-worker", "$service": "example-worker", "${environment:regex}": "protocol",
                        "${task:regex}": "example.fail|example.running", "${format:regex}": "pdf", "$__rate_interval": "5m", "$__range": "5m"}
        count = 0
        oldest_expression = None
        for dashboard in compiled.values():
            dashboard_replacements = dict(replacements)
            for variable in dashboard["templating"]["list"]:
                if variable["type"] == "constant":
                    dashboard_replacements["${" + variable["name"] + "}"] = variable["query"]
            for panel in dashboard["panels"]:
                for target in panel["targets"]:
                    expression = target["expr"]
                    for variable, replacement in dashboard_replacements.items():
                        expression = expression.replace(variable, replacement)
                    rows = query(expression)
                    if not rows:
                        diagnose_empty_panel(expression)
                        raise AssertionError(f"synthetic fixture must populate panel target: {panel['title']} / {target['refId']}")
                    if panel["title"] == "Framework success ratio":
                        assert value(expression) == 0, "failure-only attempts must show a 0% success ratio"
                        assert query(expression.replace(replacements["${task:regex}"], "missing-task")) == [], "missing execution denominator must retain no-data semantics"
                    if panel["title"] == "Oldest active attempt age by task and queue":
                        oldest_expression = expression
                        assert {row["metric"]["task_name"] for row in rows} == {"example.running"}, "age rows must identify the selected active task"
                        assert all(row["metric"]["task_queue"] == "example" and row["metric"]["task_framework"] == "manual" for row in rows)
                        assert query('task_oldest_active_age_seconds{service_name="example-worker",task_name="example.completed"}'), "fixture must retain a prior completed-task age sample in backend lookback"
                        assert query(expression.replace(replacements["${task:regex}"], "example.completed")) == [], "a completed task must not remain active while another task in its process runs"
                    count += 1
        assert oldest_expression is not None
        docker("exec", names["probe"], "python", "-c", "from pathlib import Path; Path('/tmp/sdk-active-stop').touch()")
        wait_for("SDK active-task fixture stopped", lambda: fixture_marker("stopped"), timeout=30)
        wait_for("Completed task reported zero active attempts",
                 lambda: value('sum(task_active{service_name="example-worker",task_name="example.running"})') == 0)
        assert query(oldest_expression) == [], "idle must not retain an old active-age row or synthesize zero age"
        print(f"SDK OTLP, generated dashboard provisioning and {count} PromQL expressions passed", flush=True)
    except BaseException:
        result = subprocess.run(["docker", "exec", names["probe"], "python", "-c",
                                 "from pathlib import Path; p=Path('/tmp/sdk-active-producer.log'); print(p.read_text() if p.exists() else '')"],
                                capture_output=True, text=True, timeout=15)
        if result.stdout.strip():
            print("SDK active-task fixture: " + result.stdout[-2500:], file=sys.stderr)
        for name in names.values():
            result = subprocess.run(["docker", "logs", "--tail", "15", name], capture_output=True, text=True, timeout=15)
            print(name, (result.stdout + result.stderr)[-2500:], file=sys.stderr)
        raise
    finally:
        for name in reversed(list(names.values())):
            subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=30)
        for volume in volumes.values():
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=30)
        for image in [*images.values(), *(shared_images - existing_images)]:
            subprocess.run(["docker", "image", "rm", image], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
