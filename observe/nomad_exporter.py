#!/usr/bin/env python3
"""Export Nomad job/allocation health as Prometheus gauges.

Reads the local Nomad HTTP API. Intended to run on the manager host network
so it can use 127.0.0.1:4646 without exposing Nomad publicly.

Job names are Luma apps. Traefik router names are mapped to those apps so
Grafana can aggregate HTTP RED by application instead of raw router ids.
Failed gauges count currently desired-run failures, not Nomad's lifetime
JobSummary.Failed counter.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

ROUTES_DIR = Path(os.environ.get("LUMA_ROUTES_DIR", "/opt/luma/routes"))


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**values: Any) -> str:
    return "{" + ",".join(f'{key}="{_escape(val)}"' for key, val in values.items()) + "}"


def fetch_json(addr: str, path: str, token: str, timeout: float = 4.0) -> Any:
    url = addr.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Nomad-Token"] = token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def app_id(job: dict[str, Any]) -> str:
    return str(job.get("Name") or job.get("ID") or "").strip()


def compose_job(job: dict[str, Any]) -> bool:
    meta = job.get("Meta") if isinstance(job.get("Meta"), dict) else {}
    return str(meta.get("luma.compose") or "").lower() == "true"


def job_active(job: dict[str, Any]) -> bool:
    if bool(job.get("Stop")):
        return False
    return str(job.get("Status") or "").lower() == "running"


def file_route_stems(routes_dir: Path = ROUTES_DIR) -> list[str]:
    try:
        names = sorted(path.stem for path in routes_dir.glob("*.yml") if path.is_file())
    except OSError:
        return []
    return [name for name in names if name and not name.startswith(".")]


def tasks_by_job(allocations: Iterable[Any]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for alloc in allocations:
        if not isinstance(alloc, dict):
            continue
        job = str(alloc.get("JobID") or "")
        if not job:
            continue
        states = alloc.get("TaskStates") if isinstance(alloc.get("TaskStates"), dict) else {}
        grouped.setdefault(job, set()).update(str(name) for name in states)
    return grouped


def router_mappings(jobs: Iterable[Any], allocations: Iterable[Any], route_stems: Iterable[str] | None = None) -> list[dict[str, str]]:
    """Map Traefik router labels onto Luma app names."""
    tasks = tasks_by_job(allocations)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(app: str, service: str, router: str) -> None:
        app, service, router = app.strip(), service.strip(), router.strip()
        key = (app, service, router)
        if not app or not router or key in seen:
            return
        seen.add(key)
        rows.append({"app": app, "service": service or app, "router": router})

    for job in jobs:
        if not isinstance(job, dict):
            continue
        app = app_id(job)
        if not app:
            continue
        add(app, app, f"{app}@nomad")
        add(app, app, f"{app}@file")
        if compose_job(job):
            for task in sorted(tasks.get(app, ())):
                add(app, task, f"{app}-{task}@nomad")
    for stem in route_stems if route_stems is not None else file_route_stems():
        app = "luma-observe" if str(stem).startswith("luma-observe") else str(stem)
        add(app, str(stem), f"{stem}@file")
    return rows


def current_failed(allocations: Iterable[Any]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for alloc in allocations:
        if not isinstance(alloc, dict):
            continue
        if str(alloc.get("DesiredStatus") or "") != "run":
            continue
        if str(alloc.get("ClientStatus") or "") not in {"failed", "lost"}:
            continue
        job = str(alloc.get("JobID") or "")
        group = str(alloc.get("TaskGroup") or "")
        if not job:
            continue
        key = (job, group)
        counts[key] = counts.get(key, 0) + 1
    return counts


def node_identity(node: dict[str, Any]) -> tuple[str, str]:
    meta = node.get("Meta") if isinstance(node.get("Meta"), dict) else {}
    name = str(meta.get("luma_node_name") or node.get("Name") or node.get("ID") or "").strip()
    region = str(meta.get("region") or "").strip()
    return name, region


def collect(addr: str, token: str, *, routes_dir: Path | None = None) -> str:
    lines = [
        "# HELP luma_observe_nomad_up Whether the Nomad API was reachable from luma-observe.",
        "# TYPE luma_observe_nomad_up gauge",
    ]
    try:
        jobs = fetch_json(addr, "/v1/jobs?meta=true", token)
        allocations = fetch_json(addr, "/v1/allocations?resources=false", token)
        nodes = fetch_json(addr, "/v1/nodes", token)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return "\n".join(lines + ["luma_observe_nomad_up 0", ""])
    if not isinstance(jobs, list) or not isinstance(allocations, list) or not isinstance(nodes, list):
        return "\n".join(lines + ["luma_observe_nomad_up 0", ""])

    failed_now = current_failed(allocations)
    lines.append("luma_observe_nomad_up 1")
    lines += [
        "# HELP luma_observe_job_active 1 if the Nomad job is supposed to be running.",
        "# TYPE luma_observe_job_active gauge",
        "# HELP luma_observe_job_running Running allocations reported by Nomad job summary.",
        "# TYPE luma_observe_job_running gauge",
        "# HELP luma_observe_job_failed Currently failed allocations that Nomad still wants to run.",
        "# TYPE luma_observe_job_failed gauge",
        "# HELP luma_observe_job_queued Queued allocations reported by Nomad job summary.",
        "# TYPE luma_observe_job_queued gauge",
        "# HELP luma_observe_allocs Allocation count by job and client status.",
        "# TYPE luma_observe_allocs gauge",
        "# HELP luma_observe_alloc_restarts Task restart count on a live allocation.",
        "# TYPE luma_observe_alloc_restarts gauge",
        "# HELP luma_observe_router_app Maps a Traefik router label to a Luma app.",
        "# TYPE luma_observe_router_app gauge",
    ]
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = app_id(job)
        if not name:
            continue
        summary = job.get("JobSummary") if isinstance(job.get("JobSummary"), dict) else {}
        groups = summary.get("Summary") if isinstance(summary.get("Summary"), dict) else {}
        if not groups:
            groups = {"": {}}
        active = 1 if job_active(job) else 0
        for group, counts in groups.items():
            if not isinstance(counts, dict):
                counts = {}
            labels = _labels(app=name, job=name, task_group=str(group))
            lines.append(f"luma_observe_job_active{labels} {active}")
            lines.append(f"luma_observe_job_running{labels} {_num(counts.get('Running')):g}")
            lines.append(f"luma_observe_job_failed{labels} {failed_now.get((name, str(group)), 0)}")
            lines.append(f"luma_observe_job_queued{labels} {_num(counts.get('Queued')):g}")

    status_counts: dict[tuple[str, str, str], int] = {}
    restart_lines: list[str] = []
    for alloc in allocations:
        if not isinstance(alloc, dict):
            continue
        job = str(alloc.get("JobID") or "")
        group = str(alloc.get("TaskGroup") or "")
        status = str(alloc.get("ClientStatus") or "unknown")
        if not job:
            continue
        key = (job, group, status)
        status_counts[key] = status_counts.get(key, 0) + 1
        if status != "running":
            continue
        states = alloc.get("TaskStates") if isinstance(alloc.get("TaskStates"), dict) else {}
        alloc_id = str(alloc.get("ID") or "")[:8]
        for task, state in states.items():
            if not isinstance(state, dict):
                continue
            restart_lines.append(
                "luma_observe_alloc_restarts"
                + _labels(app=job, job=job, task_group=group, alloc=alloc_id, task=str(task))
                + f" {_num(state.get('Restarts')):g}"
            )
    for (job, group, status), count in sorted(status_counts.items()):
        lines.append(
            f"luma_observe_allocs{_labels(app=job, job=job, task_group=group, status=status)} {count}"
        )
    lines.extend(restart_lines)
    for row in router_mappings(jobs, allocations, file_route_stems(routes_dir or ROUTES_DIR)):
        lines.append(
            "luma_observe_router_app"
            + _labels(app=row["app"], service=row["service"], router=row["router"])
            + " 1"
        )
    lines += [
        "# HELP luma_observe_node_ready 1 if the Nomad client is ready.",
        "# TYPE luma_observe_node_ready gauge",
        "# HELP luma_observe_node_eligible 1 if the Nomad client can receive work.",
        "# TYPE luma_observe_node_eligible gauge",
        "# HELP luma_observe_node_allocs Allocation count by node and client status.",
        "# TYPE luma_observe_node_allocs gauge",
    ]
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name, region = node_identity(node)
        if not name:
            continue
        ready = 1 if str(node.get("Status") or "").lower() == "ready" else 0
        eligible = 1 if str(node.get("SchedulingEligibility") or "").lower() == "eligible" else 0
        labels = _labels(node=name, region=region)
        lines.append(f"luma_observe_node_ready{labels} {ready}")
        lines.append(f"luma_observe_node_eligible{labels} {eligible}")
    node_allocs: dict[tuple[str, str], int] = {}
    for alloc in allocations:
        if not isinstance(alloc, dict):
            continue
        node = str(alloc.get("NodeName") or "").strip()
        status = str(alloc.get("ClientStatus") or "unknown")
        if not node:
            continue
        key = (node, status)
        node_allocs[key] = node_allocs.get(key, 0) + 1
    for (node, status), count in sorted(node_allocs.items()):
        lines.append(f"luma_observe_node_allocs{_labels(node=node, status=status)} {count}")
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    addr = "http://127.0.0.1:4646"
    token = ""

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] not in {"/", "/metrics", "/healthz"}:
            self.send_error(404)
            return
        body = collect(self.addr, self.token).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    bind = os.environ.get("LUMA_OBSERVE_BIND", "127.0.0.1")
    port = int(os.environ.get("LUMA_OBSERVE_PORT", "9107"))
    Handler.addr = os.environ.get("NOMAD_ADDR", "http://127.0.0.1:4646")
    Handler.token = os.environ.get("NOMAD_TOKEN", "").strip()
    server = ThreadingHTTPServer((bind, port), Handler)
    sys.stderr.write(f"luma-observe nomad-exporter listening on {bind}:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
