#!/usr/bin/env python3
"""Check packaged Collector -> Tempo ingestion and persistence in disposable Docker containers.

Uses the repository configs and Luma's memory conversion. An isolated shared
network namespace preserves the production loopback listeners without touching
a cluster or publishing ports. This is a lifecycle check, not a capacity test.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
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
    headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        print(json.dumps({'status': response.status, 'body': response.read().decode()}))
except urllib.error.HTTPError as error:
    print(json.dumps({'status': error.code, 'body': error.read().decode()}))
except urllib.error.URLError as error:
    print(json.dumps({'status': 0, 'body': str(error)}))
"""


def docker(*args: str, timeout: float = 60, stdin: str | None = None) -> str:
    result = subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {(result.stdout + result.stderr)[-4000:]}")
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", help="Docker target platform; defaults to the Docker host architecture")
    args = parser.parse_args()
    platform = ["--platform", args.platform] if args.platform else []
    prefix = "luma-tempo-check-" + uuid.uuid4().hex[:12]
    names = {name: prefix + "-" + name for name in ("probe", "tempo", "collector")}
    images = {name: prefix + "-" + name + ":local" for name in ("tempo", "collector")}
    volume = prefix + "-data"
    collector_volume = prefix + "-collector-data"
    helper_image = "python:3.12-alpine"
    existing_images = set(docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").splitlines())
    compose = yaml.safe_load((ROOT / "observe/docker-compose.yml").read_text())

    def request(port: int, path: str, body: str | None = None) -> dict:
        return json.loads(docker("exec", "-i", names["probe"], "python", "-c", HTTP,
                                stdin=json.dumps({"url": f"http://127.0.0.1:{port}{path}",
                                                  "method": "POST" if body is not None else "GET", "body": body})))

    def wait_for(description: str, check, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                print(description, flush=True)
                return
            time.sleep(2)
        raise RuntimeError(f"timed out: {description}")

    def start(name: str) -> None:
        service = compose["services"][name]
        resources = _resource_values(service["deploy"]["resources"])
        env = [item for key, value in service.get("environment", {}).items() for item in ("-e", f"{key}={value}")]
        mounts = (["-v", f"{volume}:/data"] if name == "tempo" else
                  ["--mount", f"type=volume,src={collector_volume},dst=/var/lib/otelcol,volume-nocopy"])
        docker("run", "-d", "--name", names[name], *platform,
               "--network", "container:" + names["probe"],
               "--memory", str(resources["MemoryMaxMB"]) + "m",
               "--memory-reservation", str(resources["MemoryMB"]) + "m",
               *env, *mounts, images[name])

    try:
        for name, image in images.items():
            dockerfile = compose["services"][name]["build"]["dockerfile"]
            docker("build", *platform, "-t", image, "-f", str(ROOT / "observe" / dockerfile),
                   str(ROOT / "observe"), timeout=600)
            print(f"built {name}", flush=True)
        docker("volume", "create", volume)
        docker("volume", "create", collector_volume)
        # Match Control's fresh local-volume preparation (0777); Tempo itself
        # still runs as the upstream non-root user. No production paths are used.
        docker("run", "-d", "--name", names["probe"], *platform, "--network", "none",
               "--memory", "64m", "-v", f"{volume}:/data", "-v", f"{collector_volume}:/collector-data", helper_image,
               "python", "-c", "import os,time; os.chmod('/data',0o777); os.chmod('/collector-data',0o777); time.sleep(3600)", timeout=180)
        start("tempo")
        wait_for("Tempo ready on fresh storage", lambda: request(3200, "/ready")["status"] == 200)
        start("collector")
        wait_for("Collector OTLP ready", lambda: request(4318, "/v1/traces", '{"resourceSpans":[]}')["status"] == 200)

        trace_id = uuid.uuid4().hex
        end = time.time_ns()
        span_name = "luma.observe.persistence-check"
        payload = {"resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "luma-observe-check"}}]},
            "scopeSpans": [{"scope": {"name": "luma.observe.check"}, "spans": [{
                "traceId": trace_id, "spanId": uuid.uuid4().hex[:16], "name": span_name,
                "kind": 1, "startTimeUnixNano": str(end - 2_000_000_000), "endTimeUnixNano": str(end),
                "status": {"code": 1},  # A slow successful trace must survive tail sampling.
            }]}],
        }]}
        response = request(4318, "/v1/traces", json.dumps(payload))
        if response["status"] != 200 or json.loads(response["body"]).get("partialSuccess", {}).get("rejectedSpans", 0) not in (0, "0"):
            raise RuntimeError(f"Collector ingestion failed: {response}")

        def found(suffix: str = "") -> bool:
            response = request(3200, "/api/traces/" + trace_id + suffix)
            return response["status"] == 200 and span_name in response["body"]

        wait_for("Sampled trace queryable through Tempo", found)
        response = request(3200, "/flush", "")
        if response["status"] != 204 and response["status"] != 200:
            raise RuntimeError(f"Tempo flush failed: {response}")

        def flushed() -> bool:
            return docker("exec", names["probe"], "python", "-c",
                          "from pathlib import Path; print(any(Path('/data/blocks').glob('*/*/meta.json')))") == "True"

        wait_for("Trace block persisted", flushed)
        # Recreate the process with the same volume. A blocks-only lookup cannot
        # accidentally pass by reading the previous ingester's live trace/WAL.
        docker("rm", "-f", names["collector"])
        docker("stop", "-t", "30", names["tempo"])
        docker("rm", names["tempo"])
        start("tempo")
        wait_for("Tempo ready after restart", lambda: request(3200, "/ready")["status"] == 200)
        wait_for("Persisted trace queryable after restart (mode=blocks)", lambda: found("?mode=blocks"))
        start("collector")
        wait_for("Collector ready with reused persistent storage", lambda: request(4318, "/v1/traces", '{"resourceSpans":[]}')["status"] == 200)
        print("Observe Tempo lifecycle check passed", flush=True)
    except BaseException:
        for name in names.values():
            result = subprocess.run(["docker", "logs", "--tail", "25", name], capture_output=True, text=True, timeout=15)
            print(name, (result.stdout + result.stderr)[-5000:], file=sys.stderr)
        raise
    finally:
        # Only remove this invocation's containers, volume and image tags.
        for name in reversed(list(names.values())):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=30)
        subprocess.run(["docker", "volume", "rm", collector_volume], capture_output=True, timeout=30)
        cleanup_images = list(images.values()) + ([] if helper_image in existing_images else [helper_image])
        for image in cleanup_images:
            subprocess.run(["docker", "image", "rm", image], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
