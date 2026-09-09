"""Join verification: a local service start is not a confirmed Control heartbeat."""
from __future__ import annotations

import time
import http.client
from .control.client import ControlClient
from .errors import LumaError


def wait_for_node_readiness(client: ControlClient, *, node_name: str, node_id: str,
                            timeout: float = 60, interval: float = 2) -> dict:
    deadline = time.monotonic() + timeout
    last_error = "no heartbeat for the newly issued agent credentials"
    while time.monotonic() < deadline:
        try:
            result = client.request("POST", "/v1/node-agent/readiness",
                                    {"nodeName": node_name, "nodeId": node_id},
                                    timeout=max(1, min(10, int(deadline - time.monotonic()))))
            if result.get("ready") is True and result.get("nodeId") == node_id:
                return result
            last_error = str(result.get("reason") or last_error)
        except (LumaError, OSError, http.client.HTTPException) as exc:
            if "404" in str(exc):
                raise LumaError("Control does not support join verification; update the manager, then rerun node join. The node was not removed.") from exc
            last_error = str(exc)
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise LumaError(
        f"Node {node_name} was provisioned but join verification failed: {last_error}. "
        "The node was not removed. Check agent/Control connectivity and rerun node join; "
        "do not initialize or delete existing workloads."
    )
