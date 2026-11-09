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
from pathlib import Path
from typing import Any, Dict

from ..errors import LumaError
from .state import mutate_state

_exported_names: set[str] = set()


def _apply_state_secrets(state: Dict[str, Any]) -> None:
    secrets = state.get("secrets") or {}
    if not isinstance(secrets, dict):
        return
    from ..secret_storage import enabled as encrypted_storage
    private = encrypted_storage()
    # These are explicit infrastructure integrations, never an application
    # secret namespace. Do not export all business credentials to subprocesses.
    infrastructure = {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID", "TRAEFIK_ACME_EMAIL", "TAILSCALE_AUTHKEY",
                      "EGRESS_SUBSCRIPTION_URL", "GITHUB_TOKEN", "GITEA_TOKEN", "ALICLOUD_ACCESS_KEY_ID", "ALICLOUD_ACCESS_KEY_SECRET"}
    if private:
        from ..config import load_config
        path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
        if path.is_file():
            dns = load_config(path).dns
            infrastructure.update(str(dns[name]) for name in ("apiTokenEnv", "zoneIdEnv") if dns.get(name))
        for name in _exported_names - infrastructure:
            os.environ.pop(name, None)
        _exported_names.intersection_update(infrastructure)
    for key, value in secrets.items():
        if value is None:
            continue
        if private and key not in infrastructure:
            os.environ.pop(str(key), None)
            continue
        os.environ[str(key)] = str(value)
        _exported_names.add(str(key))


def _render_secrets(state: Dict[str, Any], *, scope: str, body: Dict[str, Any], texts: list[str], extra_referenced: set[str] | None = None) -> tuple[Dict[str, str], Dict[str, Any]]:
    """Build the secrets map a render call consumes, WITHOUT touching os.environ.

    Returns (secrets, result). `secrets` contains only the referenced names the
    renderer substitutes ${VAR} from, overlaid with this scope's stored values.
    Tokens referenced by plain manifest fields
    (e.g. cloudflared `tokenEnv`, which is not a ${...} reference) still resolve.
    `result` carries {scope, imported, referenced, scoped} for progress display.

    `extra_referenced` names secrets the renderer WILL consume but that do not
    appear as a literal ${VAR} in the manifest text — chiefly cloudflared's
    tunnel.tokenEnv. Without folding these into `referenced`, the scoped/--env
    paths would drop them (--env filters incoming to referenced names; the
    scoped overlay loop only copies referenced names), so a cloudflare-tunnel
    deploy would fail with "missing deployment secret" unless the token happened
    to be a GLOBAL secret. They are treated exactly like a ${...} reference.

    Returning a per-deploy dict instead of writing process-global os.environ
    keeps render pure and safe under concurrent deploys (no cross-scope secret
    bleed when two deploys reference the same name with different values).
    """
    referenced = _referenced_env_names(texts)
    if extra_referenced:
        referenced.update(extra_referenced)
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

    # Plain-field references such as cloudflared tokenEnv are included above.
    # Unreferenced credentials never enter this deployment's rendering context.
    merged: Dict[str, str] = {str(k): str(v) for k, v in global_secrets.items() if k in referenced and v is not None}
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
