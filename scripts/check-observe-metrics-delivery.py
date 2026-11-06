#!/usr/bin/env python3
"""Verify the shipped PRW exporter delivers each idle metric exactly once.

Runs the pinned Collector and VictoriaMetrics in a network-none namespace with
the exporter block from observe/collector.yaml. Checks that a single point is
stored without later input, is not re-sent while the Collector stays idle, and
is retried exactly once after a short VictoriaMetrics outage. --baseline
re-enables the PRW WAL (buffer_size 1) to reproduce its tail re-send loop.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("observe_tempo_check", ROOT / "scripts/check-observe-tempo.py")
assert spec and spec.loader
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
docker = helpers.docker
IDLE_SECONDS = 8


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    prefix = "luma-prw-check-" + uuid.uuid4().hex[:10]
    names = {name: prefix + "-" + name for name in ("probe", "victoria", "collector")}
    volume = prefix + "-data"
    compose = yaml.safe_load((ROOT / "observe/docker-compose.yml").read_text())
    collector_image = next(line.split()[1] for line in (ROOT / "observe/Dockerfile.collector").read_text().splitlines()
                           if line.startswith("FROM otel/"))
    images = ["python:3.12-alpine", compose["services"]["victoria"]["image"], collector_image]
    existing_images = set(docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").splitlines())

    def request(port: int, path: str, body: dict | None = None) -> dict:
        return json.loads(docker("exec", "-i", names["probe"], "python", "-c", helpers.HTTP,
                                stdin=json.dumps({"url": f"http://127.0.0.1:{port}{path}",
                                                  "method": "GET" if body is None else "POST",
                                                  "body": None if body is None else json.dumps(body)})))

    def wait_for(check, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.5)
        raise RuntimeError("metric delivery did not complete within the bounded wait")

    def rows() -> int:
        # Raw ingested rows, before VictoriaMetrics hides same-timestamp duplicates.
        response = request(8428, "/metrics")
        for line in response["body"].splitlines():
            if line.startswith('vm_rows_inserted_total{type="promremotewrite"}'):
                return int(float(line.split()[-1]))
        return 0

    def found(name: str) -> bool:
        response = request(8428, "/api/v1/query?" + "query=" + name + "&nocache=1")
        return response["status"] == 200 and bool(json.loads(response["body"])["data"]["result"])

    def send(name: str) -> None:
        response = request(4318, "/v1/metrics", {"resourceMetrics": [{"scopeMetrics": [{"metrics": [{
            "name": name, "gauge": {"dataPoints": [{"timeUnixNano": str(time.time_ns()), "asInt": "7"}]},
        }]}]}]})
        if response["status"] != 200 or json.loads(response["body"]).get("partialSuccess", {}).get("rejectedDataPoints", 0) not in (0, "0"):
            raise RuntimeError(f"OTLP input failed: {response}")

    def expect_rows(before: int, label: str) -> int:
        time.sleep(IDLE_SECONDS)
        added = rows() - before
        if added != 1:
            raise RuntimeError(f"{label}: expected exactly 1 stored row after {IDLE_SECONDS}s idle, got {added}")
        return before + added

    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            exporter = yaml.safe_load((ROOT / "observe/collector.yaml").read_text())["exporters"]["prometheusremotewrite"]
            if args.baseline:
                exporter["wal"] = {"directory": "/var/lib/otelcol/prw", "buffer_size": 1}
            elif "wal" in exporter:
                raise RuntimeError("observe/collector.yaml re-enabled the PRW WAL; it re-sends its newest entry")
            # Keep retries short so the outage case finishes quickly; the
            # shipped config uses the exporter's default 5-minute window.
            exporter["retry_on_failure"] = {"initial_interval": "1s", "max_interval": "2s", "max_elapsed_time": "60s"}
            config = {"receivers": {"otlp": {"protocols": {"http": {"endpoint": "127.0.0.1:4318"}}}},
                      "exporters": {"prometheusremotewrite": exporter},
                      "service": {"telemetry": {"metrics": {"level": "none"}},
                                  "pipelines": {"metrics": {"receivers": ["otlp"], "exporters": ["prometheusremotewrite"]}}}}
            path = Path(directory) / "collector.yaml"
            path.write_text(yaml.safe_dump(config))
            docker("volume", "create", volume)
            docker("run", "-d", "--name", names["probe"], "--network", "none", "--memory", "64m",
                   "-v", volume + ":/data", images[0], "python", "-c",
                   "import os,time; os.makedirs('/data/prw',exist_ok=True); os.chmod('/data',0o777); os.chmod('/data/prw',0o777); time.sleep(600)")
            docker("run", "-d", "--name", names["victoria"], "--network", "container:" + names["probe"],
                   "--memory", "256m", images[1], "--httpListenAddr=127.0.0.1:8428", "--storageDataPath=/tmp/data",
                   "--search.latencyOffset=0s")
            wait_for(lambda: request(8428, "/health")["status"] == 200)
            docker("run", "-d", "--name", names["collector"], "--network", "container:" + names["probe"],
                   "--memory", "512m", "-e", "GOMEMLIMIT=256MiB", "-v", volume + ":/var/lib/otelcol",
                   "-v", str(path) + ":/config.yaml:ro", images[2], "--config=/config.yaml")
            wait_for(lambda: request(4318, "/v1/metrics", {"resourceMetrics": []})["status"] == 200)

            before = rows()
            send("prw_idle_probe")
            wait_for(lambda: found("prw_idle_probe"))
            if args.baseline:
                time.sleep(IDLE_SECONDS)
                added = rows() - before
                if added <= 1:
                    raise RuntimeError(f"Expected the PRW WAL tail re-send loop, got {added} row(s)")
                print(f"Reproduced: 1 input point became {added} stored rows in {IDLE_SECONDS}s with no further input.")
                return
            before = expect_rows(before, "idle delivery")
            print("One idle point reached VictoriaMetrics exactly once.", flush=True)

            docker("stop", "-t", "5", names["victoria"])
            send("prw_outage_probe")
            time.sleep(3)
            docker("start", names["victoria"])
            wait_for(lambda: request(8428, "/health")["status"] == 200)
            before = rows()
            wait_for(lambda: found("prw_outage_probe"))
            expect_rows(before, "retry after outage")
            print("A point sent during a VictoriaMetrics outage was retried and stored exactly once.", flush=True)
    except BaseException:
        for name in (names["collector"], names["victoria"]):
            result = subprocess.run(["docker", "logs", "--tail", "12", name], capture_output=True, text=True, timeout=15)
            print((result.stdout + result.stderr)[-5000:])
        raise
    finally:
        for name in reversed(list(names.values())):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=30)
        for image in images:
            if image not in existing_images:
                subprocess.run(["docker", "image", "rm", image], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
