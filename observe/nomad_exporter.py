#!/usr/bin/env python3
"""Export Nomad job/allocation health as Prometheus gauges.

Reads the local Nomad HTTP API. Intended to run on the manager host network
so it can use 127.0.0.1:4646 without exposing Nomad publicly.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


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


def collect(addr: str, token: str) -> str:
    lines = [
        "# HELP luma_observe_nomad_up Whether the Nomad API was reachable from luma-observe.",
        "# TYPE luma_observe_nomad_up gauge",
    ]
    try:
        jobs = fetch_json(addr, "/v1/jobs", token)
        allocations = fetch_json(addr, "/v1/allocations?resources=false", token)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return "\n".join(lines + ["luma_observe_nomad_up 0", ""])
    if not isinstance(jobs, list) or not isinstance(allocations, list):
        return "\n".join(lines + ["luma_observe_nomad_up 0", ""])

    lines.append("luma_observe_nomad_up 1")
    lines += [
        "# HELP luma_observe_job_running Running allocations reported by Nomad job summary.",
        "# TYPE luma_observe_job_running gauge",
        "# HELP luma_observe_job_failed Failed allocations reported by Nomad job summary.",
        "# TYPE luma_observe_job_failed gauge",
        "# HELP luma_observe_job_queued Queued allocations reported by Nomad job summary.",
        "# TYPE luma_observe_job_queued gauge",
        "# HELP luma_observe_allocs Allocation count by job and client status.",
        "# TYPE luma_observe_allocs gauge",
        "# HELP luma_observe_alloc_restarts Task restart count on a live allocation.",
        "# TYPE luma_observe_alloc_restarts gauge",
    ]
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("Name") or job.get("ID") or "")
        if not name:
            continue
        summary = job.get("JobSummary") if isinstance(job.get("JobSummary"), dict) else {}
        groups = summary.get("Summary") if isinstance(summary.get("Summary"), dict) else {}
        if not groups:
            groups = {"": {}}
        for group, counts in groups.items():
            if not isinstance(counts, dict):
                counts = {}
            labels = _labels(job=name, task_group=group)
            lines.append(f"luma_observe_job_running{labels} {_num(counts.get('Running')):g}")
            lines.append(f"luma_observe_job_failed{labels} {_num(counts.get('Failed')):g}")
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
        states = alloc.get("TaskStates") if isinstance(alloc.get("TaskStates"), dict) else {}
        alloc_id = str(alloc.get("ID") or "")[:8]
        for task, state in states.items():
            if not isinstance(state, dict):
                continue
            restart_lines.append(
                "luma_observe_alloc_restarts"
                + _labels(job=job, task_group=group, alloc=alloc_id, task=task)
                + f" {_num(state.get('Restarts')):g}"
            )
    for (job, group, status), count in sorted(status_counts.items()):
        lines.append(f"luma_observe_allocs{_labels(job=job, task_group=group, status=status)} {count}")
    lines.extend(restart_lines)
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
