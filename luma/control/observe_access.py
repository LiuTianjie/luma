"""Protect saved machine-access credentials when updating the Observe stack."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import LumaError
from ..observe_instrument import OBSERVE_STACK


ACCESS_CREDENTIALS = (
    "LUMA_OBSERVE_READ_TOKEN",
    "LUMA_OBSERVE_INGEST_TOKEN",
    "LUMA_OBSERVE_SCOPED_READERS",
)


def _access_environment(text: str) -> dict[str, Any]:
    """Read the gateway environment from a generated Nomad job."""
    try:
        job = json.loads(text)["Job"]
        return next((task.get("Env") or {} for group in job["TaskGroups"]
                     for task in group["Tasks"] if task["Name"] == "observe-access"), {})
    except (ValueError, KeyError, TypeError, AttributeError):
        raise LumaError("cannot verify Observe access credentials in the generated job") from None


def guard_access_credentials(target: Path, stack_text: str, state: dict[str, Any]) -> str:
    """Keep an enabled role configured while its deployment Secret is saved."""
    if not target.exists():
        return "No previous Observe job"
    before = _access_environment(target.read_text(encoding="utf-8"))
    after = _access_environment(stack_text)
    scoped = (state.get("scopedSecrets") or {}).get(OBSERVE_STACK) or {}
    saved = scoped or state.get("secrets") or {}
    removed = [name for name in ACCESS_CREDENTIALS
               if before.get(name) and not after.get(name) and saved.get(name)]
    if removed:
        references = ", ".join("${" + name + "}" for name in removed)
        raise LumaError(
            "Observe deployment would disable configured access: " + ", ".join(removed)
            + ". Keep " + references + " in observe-access.environment. "
            "To disable a role intentionally, remove its saved deployment Secret "
            "and set its environment value to an empty string before redeploying."
        )
    return "Observe access credential configuration checked"
