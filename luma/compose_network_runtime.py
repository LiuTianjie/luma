"""Allocation-owned Docker networking for Nomad clients outside Docker's VM.

This stdlib-only file is also embedded in the trusted Nomad lifecycle tasks.
Application tasks never receive the Docker socket. Only explicitly declared
ports are published; sibling services share the allocation's private loopback.
"""
from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import time
import urllib.parse
import uuid


OWNER = "luma-compose-network-v1"
LABEL_OWNER = "io.luma.compose-network.owner"
LABEL_ALLOC = "io.luma.compose-network.allocation"
LABEL_JOB = "io.luma.compose-network.job"
LABEL_SPEC = "io.luma.compose-network.spec"


class DockerError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"Docker API {status}: {message}")
        self.status = status


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=15)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Docker:
    def __init__(self, path="/var/run/docker.sock"):
        self.path = path

    def request(self, method, path, body=None):
        connection = UnixConnection(self.path)
        try:
            data = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, body=data, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read()
            result = json.loads(raw) if raw else {}
            if not 200 <= response.status < 300:
                raise DockerError(response.status, result.get("message", "request failed"))
            return result
        finally:
            connection.close()


def container_name(allocation):
    # Container names are derived only from a full Nomad allocation UUID.
    if str(uuid.UUID(allocation)) != allocation:
        raise ValueError("allocation must be a canonical UUID")
    return f"luma-compose-net-{allocation}"


def inspect_owned(docker, allocation, job):
    name = container_name(allocation)
    try:
        info = docker.request("GET", f"/containers/{name}/json")
    except DockerError as exc:
        if exc.status == 404:
            return None
        raise
    labels = (info.get("Config") or {}).get("Labels") or {}
    expected = {LABEL_OWNER: OWNER, LABEL_ALLOC: allocation, LABEL_JOB: job}
    if any(labels.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"refusing to touch unowned network container {name}")
    return info


def network_spec(image, services, ports):
    if not image or not isinstance(image, str):
        raise ValueError("network image is required")
    if not isinstance(services, list) or not services or any(
        not isinstance(name, str) or not name or any(c in name for c in " :\n\r\t") for name in services
    ):
        raise ValueError("invalid Compose service names")
    if not isinstance(ports, list):
        raise ValueError("ports must be a list")
    bindings = {}
    published = set()
    for item in ports:
        if not isinstance(item, dict) or set(item) != {"port", "published"}:
            raise ValueError("ports require port and published")
        port, host_port = item["port"], item["published"]
        if any(type(value) is not int or not 1 <= value <= 65535 for value in (port, host_port)):
            raise ValueError("ports must be integers from 1 to 65535")
        if host_port in published:
            raise ValueError(f"duplicate published port {host_port}")
        published.add(host_port)
        bindings.setdefault(f"{port}/tcp", []).append({"HostIp": "0.0.0.0", "HostPort": str(host_port)})
    return {
        "image": image,
        "services": sorted(set(services)),
        "bindings": bindings,
    }


def prepare(docker, allocation, job, image, services, ports):
    spec = network_spec(image, services, ports)
    signature = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    existing = inspect_owned(docker, allocation, job)
    if existing is None:
        body = {
            "Image": image,
            "Entrypoint": ["python"],
            "Cmd": ["-c", "import time; time.sleep(2147483647)"],
            "Labels": {LABEL_OWNER: OWNER, LABEL_ALLOC: allocation, LABEL_JOB: job, LABEL_SPEC: signature},
            "ExposedPorts": {key: {} for key in spec["bindings"]},
            "HostConfig": {
                "NetworkMode": "bridge",
                "PortBindings": spec["bindings"],
                "ExtraHosts": [f"{name}:127.0.0.1" for name in spec["services"]],
                "RestartPolicy": {"Name": "unless-stopped"},
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
            },
        }
        path = "/containers/create?name=" + urllib.parse.quote(container_name(allocation), safe="")
        try:
            docker.request("POST", path, body)
        except DockerError as exc:
            if exc.status != 409:
                raise
        existing = inspect_owned(docker, allocation, job)
    if existing is None or (existing.get("Config", {}).get("Labels") or {}).get(LABEL_SPEC) != signature:
        raise RuntimeError("existing allocation network has a different specification")
    if not existing.get("State", {}).get("Running"):
        docker.request("POST", f"/containers/{container_name(allocation)}/start")
    current = inspect_owned(docker, allocation, job)
    if not current or not current.get("State", {}).get("Running"):
        raise RuntimeError("allocation network did not start")
    actual = current.get("HostConfig") or {}
    if actual.get("NetworkMode") != "bridge" or (actual.get("PortBindings") or {}) != spec["bindings"]:
        raise RuntimeError("allocation network port bindings do not match the requested ports")
    return current


def cleanup(docker, allocation, job):
    info = inspect_owned(docker, allocation, job)
    if info is None:
        return
    try:
        docker.request("DELETE", f"/containers/{container_name(allocation)}?force=true")
    except DockerError as exc:
        if exc.status != 404:
            raise


def watch(docker, allocation, job):
    # A lost namespace fails the allocation instead of leaving healthy-looking
    # tasks attached to a dead network. The poststop hook then removes its owner.
    failures = 0
    while True:
        try:
            info = inspect_owned(docker, allocation, job)
            if not info or not info.get("State", {}).get("Running"):
                raise RuntimeError("allocation network stopped")
            failures = 0
        except (OSError, http.client.HTTPException):
            failures += 1
            if failures >= 6:
                raise
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "watch", "cleanup"))
    parser.add_argument("--allocation", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--image")
    parser.add_argument("--services", default="[]")
    parser.add_argument("--ports", default="[]")
    args = parser.parse_args()
    docker = Docker()
    if args.action == "prepare":
        prepare(docker, args.allocation, args.job, args.image, json.loads(args.services), json.loads(args.ports))
    elif args.action == "watch":
        watch(docker, args.allocation, args.job)
    else:
        cleanup(docker, args.allocation, args.job)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Compose network failed: {exc}", file=sys.stderr)
        sys.exit(1)
