from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from typing import Any

from ..errors import LumaError

TERMINAL = {"succeeded", "failed", "canceled", "superseded"}
ACTIVE_BUILD = {"preparing", "building", "verifying", "canceling"}
DIGEST = re.compile(r"sha256:[a-f0-9]{64}$")


class Conflict(LumaError):
    pass


class QueueFull(LumaError):
    pass


class NotFound(LumaError):
    pass


def identifier(prefix: str) -> str:
    return prefix + "-" + secrets.token_hex(12)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def encode_cursor(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(value: str) -> dict[str, Any]:
    try:
        if len(value) > 8192:
            raise ValueError
        result = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError) as exc:
        raise LumaError("invalid build cursor") from exc


def public_request(body: dict[str, Any]) -> dict[str, Any]:
    names = {"repoUrl", "providerId", "repository", "ref", "composeSidecar", "manifest", "region", "node", "platform", "context", "dockerfile", "buildNode", "proxyMode", "autoDeploy", "workflowApp"}
    result = {key: value for key, value in body.items() if key in names}
    if isinstance(body.get("envSecrets"), dict):
        result["envSecretNames"] = sorted(body["envSecrets"])
    return result


def summary(row: dict[str, Any]) -> dict[str, Any]:
    req = row["request"]
    return {
        "id": row["id"], "status": row["status"], "phase": row["phase"],
        "buildStatus": row["build_status"], "releaseStatus": row.get("release_status") or "",
        "mode": row["mode"], "executionBackend": "temporal-nomad",
        "source": row["source"], "projectKey": row["project_key"],
        "ref": str(req.get("ref") or ""), "buildNode": str(req.get("buildNode") or "auto"),
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "completedAt": row.get("completed_at") or 0, "message": row.get("message") or "",
        "request": req, "result": row.get("result") or {},
        "retryOf": row.get("retry_of") or "", "artifactId": row.get("artifact_id") or "",
        "releaseId": row.get("release_id") or "", "workflowId": "build/" + row["id"],
        "eventCount": row.get("next_event", 0),
        "queuePosition": row.get("queuePosition", 0), "waitReason": "build-node" if row["phase"] == "waiting-node" else row.get("waitReason", ""),
    }
