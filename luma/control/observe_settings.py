"""Persistent operator settings for the optional observability stack."""
from __future__ import annotations

from pathlib import Path
import threading
from typing import Any

from ..errors import LumaError
from ..observe_grafana import GRAFANA_PUBLIC_ROUTE_NAME, write_grafana_public_route, write_route_file
from .state import load_auth_state, mutate_state, require_token

ROUTES_DIR = Path("/opt/luma/routes")
_lock = threading.Lock()


def public_sharing_enabled(state: dict[str, Any]) -> bool:
    settings = state.get("observeSettings")
    return isinstance(settings, dict) and settings.get("grafanaPublicSharingEnabled") is True


def get_settings(token: str) -> dict[str, bool]:
    state = load_auth_state()
    require_token(state, token, token_type="deploy")
    return {"grafanaPublicSharingEnabled": public_sharing_enabled(state)}


def update_settings(token: str, body: dict[str, Any]) -> dict[str, bool]:
    require_token(load_auth_state(), token, token_type="deploy")
    if set(body) != {"grafanaPublicSharingEnabled"} or type(body.get("grafanaPublicSharingEnabled")) is not bool:
        raise LumaError("grafanaPublicSharingEnabled must be a boolean and is the only supported setting")
    enabled = body["grafanaPublicSharingEnabled"]
    with _lock:
        path = ROUTES_DIR / GRAFANA_PUBLIC_ROUTE_NAME
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        route_written = False

        def apply(state: dict[str, Any]) -> dict[str, bool]:
            nonlocal route_written
            require_token(state, token, token_type="deploy")
            write_grafana_public_route(ROUTES_DIR, str(state.get("domain") or ""), enabled=enabled)
            route_written = True
            state.setdefault("observeSettings", {})["grafanaPublicSharingEnabled"] = enabled
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
