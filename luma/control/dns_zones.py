"""DNS zone catalog stored in Control state.

The primary zone stays in the cluster config (``providers.dns``) and is managed
by first install; every additional zone lives in ``state["dnsZones"]``.  Zone
credentials are ordinary Control secrets, so they are rotated in the same
place as every other secret and never returned by the API.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import yaml

from ..config import LumaConfig
from ..dns import DnsZone, clean_name, primary_zone, zone_from_record
from ..dns_providers import (
    PROVIDER_ALIDNS,
    PROVIDER_CLOUDFLARE,
    PROVIDER_DNSPOD,
    PROVIDER_FIELDS,
    PROVIDER_LABELS,
    AlidnsProvider,
    DnspodProvider,
    cloudflare_zone_id,
    normalize_provider,
    verify_cloudflare,
)
from ..errors import LumaError
from .state import mutate_state

ZONE_NAME_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
SECRET_PREFIX = "LUMA_DNS_"


def parse_zone_name(value: Any) -> str:
    name = clean_name(value)
    if "://" in name or "/" in name or not ZONE_NAME_RE.fullmatch(name):
        raise LumaError("zone must be a registered domain such as example.com, without a scheme or path")
    return name


def zone_records(state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = state.get("dnsZones")
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)} if isinstance(raw, dict) else {}


def credential_secret_name(zone: str, field: str) -> str:
    zone_key = re.sub(r"[^A-Z0-9]+", "_", zone.upper()).strip("_")
    field_key = re.sub(r"(?<!^)(?=[A-Z])", "_", field).upper()
    return f"{SECRET_PREFIX}{zone_key}_{field_key}"


def _secret_value(secrets: Mapping[str, Any], name: str) -> str:
    # The primary zone's token may come from Control's environment instead.
    return str(secrets.get(name) or (os.environ.get(name) if name else "") or "").strip()


def _credential_values(zone: DnsZone, secrets: Mapping[str, Any]) -> Dict[str, str]:
    return {
        field: _secret_value(secrets, str(zone.credentials.get(field) or ""))
        for field in PROVIDER_FIELDS[zone.provider]
        if _secret_value(secrets, str(zone.credentials.get(field) or ""))
    }


def _secrets(state: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = state.get("secrets")
    return raw if isinstance(raw, Mapping) else {}


def verify_zone(zone: DnsZone, values: Mapping[str, str]) -> Dict[str, str]:
    """Check credentials against the provider. Returns ``{"detail", "zoneId"}``."""
    missing = [field for field in PROVIDER_FIELDS[zone.provider] if not str(values.get(field) or "").strip()]
    if missing:
        raise LumaError(f"{PROVIDER_LABELS[zone.provider]} credential missing: {', '.join(missing)}")
    if zone.provider == PROVIDER_CLOUDFLARE:
        zone_id = zone.zone_id or cloudflare_zone_id(values["apiToken"], zone.name)
        return {"detail": verify_cloudflare(values["apiToken"], zone_id, zone.name), "zoneId": zone_id}
    if zone.provider == PROVIDER_ALIDNS:
        return {"detail": AlidnsProvider(zone.name, values["accessKeyId"], values["accessKeySecret"]).verify(), "zoneId": ""}
    if zone.provider == PROVIDER_DNSPOD:
        return {"detail": DnspodProvider(zone.name, values["secretId"], values["secretKey"]).verify(), "zoneId": ""}
    raise LumaError(f"unsupported DNS provider: {zone.provider}")


def deployment_hostnames(state: Mapping[str, Any]) -> Dict[str, List[str]]:
    """Public hostnames (domain and redirectFrom) per deployment name."""
    deployments = state.get("deployments") if isinstance(state.get("deployments"), Mapping) else {}
    out: Dict[str, List[str]] = {}
    for kind in ("services", "compose"):
        records = deployments.get(kind) if isinstance(deployments.get(kind), Mapping) else {}
        for slug, record in records.items():
            if not isinstance(record, Mapping):
                continue
            try:
                manifest = yaml.safe_load(str(record.get("manifest") or "")) or {}
            except yaml.YAMLError:
                continue
            if not isinstance(manifest, Mapping):
                continue
            bodies: Iterable[Any] = [manifest] if kind == "services" else (manifest.get("services") or {}).values()
            hosts: List[str] = []
            for body in bodies:
                if not isinstance(body, Mapping):
                    continue
                if body.get("domain"):
                    hosts.append(clean_name(body["domain"]))
                redirects = body.get("redirectFrom")
                if isinstance(redirects, list):
                    hosts.extend(clean_name(item) for item in redirects if item)
            if hosts:
                out[str(record.get("name") or slug)] = sorted(set(hosts))
    return out


def _owning_zone(hostname: str, zones: List[DnsZone]) -> Optional[DnsZone]:
    matches = [zone for zone in zones if zone.covers(hostname)]
    return max(matches, key=lambda zone: len(zone.name)) if matches else None


def zone_usage(zones: List[DnsZone], state: Mapping[str, Any]) -> Dict[str, List[str]]:
    usage: Dict[str, List[str]] = {zone.name: [] for zone in zones}
    for name, hosts in deployment_hostnames(state).items():
        for host in hosts:
            owner = _owning_zone(host, zones)
            if owner is not None and name not in usage[owner.name]:
                usage[owner.name].append(name)
    return {key: sorted(value) for key, value in usage.items()}


def _summary(zone: DnsZone, record: Mapping[str, Any], secrets: Mapping[str, Any], users: List[str]) -> Dict[str, Any]:
    configured = len(_credential_values(zone, secrets)) == len(PROVIDER_FIELDS[zone.provider])
    return {
        "name": zone.name,
        "provider": zone.provider,
        "providerLabel": PROVIDER_LABELS[zone.provider],
        "primary": zone.primary,
        "credentialSecrets": dict(zone.credentials),
        "credentialsConfigured": configured,
        "status": str(record.get("status") or ("ready" if configured else "missing-credentials")),
        "detail": str(record.get("detail") or ""),
        "checkedAt": int(record.get("checkedAt") or 0),
        "createdAt": int(record.get("createdAt") or 0),
        "services": users,
    }


def list_zones(config: LumaConfig, state: Mapping[str, Any]) -> Dict[str, Any]:
    records = zone_records(state)
    zones: List[tuple[DnsZone, Mapping[str, Any]]] = []
    primary = primary_zone(config)
    if primary is not None and primary.name and primary.name not in records:
        zones.append((primary, {}))
    for name in sorted(records):
        zones.append((zone_from_record(records[name]), records[name]))
    usage = zone_usage([zone for zone, _ in zones], state)
    secrets = _secrets(state)
    return {
        "zones": [_summary(zone, record, secrets, usage.get(zone.name, [])) for zone, record in zones],
        "providers": [
            {"id": provider, "label": PROVIDER_LABELS[provider], "fields": list(PROVIDER_FIELDS[provider])}
            for provider in (PROVIDER_CLOUDFLARE, PROVIDER_ALIDNS, PROVIDER_DNSPOD)
        ],
    }


def _resolve_credentials(
    name: str,
    provider: str,
    body: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[Dict[str, str], Dict[str, str]]:
    """Return ``(field -> secret name, new secret name -> value)`` for an add/update."""
    records = zone_records(state)
    secrets = _secrets(state)
    fields = PROVIDER_FIELDS[provider]
    same_as = clean_name(body.get("sameAs"))
    if same_as:
        source = records.get(same_as)
        if source is None:
            raise LumaError(f"zone {same_as} is not in the DNS zone list")
        source_zone = zone_from_record(source)
        if source_zone.provider != provider:
            raise LumaError(f"zone {same_as} uses {PROVIDER_LABELS[source_zone.provider]}, not {PROVIDER_LABELS[provider]}")
        return dict(source_zone.credentials), {}
    values = body.get("credentials") if isinstance(body.get("credentials"), Mapping) else {}
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise LumaError(f"unknown {PROVIDER_LABELS[provider]} credential field(s): {', '.join(unknown)}")
    existing = zone_from_record(records[name]).credentials if name in records else {}
    names: Dict[str, str] = {}
    new_values: Dict[str, str] = {}
    for field in fields:
        value = str(values.get(field) or "").strip()
        if value:
            secret = credential_secret_name(name, field)
            names[field] = secret
            new_values[secret] = value
        elif existing.get(field) and _secret_value(secrets, existing[field]):
            # An update that only changes other fields keeps the stored key.
            names[field] = existing[field]
        else:
            raise LumaError(f"{PROVIDER_LABELS[provider]} needs {', '.join(fields)}; missing {field}")
    return names, new_values


def add_zone(
    config: LumaConfig,
    state: Mapping[str, Any],
    body: Mapping[str, Any],
    *,
    token_check: Callable[[Mapping[str, Any]], None],
    verify: Optional[Callable[[DnsZone, Mapping[str, str]], Dict[str, str]]] = None,
) -> Dict[str, Any]:
    name = parse_zone_name(body.get("name"))
    provider = normalize_provider(body.get("provider"))
    primary = primary_zone(config)
    if primary is not None and primary.name == name:
        raise LumaError(f"{name} is the primary zone; manage it in first install (luma manager cloudflare)")
    if name in zone_records(state) and not body.get("update"):
        raise LumaError(f"zone {name} already exists; pass update to change its provider or credentials")
    credential_names, new_secrets = _resolve_credentials(name, provider, body, state)
    secrets = {**_secrets(state), **new_secrets}
    candidate = DnsZone(name=name, provider=provider, credentials=credential_names)
    values = {field: _secret_value(secrets, secret) for field, secret in credential_names.items()}
    # Verify before saving: a zone is only listed once its key really works.
    checked = (verify or verify_zone)(candidate, values)
    now = int(time.time())

    def mutate(current: Dict[str, Any]) -> Dict[str, Any]:
        token_check(current)
        stored = current.setdefault("secrets", {})
        if not isinstance(stored, dict):
            stored = {}
            current["secrets"] = stored
        stored.update(new_secrets)
        zones = current.setdefault("dnsZones", {})
        if not isinstance(zones, dict):
            zones = {}
            current["dnsZones"] = zones
        previous = zones.get(name) if isinstance(zones.get(name), dict) else {}
        record = {
            "name": name,
            "provider": provider,
            "credentials": credential_names,
            "zoneId": checked.get("zoneId") or "",
            "status": "ready",
            "detail": checked.get("detail") or "",
            "checkedAt": now,
            "createdAt": int(previous.get("createdAt") or now),
        }
        zones[name] = record
        _drop_orphan_secrets(current, previous)
        return record

    record = mutate_state(mutate)
    return {"zone": _summary(zone_from_record(record), record, secrets, []), "created": not body.get("update")}


def check_zone(config: LumaConfig, state: Mapping[str, Any], name: Any, *, verify: Optional[Callable[[DnsZone, Mapping[str, str]], Dict[str, str]]] = None) -> Dict[str, Any]:
    zone_name = parse_zone_name(name)
    records = zone_records(state)
    primary = primary_zone(config)
    if zone_name in records:
        zone = zone_from_record(records[zone_name])
    elif primary is not None and primary.name == zone_name:
        zone = primary
    else:
        raise LumaError(f"zone {zone_name} is not in the DNS zone list")
    secrets = _secrets(state)
    values = _credential_values(zone, secrets)
    now = int(time.time())
    try:
        checked = (verify or verify_zone)(zone, values)
        status, detail = "ready", checked.get("detail") or ""
    except LumaError as exc:
        status, detail = "error", str(exc)[:500]
    if zone_name in records:
        def mutate(current: Dict[str, Any]) -> None:
            zones = current.get("dnsZones") if isinstance(current.get("dnsZones"), dict) else {}
            record = zones.get(zone_name)
            if isinstance(record, dict):
                record.update({"status": status, "detail": detail, "checkedAt": now})

        mutate_state(mutate)
    return {"name": zone_name, "status": status, "detail": detail, "checkedAt": now}


def remove_zone(state: Mapping[str, Any], name: Any, *, token_check: Callable[[Mapping[str, Any]], None]) -> Dict[str, Any]:
    zone_name = parse_zone_name(name)

    def mutate(current: Dict[str, Any]) -> Dict[str, Any]:
        token_check(current)
        records = zone_records(current)
        if zone_name not in records:
            raise LumaError(f"zone {zone_name} is not in the DNS zone list")
        zones = [zone_from_record(record) for record in records.values()]
        users = zone_usage(zones, current).get(zone_name, [])
        if users:
            raise LumaError(f"zone {zone_name} is used by {', '.join(users)}; move or remove those deployments first")
        previous = current["dnsZones"].pop(zone_name)
        _drop_orphan_secrets(current, previous)
        return {"name": zone_name, "removed": True}

    return mutate_state(mutate)


def _drop_orphan_secrets(state: Dict[str, Any], previous: Mapping[str, Any]) -> None:
    """Delete Luma-generated credential secrets no zone references any more."""
    old = previous.get("credentials") if isinstance(previous.get("credentials"), Mapping) else {}
    referenced = {
        str(secret)
        for record in zone_records(state).values()
        for secret in (record.get("credentials") or {}).values()
    }
    stored = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    for secret in old.values():
        secret = str(secret)
        if secret.startswith(SECRET_PREFIX) and secret not in referenced:
            stored.pop(secret, None)
