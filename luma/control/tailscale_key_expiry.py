"""Disable a verified Luma node's Tailscale key expiry during enrollment."""
from __future__ import annotations

import base64
import http.client
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..errors import LumaError
from ..nomad_api import NomadApi
from ..nomad_pki import client_tls
from ..nomad_security import mesh_ip, private_read, security_root, service_token_path
from ..secret_storage import read_credential_file
from .state import state_dir


_API = "https://api.tailscale.com/api/v2"
_CONFIG_FILE = "tailscale-key-expiry.json"
_LOG = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(
    path: str,
    *,
    action: str,
    authorization: str = "",
    data: bytes | None = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    if data is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(_API + path, data=data, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(256 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise LumaError(f"Tailscale API {action} failed (HTTP {exc.code})") from None
    except (OSError, http.client.HTTPException):
        raise LumaError(f"Tailscale API {action} could not be reached") from None
    if len(raw) > 256 * 1024:
        raise LumaError(f"Tailscale API {action} returned an oversized response")
    try:
        result = json.loads(raw) if raw else {}
    except ValueError:
        raise LumaError(f"Tailscale API {action} returned invalid JSON") from None
    if not isinstance(result, dict):
        raise LumaError(f"Tailscale API {action} returned an invalid response")
    return result


def _authorization(config: dict[str, Any]) -> str:
    api_key = config.get("apiKey")
    client_id = config.get("clientId")
    client_secret = config.get("clientSecret")
    if isinstance(api_key, str) and api_key.strip() and not client_id and not client_secret:
        encoded = base64.b64encode((api_key.strip() + ":").encode()).decode()
        return f"Basic {encoded}"
    if api_key or not all(isinstance(value, str) and value.strip() for value in (client_id, client_secret)):
        raise LumaError(f"{_CONFIG_FILE} requires apiKey or both clientId and clientSecret")
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "scope": "devices:core",
    }).encode()
    result = _request(
        "/oauth/token", action="token exchange", data=data,
        content_type="application/x-www-form-urlencoded",
    )
    token = result.get("access_token")
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]+", token):
        raise LumaError("Tailscale API token exchange returned no usable access token")
    return f"Bearer {token}"


def _verify_nomad_peer(address: str, node_name: str, node_id: str) -> None:
    # The IP and device ID in a join request are claims. Verify the enrolled
    # Nomad client over its own TLS listener at that literal mesh address.
    # Only use the read credential that Control already issues to node agents.
    tls = client_tls()
    if not tls:
        raise LumaError("Control has no Nomad TLS identity to verify the joining device")
    token = private_read(service_token_path(security_root(), "agent")).strip()
    if not token:
        raise LumaError("Control has no Nomad node reader credential")
    host = f"[{address}]" if ":" in address else address
    client = NomadApi(f"https://{host}:4646", token=token, tls=tls, managed_transport=False)
    try:
        peer = client.request("GET", "/v1/agent/self", timeout=4)
        if peer["config"]["NodeName"] == node_name and peer["stats"]["client"]["node_id"] == node_id:
            return
    except (LumaError, OSError, TypeError, ValueError, KeyError):
        pass
    raise LumaError("cannot verify the joining Nomad node at its Tailscale address")


def disable_on_join(
    *, node_name: str, node_id: str, tailscale_ip: str, device_id: str,
) -> dict[str, str] | None:
    """Apply the optional Control policy to this join; report a warning on failure."""
    try:
        raw_config = read_credential_file(state_dir() / _CONFIG_FILE, optional=True)
        if not raw_config:
            return None
        try:
            config = json.loads(raw_config)
        except ValueError:
            raise LumaError(f"{_CONFIG_FILE} must be a JSON object") from None
        if not isinstance(config, dict):
            raise LumaError(f"{_CONFIG_FILE} must be a JSON object")
        if not tailscale_ip or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device_id):
            raise LumaError("the join did not report a Tailscale device identity; update the joining CLI or node agent")
        address = mesh_ip(tailscale_ip)
        _verify_nomad_peer(address, node_name, node_id)
        authorization = _authorization(config)
        device_path = f"/device/{device_id}"
        device = _request(device_path, action="device lookup", authorization=authorization)
        addresses = device.get("addresses")
        if device.get("nodeId") != device_id or not isinstance(addresses, list) or address not in addresses:
            raise LumaError("Tailscale API device identity does not match the joining node")
        if device.get("authorized") is not True or device.get("isExternal") is True or device.get("isEphemeral") is True:
            raise LumaError("Tailscale device must be authorized, non-ephemeral and owned by this tailnet")
        if device.get("keyExpiryDisabled") is not True:
            _request(
                device_path + "/key", action="key expiry update", authorization=authorization,
                data=b'{"keyExpiryDisabled":true}',
            )
    except (LumaError, OSError, UnicodeError) as exc:
        detail = str(exc) if isinstance(exc, LumaError) else f"cannot read {_CONFIG_FILE}"
        message = f"Tailscale key expiry was not confirmed disabled: {detail}"
        _LOG.warning("%s: %s", node_name, message)
        return {"status": "warning", "message": message}
    return {"status": "disabled", "message": "Tailscale node key expiry is disabled"}
