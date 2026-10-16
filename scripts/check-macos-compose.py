#!/usr/bin/env python3
"""Opt-in OrbStack smoke test using an isolated local Nomad dev agent.

Requires a Nomad binary and an already available multi-arch Python image.
Never connects to the configured Luma/Nomad cluster. All test jobs, containers,
agent state and generated files are removed, including on failure.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from luma.compose import load_compose_deployment  # noqa: E402
from luma.compose_network_runtime import Docker, LABEL_ALLOC, LABEL_JOB, LABEL_OWNER, OWNER  # noqa: E402
from luma.config import LumaConfig  # noqa: E402
from luma.nomad_render import render_compose_job  # noqa: E402
from luma.nomad_api import deploy_to_nomad  # noqa: E402


def wait_for(description, fn, timeout=90):
    until = time.monotonic() + timeout
    error = None
    while time.monotonic() < until:
        try:
            result = fn()
            if result:
                return result
        except (OSError, ValueError, urllib.error.URLError) as exc:
            error = exc
        time.sleep(0.5)
    raise RuntimeError(f"timed out: {description}; last error: {error}")


def local_request(url, method="GET", body=None, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    payload = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, method=method, data=payload, headers={"Content-Type": "application/json", **(headers or {})})
    return opener.open(request, timeout=5)


def reserve_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in sockets]
    finally:
        for listener in sockets:
            listener.close()


SERVER = r"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, os, time, urllib.request
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/events":
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for index in range(2):
                self.wfile.write(("data: %s\n\n" % index).encode()); self.wfile.flush(); time.sleep(0.1)
            return
        if self.path == "/video":
            self.send_response(206); self.send_header("Content-Range", "bytes 1-4/6"); self.end_headers()
            self.wfile.write(b"bcde"); return
        if os.environ["ROLE"] == "api":
            with urllib.request.urlopen("http://database:9090/", timeout=3) as response:
                payload = response.read()
        else:
            payload = os.environ["MARKER"].encode()
        self.send_response(200); self.end_headers(); self.wfile.write(payload)
    def log_message(self, *args): pass
ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler).serve_forever()
"""


def run(nomad, image):
    if platform.system() != "Darwin":
        raise RuntimeError("this check verifies macOS/OrbStack and must run on a Mac")
    info = json.loads(subprocess.check_output(["docker", "info", "--format", "{{json .}}"]))
    if "OrbStack" not in info.get("OperatingSystem", ""):
        raise RuntimeError("OrbStack must be the current Docker engine")
    subprocess.run(["docker", "image", "inspect", image], check=True, stdout=subprocess.DEVNULL)
    docker_host = subprocess.check_output(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], text=True).strip()
    if not docker_host.startswith("unix://"):
        raise RuntimeError("a local Unix Docker socket is required")
    docker = Docker(docker_host.removeprefix("unix://"))
    http_port, rpc_port, serf_port, port_a, port_b = reserve_ports(5)
    base = f"http://127.0.0.1:{http_port}"
    test_id = uuid.uuid4().hex[:10]
    jobs = [f"luma-relay-check-{test_id}-{suffix}" for suffix in ("a", "b")]
    allocations = set()
    process = None

    def api(path, method="GET", body=None):
        with local_request(base + path, method, body) as response:
            return json.load(response)

    def owned_networks():
        filters = json.dumps({"label": [f"{LABEL_OWNER}={OWNER}"]})
        return [item for item in docker.request("GET", "/containers/json?all=true&filters=" + urllib.parse.quote(filters))
                if item.get("Labels", {}).get(LABEL_JOB) in jobs]

    with tempfile.TemporaryDirectory(prefix="luma-macos-compose-") as tmp:
        root = Path(tmp)
        config_path = root / "nomad.hcl"
        config_path.write_text(
            f'bind_addr = "127.0.0.1"\n'
            f'ports {{ http = {http_port}\n rpc = {rpc_port}\n serf = {serf_port} }}\n'
            'client { enabled = true\n network_interface = "lo0"\n cpu_total_compute = 12000\n'
            ' meta { region = "render"\n luma_node_name = "relay-check"\n luma_tailscale_ip = "127.0.0.1" } }\n'
            'consul { auto_advertise = false\n server_auto_join = false\n client_auto_join = false }\n'
            f'plugin "docker" {{ config {{ endpoint = {json.dumps(docker_host)}\n volumes {{ enabled = true }} }} }}\n'
        )
        log_path = root / "agent.log"
        log = log_path.open("w")
        try:
            process = subprocess.Popen(
                [nomad, "agent", "-dev", "-bind", "127.0.0.1", "-data-dir", str(root / "data"), "-config", str(config_path)],
                stdout=log, stderr=subprocess.STDOUT,
                env={k: v for k, v in os.environ.items() if not k.startswith(("NOMAD_", "CONSUL_"))},
            )
            def ready():
                if process.poll() is not None:
                    raise RuntimeError("isolated Nomad agent exited")
                if not api("/v1/status/leader"):
                    return False
                return any(n.get("Status") == "ready" for n in api("/v1/nodes"))
            wait_for("isolated Nomad agent", ready)
            nodes = {"relay-check": {"name": "relay-check", "region": "render", "agent": {"os": "darwin", "arch": "arm64"}}}
            config = LumaConfig({"defaults": {"images": {"composeNetwork": image}}}, None)

            def submit(job, published, marker):
                directory = root / job
                directory.mkdir(exist_ok=True)
                compose = {"services": {
                    "api": {"image": image, "command": ["python", "-u", "-c", SERVER], "environment": {"ROLE": "api", "PORT": "8080", "MARKER": marker},
                            "healthcheck": {"test": ["CMD", "curl", "-f", "http://127.0.0.1:8080/"], "interval": "2s", "timeout": "2s"}},
                    "database": {"image": image, "command": ["python", "-u", "-c", SERVER], "environment": {"ROLE": "database", "PORT": "9090", "MARKER": marker}},
                }}
                sidecar = {"name": job, "region": "render", "compose": "compose.yml", "services": {
                    "api": {"node": "relay-check", "exposure": "tailscale-relay", "domain": f"{job}.example.com", "port": 8080, "publishPort": published},
                    "database": {"exposure": "none"},
                }}
                (directory / "compose.yml").write_text(yaml.safe_dump(compose))
                (directory / "luma.compose.yml").write_text(yaml.safe_dump(sidecar))
                deployment = load_compose_deployment(directory / "luma.compose.yml")
                job_spec = render_compose_job(config, deployment, node_records=nodes, as_json=False)
                result = deploy_to_nomad(config, json.dumps(job_spec), {"nomadAddr": base}, slug=job, rollout_timeout_seconds=150)
                print(result, flush=True)

            def response_is(port, marker):
                with local_request(f"http://127.0.0.1:{port}/") as response:
                    return response.read().decode() == marker

            def remember_allocations():
                for job in jobs:
                    with contextlib.suppress(urllib.error.HTTPError):
                        allocations.update(item["ID"] for item in api(f"/v1/job/{job}/allocations"))

            for job, port, marker in zip(jobs, (port_a, port_b), ("database-a", "database-b")):
                submit(job, port, marker)
                wait_for(f"{job} public port and sibling DNS", lambda p=port, m=marker: response_is(p, m), timeout=150)
                remember_allocations()
            networks = owned_networks()
            assert len(networks) == 2, networks
            for container in networks:
                detail = docker.request("GET", f"/containers/{container['Id']}/json")
                assert set(detail["HostConfig"]["PortBindings"]) == {"8080/tcp"}, detail["HostConfig"]["PortBindings"]
            with local_request(f"http://127.0.0.1:{port_a}/events") as response:
                assert response.readline() == b"data: 0\n"
                assert response.readline() == b"\n"
                assert response.readline() == b"data: 1\n"
            with local_request(f"http://127.0.0.1:{port_a}/video", headers={"Range": "bytes=1-4"}) as response:
                assert response.status == 206
                assert response.headers["Content-Range"] == "bytes 1-4/6"
                assert response.read() == b"bcde"
            api(f"/v1/job/{jobs[0]}?purge=true", "DELETE")
            wait_for("poststop removes first allocation namespace", lambda: len(owned_networks()) == 1)
            assert response_is(port_b, "database-b")
            submit(jobs[0], port_a, "database-a-recreated")
            wait_for("recreated allocation reuses the public port", lambda: response_is(port_a, "database-a-recreated"), timeout=150)
            remember_allocations()
            print("PASS: Luma rollout health, Nomad scheduling, remapped Mac ports, sibling DNS, two-stack isolation, SSE, Range, poststop cleanup and recreation", flush=True)
        except Exception:
            with contextlib.suppress(Exception):
                for job in jobs:
                    entries = api(f"/v1/job/{job}/allocations")
                    print(json.dumps({"job": job, "allocations": [{"ID": a["ID"], "status": a.get("ClientStatus"), "tasks": a.get("TaskStates")} for a in entries]}, indent=2), file=sys.stderr)
            log.flush()
            print(log_path.read_text()[-10000:], file=sys.stderr)
            raise
        finally:
            for job in jobs:
                with contextlib.suppress(Exception):
                    allocations.update(a["ID"] for a in api(f"/v1/job/{job}/allocations"))
                    api(f"/v1/job/{job}?purge=true", "DELETE")
            with contextlib.suppress(Exception):
                wait_for("all poststop cleanup hooks", lambda: not owned_networks(), timeout=35)
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            log.close()
            # Remove only containers labelled with allocations from this dev
            # agent, even if an interrupted hook left one behind.
            for allocation in allocations:
                filters = urllib.parse.quote(json.dumps({"label": [f"com.hashicorp.nomad.alloc_id={allocation}"]}))
                for item in docker.request("GET", "/containers/json?all=true&filters=" + filters):
                    docker.request("DELETE", f"/containers/{item['Id']}?force=true")
            for item in owned_networks():
                if item.get("Labels", {}).get(LABEL_ALLOC) in allocations:
                    docker.request("DELETE", f"/containers/{item['Id']}?force=true")
            if owned_networks():
                raise RuntimeError("test-owned allocation network cleanup is incomplete")
            print("Cleaned this check's jobs, containers and temporary Nomad data.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nomad", required=True, help="Path to a Nomad 1.9.7 executable")
    parser.add_argument("--image", required=True, help="Locally available Python 3 image")
    args = parser.parse_args()
    run(args.nomad, args.image)
