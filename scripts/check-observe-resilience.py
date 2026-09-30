#!/usr/bin/env python3
"""Exercise ingress with a disposable healthy, rejecting, stalled and absent OTLP receiver.

Uses Docker only; never connects to a Luma cluster. Run with the project venv.
The baseline option restores the previous duplicate OTLP metric export and limits.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from luma.nomad_render import render_traefik_job

HELPER = '''
import http.server, json, os, pathlib, time
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        body = b"backend-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        mode = pathlib.Path("/probe/mode").read_text().strip()
        with open("/probe/exports.jsonl", "a") as f:
            f.write(json.dumps({"path": self.path, "mode": mode}) + "\\n")
        if mode == "stalled": time.sleep(10)
        self.send_response(503 if mode == "rejecting" else 200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        try: self.end_headers()
        except (BrokenPipeError, ConnectionResetError): pass
http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ["PROBE_PORT"])), Handler).serve_forever()
'''


def docker(*args: str, timeout: float = 45) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[-2000:]}")
    return result.stdout.strip()


def get(url: str) -> tuple[float, str]:
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=2) as response:
        body = response.read().decode()
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
    return time.monotonic() - started, body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    os.environ["TZ"] = "Asia/Shanghai"
    prefix = "luma-observe-probe-" + uuid.uuid4().hex[:8]
    names = [prefix + suffix for suffix in ("-ingress", "-backend", "-collector")]
    report = {"variant": "baseline" if args.baseline else "fixed", "startedAt": datetime.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "phases": []}
    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            root = Path(directory)
            (root / "helper.py").write_text(HELPER)
            (root / "mode").write_text("healthy")
            (root / "routes.yml").write_text(json.dumps({"http": {
                "routers": {"probe": {"rule": "PathPrefix(`/`)", "entryPoints": ["web"], "service": "probe"}},
                "services": {"probe": {"loadBalancer": {"servers": [{"url": "http://127.0.0.1:9000"}]}}},
            }}))
            task = render_traefik_job(image="traefik:v3.6.21", as_json=False)["Job"]["TaskGroups"][0]["Tasks"][0]
            flags = [v for v in task["Config"]["args"] if not v.startswith(("--providers.", "--entrypoints.", "--api."))]
            flags += ["--providers.file.filename=/probe/routes.yml", "--entrypoints.web.address=:8080", "--entrypoints.metrics.address=:8082"]
            env = task["Env"] if not args.baseline else {}
            if args.baseline:
                flags += ["--metrics.otlp=true", "--metrics.otlp.addRoutersLabels=true", "--metrics.otlp.addServicesLabels=true", "--metrics.otlp.http.endpoint=http://127.0.0.1:4318"]
            env_args = ["-e", "TZ=Asia/Shanghai", *[item for key, value in env.items() for item in ("-e", key + "=" + value)]]
            docker("run", "-d", "--name", names[0], "--memory", "256m" if args.baseline else "512m",
                   "-p", "127.0.0.1::8080", "-p", "127.0.0.1::8082", "-v", f"{directory}:/probe:ro",
                   *env_args, "traefik:v3.6.21", *flags)
            for name, port in zip(names[1:], (9000, 4318)):
                docker("run", "-d", "--name", name, "--network", "container:" + names[0], "--memory", "128m",
                       "-v", f"{directory}:/probe", "-e", "TZ=Asia/Shanghai", "-e", f"PROBE_PORT={port}", "python:3.12-alpine", "python", "/probe/helper.py")
            container = json.loads(docker("inspect", names[0]))[0]
            ports = container["NetworkSettings"]["Ports"]
            urls = ["http://127.0.0.1:" + ports[p][0]["HostPort"] + suffix for p, suffix in (("8080/tcp", "/probe"), ("8082/tcp", "/metrics"))]
            deadline = time.monotonic() + 20
            while True:
                try:
                    if get(urls[0])[1] == "backend-ok":
                        break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                time.sleep(0.2)

            for mode, duration in (("healthy", 5), ("rejecting", 12), ("stalled", 12), ("absent", 12), ("recovered", 8)):
                (root / "mode").write_text("healthy" if mode == "recovered" else mode)
                if mode == "absent":
                    docker("stop", "-t", "1", names[2])
                elif mode == "recovered":
                    docker("start", names[2])
                end = time.monotonic() + duration

                def probe(worker: int) -> tuple[list[float], list[str]]:
                    latencies, errors = [], []
                    while time.monotonic() < end:
                        try:
                            latency, body = get(urls[1] if worker == 0 else urls[0])
                            if worker and body != "backend-ok":
                                raise RuntimeError("backend response mismatch")
                            latencies.append(latency)
                        except Exception as exc:
                            errors.append(str(exc))
                        time.sleep(0.02)
                    return latencies, errors

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(probe, range(8)))
                latencies = sorted(value for values, _ in results for value in values)
                errors = [error for _, values in results for error in values]
                state = json.loads(docker("inspect", names[0]))[0]["State"]
                phase = {"mode": mode, "requests": len(latencies), "errors": len(errors), "p99Ms": round(latencies[int(len(latencies) * .99)] * 1000, 2) if latencies else None, "maxMs": round(max(latencies) * 1000, 2) if latencies else None, "oomKilled": state["OOMKilled"]}
                report["phases"].append(phase)
                print(json.dumps(phase), flush=True)
                if errors or state["OOMKilled"] or state["Status"] != "running":
                    raise RuntimeError(f"{mode} failed: {errors[:3]}")
            exports = [json.loads(line) for line in (root / "exports.jsonl").read_text().splitlines()]
            report["exportPaths"] = sorted({row["path"] for row in exports})
            if not any(row["path"] == "/v1/traces" and row["mode"] == "healthy" for row in exports):
                raise RuntimeError("trace export was not exercised")
            if not args.baseline and any(row["path"] == "/v1/metrics" for row in exports):
                raise RuntimeError("duplicate OTLP metrics export is still enabled")
            report["ok"] = True
    except BaseException as exc:
        report["error"] = str(exc)
        for name in names:
            result = subprocess.run(["docker", "logs", "--tail", "8", name], capture_output=True, text=True, timeout=10)
            print(name, (result.stdout + result.stderr)[-2500:], file=sys.stderr)
        raise
    finally:
        for name in reversed(names):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"ok": report["ok"], "variant": report["variant"], "exportPaths": report["exportPaths"]}), flush=True)


if __name__ == "__main__":
    main()
