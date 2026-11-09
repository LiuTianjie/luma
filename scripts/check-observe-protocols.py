#!/usr/bin/env python3
"""Exercise standard observation protocols and config loading in disposable containers.

Uses the packaged Collector, vmauth, VictoriaMetrics, VictoriaLogs, Tempo,
Grafana and vmalert. Synthetic data stays in an isolated network namespace;
no host ports, deployment credentials or application services are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luma.nomad_render import _resource_values

HTTP = """
import json, sys, urllib.error, urllib.request
payload = json.load(sys.stdin)
request = urllib.request.Request(payload['url'], method=payload['method'],
    data=payload['body'].encode() if payload['body'] is not None else None,
    headers=payload['headers'])
try:
    with urllib.request.urlopen(request, timeout=8) as response:
        print(json.dumps({'status': response.status, 'body': response.read().decode()}))
except urllib.error.HTTPError as error:
    print(json.dumps({'status': error.code, 'body': error.read().decode()}))
except urllib.error.URLError as error:
    print(json.dumps({'status': 0, 'body': str(error)}))
"""

# These credentials are synthetic, scoped to a --network none test namespace.
READER = "protocol-reader-" + "r" * 40
WRITER = "protocol-writer-" + "w" * 40
MESH = "protocol-mesh-" + "m" * 40
SERVICE = "luma-observe-protocol-check"


def docker(*args: str, timeout: float = 60, stdin: str | None = None) -> str:
    result = subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {(result.stdout + result.stderr)[-4000:]}")
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", help="Docker platform; defaults to the Docker host architecture")
    args = parser.parse_args()
    platform = ["--platform", args.platform] if args.platform else []
    prefix = "luma-protocol-check-" + uuid.uuid4().hex[:12]
    compose = yaml.safe_load((ROOT / "observe/docker-compose.yml").read_text())
    services = ["victoria", "victoria-logs", "tempo", "collector", "observe-access", "vmalert", "grafana"]
    names = {name: prefix + "-" + name for name in ["probe", *services]}
    images = {name: prefix + "-" + name + ":local" for name in services if "build" in compose["services"][name]}
    volumes = {name: prefix + "-" + name for name in ("victoria", "victoria-logs", "tempo", "extensions")}
    helper_image = "python:3.12-alpine"
    existing_images = set(docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").splitlines())

    def request(port: int, path: str, body: str | None = None, *, token: str = "", content_type: str = "application/json") -> dict:
        headers = {"Content-Type": content_type, "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        payload = {"url": f"http://127.0.0.1:{port}{path}", "method": "POST" if body is not None else "GET",
                   "body": body, "headers": headers}
        return json.loads(docker("exec", "-i", names["probe"], "python", "-c", HTTP, stdin=json.dumps(payload)))

    def expect(response: dict, codes=(200,)) -> dict:
        if response["status"] not in codes:
            raise RuntimeError(f"expected HTTP {codes}, got {response}")
        return response

    def wait_for(description: str, check, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                print(description, flush=True)
                return
            time.sleep(2)
        raise RuntimeError(f"timed out: {description}")

    def start(name: str, *, enabled: bool = False) -> None:
        service = compose["services"][name]
        resources = _resource_values(service["deploy"]["resources"])
        # Secret references such as ${LUMA_OBSERVE_READ_TOKEN} are rendered by
        # Luma Control in production; here they start unset, like a closed install.
        environment = {key: "" if re.fullmatch(r"\$\{[A-Z0-9_]+\}", str(value)) else value
                       for key, value in service.get("environment", {}).items()}
        if name == "collector":
            environment.update(LUMA_OTLP_MESH_BIND="127.0.0.1", LUMA_OTLP_TOKEN=MESH)
        if name == "observe-access" and enabled:
            environment.update(LUMA_OBSERVE_READ_TOKEN=READER, LUMA_OBSERVE_INGEST_TOKEN=WRITER)
        env = [item for key, value in environment.items() for item in ("-e", f"{key}={value}")]
        mounts = []
        if name in ("victoria", "victoria-logs", "tempo"):
            mounts = ["-v", f"{volumes[name]}:/data"]
        if name in ("grafana", "vmalert"):
            mounts = ["-v", f"{volumes['extensions']}:/etc/luma-observe/extensions:ro"]
        command = list(service.get("command", []))
        if name == "vmalert":
            # Rule evaluation and reload use the production config; notification
            # delivery is explicitly disabled in this isolated protocol check.
            command = [arg for arg in command if not arg.startswith("--notifier.url=")]
            command.append("--notifier.blackhole")
        docker("run", "-d", "--name", names[name], *platform, "--network", "container:" + names["probe"],
               "--memory", str(resources["MemoryMaxMB"]) + "m",
               "--memory-reservation", str(resources["MemoryMB"]) + "m",
               *env, *mounts, images.get(name, service["image"]), *command, timeout=180)

    def metric(expression: str) -> float | None:
        response = request(8427, "/metrics/api/v1/query?" + urllib.parse.urlencode({"query": expression}), token=READER)
        if response["status"] != 200:
            return None
        rows = json.loads(response["body"]).get("data", {}).get("result", [])
        return float(rows[0]["value"][1]) if rows else None

    def metric_rows(expression: str) -> list[dict]:
        response = expect(request(8427, "/metrics/api/v1/query?" + urllib.parse.urlencode({"query": expression}), token=READER))
        payload = json.loads(response["body"])
        if payload.get("status") != "success":
            raise RuntimeError(f"metrics query failed: {payload}")
        return payload["data"]["result"]

    def sampling_decisions() -> int:
        # Read directly instead of waiting for the next diagnostic scrape. This
        # proves the early children's decisions happened before sending roots.
        prefix = "otelcol_processor_tail_sampling_global_count_traces_sampled"
        lines = expect(request(8888, "/metrics"))["body"].splitlines()
        return int(sum(float(line.rsplit(" ", 1)[1]) for line in lines
                       if line.split("{", 1)[0].split(" ", 1)[0] in {prefix, prefix + "_total"}))

    def write_extensions(version: int) -> None:
        dashboard = {"uid": "luma-protocol-check", "title": f"Protocol extension {version}", "schemaVersion": 39,
                     "version": version, "panels": [], "timezone": "browser"}
        rules = {"groups": [{"name": "luma-protocol-extension", "interval": "5s", "rules": [
            {"record": "luma_protocol_extension_ready", "expr": f"vector({version})"}]}]}
        files = {"dashboards/protocol.json": json.dumps(dashboard), "rules/protocol.yml": yaml.safe_dump(rules)}
        code = """
import json, sys
from pathlib import Path
for name, content in json.load(sys.stdin).items():
    path = Path('/extensions') / name
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(content)
    temporary.replace(path)
"""
        docker("exec", "-i", names["probe"], "python", "-c", code, stdin=json.dumps(files))

    def dashboard_loaded(version: int) -> bool:
        response = request(3100, "/api/dashboards/uid/luma-protocol-check")
        return response["status"] == 200 and json.loads(response["body"])["dashboard"]["title"] == f"Protocol extension {version}"

    try:
        for name, image in images.items():
            dockerfile = compose["services"][name]["build"]["dockerfile"]
            docker("build", *platform, "-t", image, "-f", str(ROOT / "observe" / dockerfile),
                   str(ROOT / "observe"), timeout=600)
            print(f"built {name}", flush=True)
        for volume in volumes.values():
            docker("volume", "create", volume)
        mounts = [item for name, volume in volumes.items() for item in ("-v", f"{volume}:/storage/{name}")]
        docker("run", "-d", "--name", names["probe"], *platform, "--network", "none", "--memory", "96m",
               *mounts, "-v", f"{volumes['extensions']}:/extensions", helper_image,
               "python", "-c", "from pathlib import Path; import time; [p.chmod(0o777) for p in Path('/storage').iterdir()]; time.sleep(3600)", timeout=180)
        for name in services:
            start(name)
        for port, path, description in ((8428, "/health", "VictoriaMetrics"), (9428, "/health", "VictoriaLogs"),
                                         (3200, "/ready", "Tempo"), (8429, "/metrics", "vmauth"),
                                         (8880, "/api/v1/rules", "vmalert with empty extensions"),
                                         (3100, "/api/health", "Grafana with empty extensions")):
            wait_for(description + " ready", lambda p=port, u=path: request(p, u)["status"] == 200)
        wait_for("Collector OTLP ready", lambda: request(4318, "/v1/traces", '{"resourceSpans":[]}')["status"] == 200)
        expect(request(8427, "/metrics/api/v1/query?query=1"), (401,))
        expect(request(8427, "/metrics/api/v1/query?query=1", token=READER), (401,))
        print("Default gateway has no authorized users", flush=True)
        docker("rm", "-f", names["observe-access"])
        start("observe-access", enabled=True)
        wait_for("Configured query role ready", lambda: request(8427, "/metrics/api/v1/query?query=1", token=READER)["status"] == 200)
        for token in ("", "invalid-credential"):
            expect(request(8427, "/metrics/api/v1/query?query=1", token=token), (401,))
        for path in ("/metrics/api/v1/admin/tsdb/delete_series", "/metrics/api/v1/write", "/logs/insert/jsonline",
                     "/traces/flush", "/otlp/v1/metrics", "/otlp/v1/traces", "/-/reload", "/metrics", "/debug/pprof/"):
            expect(request(8427, path, "{}", token=READER), (400, 403, 404))
        expect(request(8427, "/metrics/api/v1/query?query=1", token=WRITER), (400, 403, 404))
        expect(request(4319, "/v1/metrics", '{"resourceMetrics":[]}'), (401,))
        print("Read/write roles, mesh auth and admin path isolation verified", flush=True)
        write_extensions(1)

        end = time.time_ns()
        attributes = [{"key": "service.name", "value": {"stringValue": SERVICE}}]
        def encode_attributes(values: dict[str, str | int] | None) -> list[dict]:
            if values is None:
                return attributes
            return [{"key": key, "value": {"intValue": str(value)} if isinstance(value, int) else {"stringValue": value}}
                    for key, value in values.items()]

        def send_spans(rows: list[dict], resource: dict[str, str | int] | None = None) -> None:
            payload = json.dumps({"resourceSpans": [{"resource": {"attributes": encode_attributes(resource)}, "scopeSpans": [
                {"scope": {"name": "luma.protocol.check"}, "spans": rows}]}]})
            response = expect(request(8427, "/otlp/v1/traces", payload, token=WRITER))
            if json.loads(response["body"]).get("partialSuccess", {}).get("rejectedSpans", 0) not in (0, "0"):
                raise RuntimeError("Collector rejected synthetic spans")

        def metric_payload(name: str, value: int, resource: dict[str, str | int] | None = None) -> str:
            recorded = time.time_ns()
            return json.dumps({"resourceMetrics": [{"resource": {"attributes": encode_attributes(resource)}, "scopeMetrics": [{
                "scope": {"name": "luma.protocol.check"}, "metrics": [{"name": name,
                "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": [{
                    "startTimeUnixNano": str(recorded - 1_000_000_000), "timeUnixNano": str(recorded), "asInt": str(value)}]}}]}]}]})
        expect(request(8427, "/otlp/v1/metrics", metric_payload("luma_protocol_native_events", 100), token=WRITER))
        expect(request(4319, "/v1/metrics", metric_payload("luma_protocol_mesh_events", 7), token=MESH))
        spans = []
        for index in range(102):
            trace_id = hashlib.sha256(f"luma-protocol-trace-{index}".encode()).hexdigest()[:32]
            name = "fast-success" if index < 100 else ("slow-success" if index == 100 else "fast-error")
            spans.append({"traceId": trace_id, "spanId": uuid.uuid4().hex[:16], "name": name, "kind": 1,
                          "startTimeUnixNano": str(end - (2_000_000_000 if index == 100 else 10_000_000)),
                          "endTimeUnixNano": str(end), "status": {"code": 2 if index == 101 else 1}})
            if index < 100:
                # Nested manual task spans can finish before their execution
                # root. A task-shaped CONSUMER with a parent is still a child.
                spans[-1].update(kind=5, parentSpanId=uuid.uuid4().hex[:16], attributes=[
                    {"key": "task.framework", "value": {"stringValue": "manual"}},
                    {"key": "task.name", "value": {"stringValue": "nested-task"}},
                ])
        send_spans(spans)
        expect(request(9428, "/insert/jsonline?_stream_fields=service&_msg_field=_msg", json.dumps({
            "service": SERVICE, "_msg": "protocol-check", "trace_id": spans[101]["traceId"]}) + "\n", content_type="application/stream+json"))

        wait_for("Native Metrics: 100 events available through read gateway", lambda: metric(f'sum(luma_protocol_native_events_total{{service_name="{SERVICE}"}})') == 100)
        wait_for("Mesh OTLP Metrics: 7 events available", lambda: metric(f'sum(luma_protocol_mesh_events_total{{service_name="{SERVICE}"}})') == 7)
        wait_for("Span metrics count all 100 fast successes before sampling", lambda: metric(f'sum(traces_span_metrics_calls_total{{service_name="{SERVICE}",span_name="fast-success"}})') == 100)
        range_params = urllib.parse.urlencode({"query": "luma_protocol_native_events_total", "start": end // 1_000_000_000 - 60,
                                              "end": int(time.time()), "step": 15})
        expect(request(8427, "/metrics/api/v1/query_range?" + range_params, token=READER))
        log_params = urllib.parse.urlencode({"query": f'service:"{SERVICE}"', "limit": 5})
        wait_for("LogsQL returns ingested event with trace correlation", lambda: spans[101]["traceId"] in request(8427, "/logs/select/logsql/query?" + log_params, token=READER)["body"])
        for span in spans[100:]:
            wait_for(span["name"] + " survives sampling and is queryable by trace ID", lambda s=span: s["name"] in request(8427, "/traces/api/traces/" + s["traceId"], token=READER)["body"])
        wait_for("Early child sampling decisions completed", lambda: sampling_decisions() >= len(spans))
        trace_params = urllib.parse.urlencode({"q": f'{{ resource.service.name = "{SERVICE}" && name = "fast-success" }}', "limit": 200})
        response = expect(request(8427, "/traces/api/search?" + trace_params, token=READER))
        retained_traces = json.loads(response["body"]).get("traces", [])
        retained = len(retained_traces)
        if not 0 < retained < 100:
            raise RuntimeError(f"expected a sampled subset of 100 fast successes, got {retained}")
        print(f"Tempo retained {retained}/100 fast successes; native and span metrics still count 100", flush=True)

        # Tempo's Search JSON may omit leading zeroes from a trace ID.
        retained_ids = {row["traceID"].lower().zfill(32) for row in retained_traces}
        early_child = next(span for span in spans[:100] if span["traceId"] not in retained_ids)
        expect(request(8427, "/traces/api/traces/" + early_child["traceId"], token=READER), (404,))
        completed = time.time_ns()
        task_roots = []
        for index in range(102):
            name = "task-root-fast-success" if index < 100 else ("task-root-slow-success" if index == 100 else "task-root-fast-error")
            task_roots.append({
                "traceId": hashlib.sha256(f"luma-protocol-task-root-{index}".encode()).hexdigest()[:32],
                "spanId": uuid.uuid4().hex[:16], "name": name, "kind": 5,
                "startTimeUnixNano": str(completed - (2_000_000_000 if index == 100 else 10_000_000)),
                "endTimeUnixNano": str(completed), "status": {"code": 2 if index == 101 else 1},
                "attributes": [
                    {"key": "task.framework", "value": {"stringValue": "taskiq"}},
                    {"key": "task.name", "value": {"stringValue": name}},
                ],
            })
        # Reuse a trace whose early child was already rejected. The long failed
        # execution root must get an independent decision using its final result.
        task_roots.append({**task_roots[-1], "name": "task-root-late-error",
                           "traceId": early_child["traceId"], "spanId": early_child["parentSpanId"],
                           "startTimeUnixNano": str(end - 1_000_000_000), "attributes": [
                               {"key": "task.framework", "value": {"stringValue": "celery"}},
                               {"key": "task.name", "value": {"stringValue": "task-root-late-error"}},
                           ]})
        send_spans(task_roots)
        for span in task_roots[100:]:
            wait_for(span["name"] + " retained from completed outcome/duration", lambda s=span: s["name"] in request(8427, "/traces/api/traces/" + s["traceId"], token=READER)["body"])
        wait_for("Task root sampling decisions completed", lambda: sampling_decisions() >= len(spans) + len(task_roots))
        # Pinned contrib v0.122.0 uses FNV-1a(default-hash-seed + trace ID).
        # These seven fixed fixture IDs fall below its 10% threshold:
        # processor/tailsamplingprocessor/internal/sampling/probabilistic.go
        expected_roots = {task_roots[index]["traceId"] for index in (6, 24, 33, 38, 48, 72, 76)}
        wait_for("Deterministically sampled fast task root readable by trace ID", lambda:
                 "task-root-fast-success" in request(8427, "/traces/api/traces/" + task_roots[6]["traceId"], token=READER)["body"])
        root_query = urllib.parse.urlencode({"q": f'{{ resource.service.name = "{SERVICE}" && name = "task-root-fast-success" }}', "limit": 200})
        def task_roots_searchable() -> bool:
            # Tempo's trace-ID read includes live traces; search waits until
            # trace_idle_period/sweeps move them to a searchable head/WAL block.
            root_response = expect(request(8427, "/traces/api/search?" + root_query, token=READER))
            actual = {row["traceID"].lower().zfill(32) for row in json.loads(root_response["body"]).get("traces", [])}
            if not actual <= expected_roots:
                raise RuntimeError(f"unexpected fast task roots bypassed baseline sampling: {sorted(actual - expected_roots)}")
            return actual == expected_roots

        wait_for("All seven deterministically sampled fast task roots searchable", task_roots_searchable, timeout=60)
        retained_roots = len(expected_roots)
        wait_for("Span metrics count each accepted task root exactly once", lambda: metric(
            f'sum(traces_span_metrics_calls_total{{service_name="{SERVICE}",span_name=~"task-root-.*"}})') == len(task_roots))
        late_trace = expect(request(8427, "/traces/api/traces/" + early_child["traceId"], token=READER))["body"]
        if '"fast-success"' in late_trace:
            raise RuntimeError("already rejected child unexpectedly appeared with the late task root")
        print(f"Completed task roots sample based on outcome/duration ({retained_roots}/100 fast roots retained); children are best effort", flush=True)

        # Default and opted-in sources deliberately share all six service keys.
        # One default source has no instance metadata at all: the service-mode
        # label must keep its cumulative stream separate during an opt-in rollout.
        stable_resource = {
            "service.name": "luma-span-routing-check", "service.namespace": "protocol-apps",
            "deployment.environment.name": "protocol", "luma.stack": "protocol-stack",
            "luma.task": "worker", "luma.region": "protocol-region",
        }
        task_fields = {"task.name": "example.execute", "task.queue": "primary", "task.framework": "celery", "task.outcome": "succeeded"}
        resource_keys, task_keys = tuple(stable_resource), tuple(task_fields)
        resource_labels = tuple(key.replace(".", "_") for key in resource_keys)
        task_labels = resource_labels + tuple(key.replace(".", "_") for key in task_keys)

        def process_resource(identity: str, *, service: bool) -> dict:
            resource = {**stable_resource, "service.instance.id": identity, "host.name": identity + "-host",
                        "process.pid": 4100 + len(identity), "k8s.pod.uid": identity + "-pod",
                        "telemetry.sdk.version": identity + "-sdk", "application.release": identity + "-release"}
            if service:
                resource["luma.observe.span_metrics"] = "service"
            return resource

        def execution_span(fields: dict, *, name: str = "protocol.task", kind: int = 5) -> dict:
            recorded = time.time_ns()
            return {"traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16], "name": name, "kind": kind,
                    "startTimeUnixNano": str(recorded - 250_000_000), "endTimeUnixNano": str(recorded),
                    "status": {"code": 2 if fields["task.outcome"] in {"failed", "retrying"} else 1},
                    "attributes": encode_attributes(fields)}

        def task_key(resource: dict, fields: dict) -> tuple:
            return tuple(resource[key] for key in resource_keys) + tuple(fields[key] for key in task_keys)

        defaults = [process_resource(f"default-instance-{index}", service=False) for index in range(2)]
        defaults[1]["luma.observe.span_metrics"] = "instance"  # Unknown modes retain the default behavior.
        defaults.append(dict(stable_resource))
        trace_resources = []
        for resource in defaults:
            root = execution_span({**task_fields, "task.outcome": "failed"})
            child = execution_span(task_fields, name="protocol.child")
            child.update(traceId=root["traceId"], parentSpanId=root["spanId"])
            send_spans([root, child], resource)
            if "service.instance.id" in resource:
                trace_resources.append((root, resource))

        opted_in = [process_resource(f"service-instance-{index}", service=True) for index in range(3)]
        expected_tasks = {task_key(stable_resource, task_fields): 6.0,
                          task_key(stable_resource, {**task_fields, "task.outcome": "failed"}): 3.0}
        for resource in opted_in:
            roots = [execution_span({**task_fields, "task.outcome": outcome}) for outcome in ("succeeded", "succeeded", "failed")]
            child = execution_span(task_fields, name="protocol.child")
            child.update(traceId=roots[-1]["traceId"], parentSpanId=roots[-1]["spanId"])
            # A task-shaped span without a parent still is not an execution root
            # unless its kind is CONSUMER. Neither this helper nor the child counts.
            helper = execution_span(task_fields, name="protocol.helper", kind=1)
            send_spans([*roots, child, helper], resource)
            trace_resources.append((roots[-1], resource))
            expect(request(4319, "/v1/metrics", metric_payload("luma_protocol_instance_events", 1, resource), token=MESH))

        # Keep span.name fixed while changing each task dimension, so a missing
        # dimension cannot be accidentally rescued by a different span name.
        for key, value in {"task.name": "example.other", "task.queue": "secondary", "task.framework": "taskiq", "task.outcome": "retrying"}.items():
            fields = {**task_fields, key: value}
            resource = process_resource("dimension-" + key, service=True)
            send_spans([execution_span(fields)], resource)
            expected_tasks[task_key(resource, fields)] = 1.0
        expected_service_roots = {tuple(stable_resource.values()): 13.0}
        for key in resource_keys:
            resource = process_resource("boundary-" + key, service=True)
            resource[key] = stable_resource[key] + "-other"
            send_spans([execution_span(task_fields)], resource)
            expected_tasks[task_key(resource, task_fields)] = 1.0
            expected_service_roots[tuple(resource[key] for key in resource_keys)] = 1.0

        task_selector = '{luma_observe_span_metrics="service",span_name="protocol.task"}'
        task_calls = "task_span_calls_total" + task_selector
        service_calls = "traces_span_metrics_calls_total" + task_selector
        wait_for("Opt-in task metrics received all 19 execution roots", lambda: metric(f"sum({task_calls})") == 19)
        wait_for("Opt-in RED metrics count each execution root once", lambda: metric(f"sum({service_calls})") == 19)
        for expression in (task_calls, "task_span_duration_seconds_count" + task_selector):
            rows = metric_rows(expression)
            actual = {tuple(row["metric"].get(key) for key in task_labels): float(row["value"][1]) for row in rows}
            if len(rows) != len(expected_tasks) or actual != expected_tasks:
                raise RuntimeError(f"task dimensions, service boundaries or counts differ: {expression}: {rows}")
        service_rows = metric_rows(service_calls)
        service_totals = {}
        for row in service_rows:
            key = tuple(row["metric"].get(label) for label in resource_labels)
            service_totals[key] = service_totals.get(key, 0) + float(row["value"][1])
        if len(service_rows) != 8 or service_totals != expected_service_roots:
            raise RuntimeError(f"service span metrics did not preserve the six resource boundaries: {service_rows}")
        for child_name in ("protocol.child", "protocol.helper"):
            expression = 'traces_span_metrics_calls_total{luma_observe_span_metrics="service",span_name=' + json.dumps(child_name) + '}'
            rows = metric_rows(expression)
            if len(rows) != 1 or float(rows[0]["value"][1]) != 3:
                raise RuntimeError(f"opt-in RED connector must count children/helpers exactly once: {rows}")
        if metric_rows('task_span_calls_total{span_name=~"protocol.child|protocol.helper"}') or metric_rows('task_span_calls_total{luma_observe_span_metrics!="service"}'):
            raise RuntimeError("task metrics included a child/helper or an application that did not opt in")

        base_labels = {**dict(zip(resource_labels, stable_resource.values())),
                       **{key.replace(".", "_"): value for key, value in task_fields.items()}, "luma_observe_span_metrics": "service"}
        base_selector = ",".join(f"{key}={json.dumps(value)}" for key, value in base_labels.items())
        buckets = metric_rows("task_span_duration_seconds_bucket{" + base_selector + "}")
        if not buckets or len({row["metric"]["le"] for row in buckets}) != len(buckets):
            raise RuntimeError(f"expected one task duration bucket set across the three processes: {buckets}")
        if {float(row["value"][1]) for row in buckets if row["metric"]["le"] == "+Inf"} != {6.0} or metric("task_span_duration_seconds_sum{" + base_selector + "}") != 1.5:
            raise RuntimeError("task duration histogram must aggregate six 250ms successes in seconds")
        generated = metric_rows('{__name__=~"(traces_span_metrics|task_span)_.*",luma_observe_span_metrics="service"}')
        transient_labels = {"service_instance_id", "instance", "process_pid", "host_name", "k8s_pod_uid", "telemetry_sdk_version", "application_release"}
        if any(transient_labels.intersection(row["metric"]) for row in generated):
            raise RuntimeError(f"opt-in derived metrics retained a process-specific resource label: {generated}")

        for span_name in ("protocol.task", "protocol.child"):
            rows = metric_rows('traces_span_metrics_calls_total{luma_observe_span_metrics!="service",span_name=' + json.dumps(span_name) + '}')
            actual = {row["metric"].get("service_instance_id", ""): float(row["value"][1]) for row in rows}
            if len(rows) != 3 or actual != {"default-instance-0": 1.0, "default-instance-1": 1.0, "": 1.0}:
                raise RuntimeError(f"default sources changed or collided with opt-in service metrics: {rows}")
            for resource in defaults[:2]:
                row = next(row for row in rows if row["metric"].get("service_instance_id") == resource["service.instance.id"])
                if row["metric"].get("host_name") != resource["host.name"]:
                    raise RuntimeError(f"default span metrics lost original resource attributes: {row}")
        wait_for("Native OTLP instance counters received", lambda: metric("sum(luma_protocol_instance_events_total)") == 3)
        native_rows = metric_rows("luma_protocol_instance_events_total")
        native_by_instance = {row["metric"].get("service_instance_id"): row for row in native_rows}
        if len(native_rows) != 3 or set(native_by_instance) != {resource["service.instance.id"] for resource in opted_in}:
            raise RuntimeError(f"native metric process identities were merged or removed: {native_rows}")
        for resource in opted_in:
            row = native_by_instance[resource["service.instance.id"]]
            if float(row["value"][1]) != 1 or row["metric"].get("host_name") != resource["host.name"]:
                raise RuntimeError(f"native metric resource metadata changed: {row}")

        def trace_resource_preserved(span: dict, resource: dict) -> bool:
            response = request(8427, "/traces/api/traces/" + span["traceId"], token=READER)
            return response["status"] == 200 and all(value in response["body"] for value in (resource["service.instance.id"], resource["host.name"]))

        for span, resource in trace_resources:
            wait_for("Stored trace preserves " + resource["service.instance.id"], lambda s=span, r=resource: trace_resource_preserved(s, r))
        print("Opt-in span metrics aggregate three processes and 19 task roots across task/service boundaries; default sources, native metrics and traces preserve their resources", flush=True)

        metrics = expect(request(8888, "/metrics"))["body"]
        if "otelcol_receiver_accepted_spans" not in metrics:
            raise RuntimeError("Collector internal acceptance metrics unavailable")
        wait_for("External dashboard loaded", lambda: dashboard_loaded(1))
        wait_for("External recording rule loaded and evaluated", lambda: metric("luma_protocol_extension_ready") == 1)
        write_extensions(2)
        wait_for("External dashboard hot-reloaded", lambda: dashboard_loaded(2))
        wait_for("External rule hot-reloaded and evaluated", lambda: metric("luma_protocol_extension_ready") == 2)
        print("Observe standard protocols check passed", flush=True)
    except BaseException:
        for name in names.values():
            result = subprocess.run(["docker", "logs", "--tail", "20", name], capture_output=True, text=True, timeout=15)
            print(name, (result.stdout + result.stderr)[-4000:], file=sys.stderr)
        raise
    finally:
        for name in reversed(list(names.values())):
            subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=30)
        for volume in volumes.values():
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=30)
        cleanup_images = list(images.values()) + ([] if helper_image in existing_images else [helper_image])
        for image in cleanup_images:
            subprocess.run(["docker", "image", "rm", image], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
