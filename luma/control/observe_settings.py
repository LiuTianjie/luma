"""Persistent operator settings for the optional observability stack."""
from __future__ import annotations

from pathlib import Path
import threading
from typing import Any

from ..errors import LumaError
from ..observe_grafana import GRAFANA_PUBLIC_ROUTE_NAME, write_grafana_public_route, write_route_file
from .observe_sharing import PASSWORD_HASH_KEY, hash_password, public_sharing_enabled
from .state import load_auth_state, mutate_state, require_token

ROUTES_DIR = Path("/opt/luma/routes")
_lock = threading.Lock()


def get_settings(token: str) -> dict[str, bool]:
    state = load_auth_state()
    require_token(state, token, token_type="deploy")
    return {"grafanaPublicSharingEnabled": public_sharing_enabled(state)}


def update_settings(token: str, body: dict[str, Any]) -> dict[str, bool]:
    require_token(load_auth_state(), token, token_type="deploy")
    if set(body) - {"grafanaPublicSharingEnabled", "grafanaPublicSharingPassword"} or type(body.get("grafanaPublicSharingEnabled")) is not bool:
        raise LumaError("grafanaPublicSharingEnabled must be a boolean; only a sharing password may also be provided")
    enabled = body["grafanaPublicSharingEnabled"]
    if not enabled and "grafanaPublicSharingPassword" in body:
        raise LumaError("provide a sharing password only when enabling sharing")
    # Hash before entering the state transaction. Every enable/reset needs a
    # password; disabling deletes it and re-enabling cannot revive old sessions.
    password_hash = hash_password(body.get("grafanaPublicSharingPassword")) if enabled else ""
    with _lock:
        path = ROUTES_DIR / GRAFANA_PUBLIC_ROUTE_NAME
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        route_written = False

        def apply(state: dict[str, Any]) -> dict[str, bool]:
            nonlocal route_written
            require_token(state, token, token_type="deploy")
            write_grafana_public_route(ROUTES_DIR, str(state.get("domain") or ""), enabled=enabled)
            route_written = True
            settings = state.setdefault("observeSettings", {})
            settings["grafanaPublicSharingEnabled"] = enabled
            if enabled:
                settings[PASSWORD_HASH_KEY] = password_hash
            else:
                settings.pop(PASSWORD_HASH_KEY, None)
            return {"grafanaPublicSharingEnabled": enabled}

        try:
            return mutate_state(apply)
        except Exception:
            # SQLite and the file provider cannot share a transaction. If the
            # commit fails, restore exactly the previous route before reporting it.
            if route_written:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    write_route_file(path, previous)
            raise


def reconcile_settings() -> None:
    """Restore the saved setting when Control starts, including the disabled state."""
    with _lock:
        state = load_auth_state()
        write_grafana_public_route(ROUTES_DIR, str(state.get("domain") or ""), enabled=public_sharing_enabled(state))
