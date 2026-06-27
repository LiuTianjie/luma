from __future__ import annotations

"""Deployment secret resolution for Luma Control.

A leaf module: it only depends on the state store and stdlib, never back on
server.py. server.py re-exports these names, so existing callers/tests that
import them from luma.control.server keep working unchanged.

The headline invariant lives in _render_secrets: render consumes an explicit
per-deploy secrets dict, NOT process-global os.environ — so two concurrent
deploys referencing the same secret name cannot clobber each other's value.
"""

import os
import re
from typing import Any, Dict

from ..errors import LumaError
from .state import mutate_state


def _apply_state_secrets(state: Dict[str, Any]) -> None:
    secrets = state.get("secrets") or {}
    if not isinstance(secrets, dict):
        return
    for key, value in secrets.items():
        if value is None:
            continue
        os.environ[str(key)] = str(value)


def _render_secrets(state: Dict[str, Any], *, scope: str, body: Dict[str, Any], texts: list[str]) -> tuple[Dict[str, str], Dict[str, Any]]:
    """Build the secrets map a render call consumes, WITHOUT touching os.environ.

    Returns (secrets, result). `secrets` is the merged global∪scoped view the
    renderer substitutes ${VAR} from — it is the full global map overlaid with
    this scope's stored values, so tokens referenced by plain manifest fields
    (e.g. cloudflared `tokenEnv`, which is not a ${...} reference) still resolve.
    `result` carries {scope, imported, referenced, scoped} for progress display.

    Returning a per-deploy dict instead of writing process-global os.environ
    keeps render pure and safe under concurrent deploys (no cross-scope secret
    bleed when two deploys reference the same name with different values).
    """
    referenced = _referenced_env_names(texts)
    incoming = _request_env_secrets(body)
    global_secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    scoped = state.get("scopedSecrets") if isinstance(state.get("scopedSecrets"), dict) else {}
    current = scoped.get(scope) if isinstance(scoped.get(scope), dict) else {}
    scoped_mode = incoming is not None or bool(current)
    imported: Dict[str, str] = {}
    if incoming is not None:
        imported = {key: value for key, value in incoming.items() if key in referenced}
        if imported:
            def mutate(persisted: Dict[str, Any]) -> None:
                persisted_scoped = persisted.setdefault("scopedSecrets", {})
                if not isinstance(persisted_scoped, dict):
                    persisted_scoped = {}
                    persisted["scopedSecrets"] = persisted_scoped
                persisted_current = persisted_scoped.setdefault(scope, {})
                if not isinstance(persisted_current, dict):
                    persisted_current = {}
                    persisted_scoped[scope] = persisted_current
                persisted_current.update(imported)

            mutate_state(mutate)
            if not isinstance(state.get("scopedSecrets"), dict):
                state["scopedSecrets"] = {}
            if not isinstance(state["scopedSecrets"].get(scope), dict):
                state["scopedSecrets"][scope] = {}
            state["scopedSecrets"][scope].update(imported)
            scoped = state["scopedSecrets"]
            current = scoped.get(scope) if isinstance(scoped.get(scope), dict) else {}

    # Base map: every global secret. Render only substitutes names that actually
    # appear in the manifest, so carrying unreferenced globals here is harmless
    # and lets plain-field token references (cloudflared tokenEnv) resolve.
    merged: Dict[str, str] = {str(k): str(v) for k, v in global_secrets.items() if v is not None}
    if scoped_mode:
        # Scoped names override globals; a referenced name absent from this
        # scope is an error (and must NOT silently fall back to a global value).
        missing: list[str] = []
        for name in sorted(referenced):
            if name in current:
                merged[name] = str(current[name])
            else:
                merged.pop(name, None)
                missing.append(name)
        if missing:
            raise LumaError(f"missing scoped deployment secrets for {scope}: {', '.join(missing)}. Add them to --env or run: luma secret set <NAME> --scope {scope}")

    result = {"scope": scope, "imported": len(imported), "referenced": sorted(referenced), "scoped": scoped_mode}
    return merged, result


def _request_env_secrets(body: Dict[str, Any]) -> Dict[str, str] | None:
    raw = body.get("envSecrets")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LumaError("envSecrets must be a mapping")
    values: Dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        if not _valid_env_name(name):
            raise LumaError(f"env secret name must be a valid environment variable name: {name!r}")
        values[name] = "" if value is None else str(value)
    return values


def _referenced_env_names(texts: list[str]) -> set[str]:
    names: set[str] = set()
    for text in texts:
        names.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text))
    return names


def _valid_env_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))
