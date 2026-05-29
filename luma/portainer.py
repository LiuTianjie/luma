from __future__ import annotations

import os
import urllib.request

from .config import LumaConfig
from .errors import LumaError
from .service import ServiceSpec


def trigger_webhook(config: LumaConfig, service: ServiceSpec) -> str:
    portainer = config.portainer
    webhook_url = service.dns.get("portainerWebhookUrl") or portainer.get("webhookUrl")
    webhook_env = str(portainer.get("webhookUrlEnv", "PORTAINER_WEBHOOK_URL"))
    webhook_url = webhook_url or os.environ.get(webhook_env)
    if not webhook_url:
        return "Portainer webhook skipped: no webhook configured"

    req = urllib.request.Request(webhook_url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LumaError(f"Portainer webhook error {exc.code}: {detail}") from exc
    return f"Portainer webhook triggered for {service.name}: HTTP {status}"
