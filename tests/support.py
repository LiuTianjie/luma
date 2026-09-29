"""Shared helpers for the Luma integration test modules."""
import json
from typing import Any, Dict


def _docker_service(service_id, name, image, replicas, constraints, labels):
    return {
        "ID": service_id,
        "Spec": {
            "Name": name,
            "Labels": labels,
            "Mode": {"Replicated": {"Replicas": replicas}},
            "TaskTemplate": {
                "ContainerSpec": {"Image": image},
                "Placement": {"Constraints": constraints},
            },
        },
    }


def _docker_task(service_id, node_id, state, container_id=""):
    status = {"State": state}
    if container_id:
        status["ContainerStatus"] = {"ContainerID": container_id}
    return {
        "ID": f"{service_id}-{node_id}-{state}",
        "ServiceID": service_id,
        "NodeID": node_id,
        "DesiredState": "running",
        "Status": status,
    }


def _restore_env(key, value):
    import os

    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


class _JsonResponse:
    def __init__(self, payload: Any, headers: Dict[str, str] | None = None):
        self.payload = payload
        self.headers = headers or {}

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _set_env(key, value):
    import os

    old = os.environ.get(key)
    os.environ[key] = value
    return old

