#!/usr/bin/env python3
"""Verify an idle PRW WAL batch and crash replay in disposable containers.

No scraping or later input can accidentally flush the batch under test. Use
--baseline to reproduce the pinned exporter's default-buffer idle-flush bug.
All traffic stays inside a network-none namespace; no cluster is contacted.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("observe_tempo_check", ROOT / "scripts/check-observe-tempo.py")
assert spec and spec.loader
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
docker = helpers.docker


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    prefix = "luma-wal-check-" + uuid.uuid4().hex[:10]
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

    def wait_for(check, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.5)
        raise RuntimeError("WAL delivery did not complete within the bounded wait")

    def found(name: str) -> bool:
        response = request(8428, "/api/v1/query?" + urllib.parse.urlencode({"query": name, "nocache": 1}))
        return response["status"] == 200 and bool(json.loads(response["body"])["data"]["result"])

    def send(name: str) -> None:
        response = request(4318, "/v1/metrics", {"resourceMetrics": [{"scopeMetrics": [{"metrics": [{
            "name": name, "gauge": {"dataPoints": [{"timeUnixNano": str(time.time_ns()), "asInt": "7"}]},
        }]}]}]})
        if response["status"] != 200 or json.loads(response["body"]).get("partialSuccess", {}).get("rejectedDataPoints", 0) not in (0, "0"):
            raise RuntimeError(f"OTLP input failed: {response}")

    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            exporter = yaml.safe_load((ROOT / "observe/collector.yaml").read_text())["exporters"]["prometheusremotewrite"]
            if args.baseline:
                exporter["wal"].pop("buffer_size", None)
            # Shorten only the timeout so the baseline cannot be mistaken for a
            # still-pending timer. No receivers generate background metrics.
            exporter["wal"]["truncate_frequency"] = "2s"
            config = {"receivers": {"otlp": {"protocols": {"http": {"endpoint": "127.0.0.1:4318"}}}},
                      "exporters": {"prometheusremotewrite": exporter},
                      "service": {"pipelines": {"metrics": {"receivers": ["otlp"], "exporters": ["prometheusremotewrite"]}}}}
            path = Path(directory) / "collector.yaml"
            path.write_text(yaml.safe_dump(config))
            docker("volume", "create", volume)
            docker("run", "-d", "--name", names["probe"], "--network", "none", "--memory", "64m",
                   "-v", volume + ":/data", images[0], "python", "-c",
                   "import os,time; os.chmod('/data',0o777); os.makedirs('/data/prw',exist_ok=True); os.chmod('/data/prw',0o777); time.sleep(600)")
            docker("run", "-d", "--name", names["victoria"], "--network", "container:" + names["probe"],
                   "--memory", "256m", images[1], "--httpListenAddr=127.0.0.1:8428", "--storageDataPath=/tmp/data",
                   "--search.latencyOffset=0s")
            wait_for(lambda: request(8428, "/health")["status"] == 200)

            def start_collector() -> None:
                docker("run", "-d", "--name", names["collector"], "--network", "container:" + names["probe"],
                       "--memory", "512m", "-e", "GOMEMLIMIT=256MiB", "-v", volume + ":/var/lib/otelcol",
                       "-v", str(path) + ":/config.yaml:ro", images[2], "--config=/config.yaml")
                wait_for(lambda: request(4318, "/v1/metrics", {"resourceMetrics": []})["status"] == 200)

            start_collector()
            send("wal_idle_probe")
            if args.baseline:
                time.sleep(4)
                if found("wal_idle_probe"):
                    raise RuntimeError("Expected the pinned default-buffer idle-flush failure")
                send("wal_wakeup_probe")
                wait_for(lambda: found("wal_idle_probe"))
                print("Reproduced: timer expired without delivery; a later input unlocked the pending batch.")
                return

            wait_for(lambda: found("wal_idle_probe"))
            print("One batch reached VictoriaMetrics without any later metric input.", flush=True)
            docker("stop", "-t", "5", names["victoria"])
            send("wal_replay_probe")
            wait_for(lambda: docker("exec", names["probe"], "python", "-c",
                                    "from pathlib import Path; print(any(b'wal_replay_probe' in p.read_bytes() for p in Path('/data/prw').rglob('*') if p.is_file()))") == "True")
            # Kill without graceful flush, then replay the same disk while the
            # input remains idle. The query proves the persisted batch survived.
            docker("rm", "-f", names["collector"])
            docker("start", names["victoria"])
            wait_for(lambda: request(8428, "/health")["status"] == 200)
            start_collector()
            wait_for(lambda: found("wal_replay_probe"))
            print("Pending WAL batch survived collector termination and replayed without new input.", flush=True)
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
