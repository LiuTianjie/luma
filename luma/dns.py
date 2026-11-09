"""DNS zones and provider routing.

A cluster has one *primary* zone, configured as ``providers.dns`` (Cloudflare,
unchanged since the first release), plus any number of additional zones that
Control stores in its state and injects as ``providers.dns.zones``.  Every
record operation resolves the zone that owns the hostname by longest suffix
and dispatches to that zone's provider.  Hostnames outside every listed zone
keep using the primary zone, exactly as before additional zones existed.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from . import cloudflare
from .config import LumaConfig
from .dns_providers import (
    PROVIDER_CLOUDFLARE,
    PROVIDER_FIELDS,
    PROVIDER_LABELS,
    DnsRecord,
    RecordProvider,
    normalize_provider,
    provider_ttl,
    record_provider,
)
from .errors import LumaError
from .service import ServiceSpec


@dataclass(frozen=True)
class DnsZone:
    name: str
    provider: str
    # credential field -> Control secret (environment variable) name
    credentials: Mapping[str, str] = field(default_factory=dict)
    zone_id: str = ""
    primary: bool = False

    def covers(self, hostname: str) -> bool:
        host = clean_name(hostname)
        return bool(self.name) and (host == self.name or host.endswith(f".{self.name}"))


def clean_name(value: Any) -> str:
    return str(value or "").strip().strip(".").lower()


def primary_zone(config: LumaConfig) -> Optional[DnsZone]:
    dns = config.dns
    if dns.get("provider") != PROVIDER_CLOUDFLARE:
        return None
    return DnsZone(
        name=clean_name(dns.get("zone")),
        provider=PROVIDER_CLOUDFLARE,
        credentials={"apiToken": str(dns.get("apiTokenEnv") or "CLOUDFLARE_API_TOKEN")},
        zone_id=str(dns.get("zoneId") or ""),
        primary=True,
    )


def zone_from_record(record: Mapping[str, Any]) -> DnsZone:
    credentials = record.get("credentials") if isinstance(record.get("credentials"), Mapping) else {}
    return DnsZone(
        name=clean_name(record.get("name")),
        provider=normalize_provider(record.get("provider")),
        credentials={str(key): str(value) for key, value in credentials.items() if value},
        zone_id=str(record.get("zoneId") or ""),
    )


def additional_zones(config: LumaConfig) -> List[DnsZone]:
    raw = config.dns.get("zones")
    if not isinstance(raw, list):
        return []
    return [zone_from_record(item) for item in raw if isinstance(item, Mapping) and item.get("name")]


def zone_catalog(config: LumaConfig) -> List[DnsZone]:
    zones = additional_zones(config)
    primary = primary_zone(config)
    if primary is not None and not any(zone.name == primary.name for zone in zones):
        zones.insert(0, primary)
    return zones


def zone_for(config: LumaConfig, hostname: str) -> Optional[DnsZone]:
    """The zone that owns ``hostname``: the longest matching suffix, else the primary."""
    matches = [zone for zone in additional_zones(config) if zone.covers(hostname)]
    primary = primary_zone(config)
    if primary is not None and primary.covers(hostname):
        matches.append(primary)
    if matches:
        return max(matches, key=lambda zone: len(zone.name))
    return primary


def with_dns_runtime(config: LumaConfig, *, zones: List[Mapping[str, Any]] | None = None, node_targets: Mapping[str, str] | None = None) -> LumaConfig:
    """Return ``config`` with Control's zone catalog and node-edge targets injected."""
    if not zones and not node_targets:
        return config
    raw = copy.deepcopy(config.raw)
    providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    dns = dict(config.dns)
    if not dns:
        # Injected keys alone must not make an unconfigured cluster look like
        # it has a Cloudflare primary zone (``config.dns`` defaults to it).
        dns["provider"] = "none"
    if zones:
        dns["zones"] = [dict(item) for item in zones]
    if node_targets:
        dns["nodeTargets"] = dict(node_targets)
    providers["dns"] = dns
    raw["providers"] = providers
    raw.pop("dns", None)
    return LumaConfig(raw, config.path)


def cert_resolver_for(config: LumaConfig, hostname: str, *, node_edge: bool = False) -> str:
    """The ACME resolver that can issue a certificate for ``hostname``.

    Cloudflare DNS-01 only validates names the Cloudflare token can edit, so
    a hostname in an Alibaba Cloud or DNSPod zone, and every node-edge
    hostname (served by a Traefik that holds no DNS credentials), uses HTTP-01.
    """
    if node_edge:
        return config.http_cert_resolver
    zone = zone_for(config, hostname)
    if zone is not None and zone.provider != PROVIDER_CLOUDFLARE:
        return config.http_cert_resolver
    return config.cert_resolver


def _credential_values(zone: DnsZone, secrets: Mapping[str, object] | None) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for field_name in PROVIDER_FIELDS[zone.provider]:
        env_name = str(zone.credentials.get(field_name) or "")
        if not env_name:
            continue
        value = os.environ.get(env_name) or (secrets.get(env_name) if isinstance(secrets, Mapping) else None)
        if value:
            values[field_name] = str(value)
    return values


def _cloudflare_config(config: LumaConfig, zone: DnsZone) -> LumaConfig:
    """The primary-zone config shape the Cloudflare synchronizer reads, for ``zone``."""
    if zone.primary:
        return config
    raw = copy.deepcopy(config.raw)
    providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    dns = dict(providers.get("dns") or raw.get("dns") or {})
    dns.update({
        "provider": PROVIDER_CLOUDFLARE,
        "type": PROVIDER_CLOUDFLARE,
        "zone": zone.name,
        "zoneId": zone.zone_id,
        # An empty variable name never matches, so CLOUDFLARE_ZONE_ID in the
        # environment cannot redirect this zone to the primary one.
        "zoneIdEnv": "",
        "apiTokenEnv": str(zone.credentials.get("apiToken") or "CLOUDFLARE_API_TOKEN"),
    })
    providers["dns"] = dns
    raw["providers"] = providers
    raw.pop("dns", None)
    return LumaConfig(raw, config.path)


def _provider(zone: DnsZone, secrets: Mapping[str, object] | None) -> RecordProvider:
    return record_provider(zone.provider, zone.name, _credential_values(zone, secrets))


def _record_dict(record: DnsRecord) -> Dict[str, Any]:
    return {"id": record.id, "name": record.name, "type": record.type, "content": record.content, "ttl": record.ttl}


def _conflicting_types(record_type: str) -> set[str]:
    if record_type == "CNAME":
        return {"A", "AAAA"}
    if record_type in {"A", "AAAA"}:
        return {"CNAME"}
    return set()


def _skip(service: ServiceSpec) -> Optional[str]:
    if not service.public:
        return "DNS skipped: service is not public"
    if service.exposure == "cloudflare-tunnel":
        return "DNS skipped: Cloudflare Tunnel public hostname is managed by the tunnel"
    return None


def _unmanaged(config: LumaConfig, hostname: str) -> str:
    if not zone_catalog(config):
        return "DNS skipped: dns.provider is not cloudflare"
    return f"DNS skipped: no Luma DNS zone manages {hostname}; add one with luma dns zone add"


def sync_dns(
    config: LumaConfig,
    service: ServiceSpec,
    *,
    secrets: Mapping[str, object] | None = None,
    override: Mapping[str, Any] | None = None,
) -> str:
    """Point the service domain at its edge, or at ``override`` (CDN CNAME)."""
    skipped = _skip(service)
    if skipped:
        return skipped
    zone = zone_for(config, str(service.domain))
    if zone is None:
        return _unmanaged(config, str(service.domain))
    if zone.provider == PROVIDER_CLOUDFLARE:
        return cloudflare.sync_dns(_cloudflare_config(config, zone), service, secrets=secrets, override=override)

    override = override or {}
    dns_config = config.dns
    target = override.get("target") or config.service_dns_target(
        exposure=service.exposure, region=service.region, node=service.node, explicit=service.dns.get("target"),
    )
    if not target:
        raise LumaError(cloudflare._missing_target_message(service))
    if service.dns.get("proxied") and "proxied" not in override:
        raise LumaError(f"dns.proxied is a Cloudflare feature; {service.domain} is in a {PROVIDER_LABELS[zone.provider]} zone")
    record_type = str(override.get("type") or service.dns.get("type") or dns_config.get("recordType", "A")).upper()
    ttl = provider_ttl(zone.provider, service.dns.get("ttl", 0))
    name = clean_name(service.domain)
    provider = _provider(zone, secrets)
    existing = provider.records(name)
    same = [item for item in existing if item.type == record_type]
    conflicting = [item for item in existing if item.type in _conflicting_types(record_type)]
    replace_target = same[0] if same else (conflicting[0] if conflicting else None)
    for item in conflicting:
        if item is not replace_target:
            provider.delete(item)
    if replace_target is not None:
        provider.update(replace_target, name, record_type, str(target), ttl)
        return f"DNS updated: {name} -> {target}"
    provider.create(name, record_type, str(target), ttl)
    return f"DNS created: {name} -> {target}"


def delete_dns(
    config: LumaConfig,
    service: ServiceSpec,
    *,
    secrets: Mapping[str, object] | None = None,
    override: Mapping[str, Any] | None = None,
) -> str:
    skipped = _skip(service)
    if skipped:
        return skipped
    zone = zone_for(config, str(service.domain))
    if zone is None:
        return _unmanaged(config, str(service.domain))
    if zone.provider == PROVIDER_CLOUDFLARE:
        return cloudflare.delete_dns(_cloudflare_config(config, zone), service, secrets=secrets, override=override)
    record_type = str((override or {}).get("type") or service.dns.get("type") or config.dns.get("recordType", "A")).upper()
    name = clean_name(service.domain)
    provider = _provider(zone, secrets)
    existing = provider.records(name, record_type)
    if not existing:
        return f"DNS not found: {name}"
    for record in existing:
        provider.delete(record)
    if len(existing) == 1:
        return f"DNS deleted: {name}"
    return f"DNS deleted: {name} ({len(existing)} records)"


def dns_records(config: LumaConfig, name: str, *, secrets: Mapping[str, object] | None = None) -> List[Dict[str, Any]]:
    zone = zone_for(config, name)
    if zone is None:
        return []
    if zone.provider == PROVIDER_CLOUDFLARE:
        return cloudflare.dns_records(_cloudflare_config(config, zone), name, secrets=secrets)
    return [_record_dict(record) for record in _provider(zone, secrets).records(clean_name(name))]


def upsert_record(config: LumaConfig, name: str, record_type: str, content: str, *, secrets: Mapping[str, object] | None = None) -> str:
    """Create or update one DNS-only record (CDN verification and DCV delegation)."""
    zone = zone_for(config, name)
    if zone is None:
        raise LumaError(f"{record_type} record publishing needs a Luma DNS zone for {name}")
    if zone.provider == PROVIDER_CLOUDFLARE:
        return cloudflare.upsert_record(_cloudflare_config(config, zone), name, record_type, content, secrets=secrets)
    provider = _provider(zone, secrets)
    clean = clean_name(name)
    existing = provider.records(clean, record_type)
    ttl = provider_ttl(zone.provider, 0)
    if existing:
        provider.update(existing[0], clean, record_type, content, ttl)
    else:
        provider.create(clean, record_type, content, ttl)
    return name


def upsert_txt_record(config: LumaConfig, name: str, content: str, *, secrets: Mapping[str, object] | None = None) -> str:
    return upsert_record(config, name, "TXT", content, secrets=secrets)


def delete_record(config: LumaConfig, name: str, record_type: str, *, secrets: Mapping[str, object] | None = None) -> int:
    zone = zone_for(config, name)
    if zone is None:
        return 0
    if zone.provider == PROVIDER_CLOUDFLARE:
        return cloudflare.delete_record(_cloudflare_config(config, zone), name, record_type, secrets=secrets)
    provider = _provider(zone, secrets)
    records = provider.records(clean_name(name), record_type)
    for record in records:
        provider.delete(record)
    return len(records)


def zone_summary(zone: DnsZone, *, configured: bool) -> Dict[str, Any]:
    return {
        "name": zone.name,
        "provider": zone.provider,
        "providerLabel": PROVIDER_LABELS[zone.provider],
        "primary": zone.primary,
        "credentials": dict(zone.credentials),
        "credentialsConfigured": configured,
    }


__all__ = [
    "cert_resolver_for",
    "DnsZone",
    "additional_zones",
    "clean_name",
    "delete_dns",
    "delete_record",
    "dns_records",
    "primary_zone",
    "sync_dns",
    "upsert_record",
    "upsert_txt_record",
    "with_dns_runtime",
    "zone_catalog",
    "zone_for",
    "zone_from_record",
    "zone_summary",
]
