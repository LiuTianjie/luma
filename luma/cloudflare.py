from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .config import LumaConfig
from .errors import LumaError
from .service import ServiceSpec


API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareClient:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            API_BASE + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LumaError(f"Cloudflare API error {exc.code}: {detail}") from exc
        if not payload.get("success"):
            raise LumaError(f"Cloudflare API failed: {payload.get('errors')}")
        return payload


def sync_dns(config: LumaConfig, service: ServiceSpec) -> str:
    if not service.public:
        return "DNS skipped: service is not public"
    if service.exposure == "cloudflare-tunnel":
        return "DNS skipped: Cloudflare Tunnel public hostname is managed by the tunnel"

    dns_config = config.dns
    if dns_config.get("provider") != "cloudflare":
        return "DNS skipped: dns.provider is not cloudflare"

    token_env = str(dns_config.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN"))
    token = os.environ.get(token_env)
    if not token:
        raise LumaError(f"missing Cloudflare API token env var: {token_env}")

    zone_id = os.environ.get(str(dns_config.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID")))
    if not zone_id:
        zone_id = dns_config.get("zoneId")
    if not zone_id:
        raise LumaError("missing Cloudflare zone id: run luma cloudflare connect --zone <domain> or set providers.dns.zoneId")

    target = service.dns.get("target") or dns_config.get("edgeTarget")
    if not target:
        target = config.dns_target_for(exposure=service.exposure, region=service.region)
    if not target:
        raise LumaError("missing DNS target: configure an edge node publicIp or set service dns.target")

    record_type = str(service.dns.get("type") or dns_config.get("recordType", "A")).upper()
    proxied = bool(service.dns.get("proxied", dns_config.get("proxied", False)))
    ttl = int(service.dns.get("ttl", dns_config.get("ttl", 1)))

    client = CloudflareClient(token)
    name = service.domain
    query = urllib.parse.urlencode({"type": record_type, "name": name})
    existing = client.request("GET", f"/zones/{zone_id}/dns_records?{query}")["result"]
    body = {
        "type": record_type,
        "name": name,
        "content": target,
        "ttl": ttl,
        "proxied": proxied,
        "comment": f"managed by Luma for {service.name}",
    }
    if existing:
        record_id = existing[0]["id"]
        client.request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", body)
        return f"DNS updated: {name} -> {target}"
    client.request("POST", f"/zones/{zone_id}/dns_records", body)
    return f"DNS created: {name} -> {target}"


def delete_dns(config: LumaConfig, service: ServiceSpec) -> str:
    if not service.public:
        return "DNS skipped: service is not public"
    if service.exposure == "cloudflare-tunnel":
        return "DNS skipped: Cloudflare Tunnel public hostname is managed by the tunnel"

    dns_config = config.dns
    if dns_config.get("provider") != "cloudflare":
        return "DNS skipped: dns.provider is not cloudflare"

    token_env = str(dns_config.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN"))
    token = os.environ.get(token_env)
    if not token:
        raise LumaError(f"missing Cloudflare API token env var: {token_env}")

    zone_id = os.environ.get(str(dns_config.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID")))
    if not zone_id:
        zone_id = dns_config.get("zoneId")
    if not zone_id:
        raise LumaError("missing Cloudflare zone id: run luma cloudflare connect --zone <domain> or set providers.dns.zoneId")

    record_type = str(service.dns.get("type") or dns_config.get("recordType", "A")).upper()
    client = CloudflareClient(token)
    name = service.domain
    query = urllib.parse.urlencode({"type": record_type, "name": name})
    existing = client.request("GET", f"/zones/{zone_id}/dns_records?{query}")["result"]
    if not existing:
        return f"DNS not found: {name}"
    deleted = 0
    for record in existing:
        record_id = record.get("id")
        if not record_id:
            continue
        client.request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
        deleted += 1
    if deleted == 1:
        return f"DNS deleted: {name}"
    return f"DNS deleted: {name} ({deleted} records)"


def sync_control_dns(config: LumaConfig, domain: str) -> str:
    dns_config = config.dns
    if dns_config.get("provider") != "cloudflare":
        return "Control DNS skipped: dns.provider is not cloudflare"
    token_env = str(dns_config.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN"))
    token = os.environ.get(token_env)
    if not token:
        raise LumaError(f"missing Cloudflare API token env var: {token_env}")
    zone_id = os.environ.get(str(dns_config.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID"))) or dns_config.get("zoneId")
    if not zone_id:
        raise LumaError("missing Cloudflare zone id: run luma cloudflare connect --zone <domain> or set providers.dns.zoneId")
    target = dns_config.get("edgeTarget") or config.default_dns_target()
    if not target:
        return "Control DNS skipped: missing DNS target; configure providers.dns.edgeTarget or run luma configure --role manager"
    record_type = str(dns_config.get("recordType", "A")).upper()
    proxied = bool(dns_config.get("controlProxied", dns_config.get("proxied", False)))
    ttl = int(dns_config.get("ttl", 1))
    client = CloudflareClient(token)
    query = urllib.parse.urlencode({"type": record_type, "name": domain})
    existing = client.request("GET", f"/zones/{zone_id}/dns_records?{query}")["result"]
    body = {
        "type": record_type,
        "name": domain,
        "content": str(target),
        "ttl": ttl,
        "proxied": proxied,
        "comment": "managed by Luma control plane",
    }
    if existing:
        record_id = existing[0]["id"]
        client.request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", body)
        return f"Control DNS updated: {domain} -> {target}"
    client.request("POST", f"/zones/{zone_id}/dns_records", body)
    return f"Control DNS created: {domain} -> {target}"


def get_token(config: LumaConfig) -> tuple[str, str]:
    dns_config = config.dns
    token_env = str(dns_config.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN"))
    token = os.environ.get(token_env)
    if not token:
        raise LumaError(f"missing Cloudflare API token env var: {token_env}")
    return token, token_env


def find_zone(config: LumaConfig, zone_name: str) -> Dict[str, Any]:
    token, _ = get_token(config)
    client = CloudflareClient(token)
    query = urllib.parse.urlencode({"name": zone_name})
    result = client.request("GET", f"/zones?{query}")["result"]
    if not result:
        raise LumaError(f"Cloudflare zone not found: {zone_name}")
    return result[0]
