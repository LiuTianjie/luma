"""Control-side ESA lifecycle: state records and deployment steps.

``luma.cdn`` talks to Alibaba Cloud ESA; this module binds it to Control state
(``cdnDomains`` and ``cdnSites`` in control_config) and the Cloudflare zone.
Provider errors surface as ``LumaError`` so deployment steps decide whether
they are fatal (prepare, certificate) or advisory (purge).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..cdn import (
    AliyunClient,
    configure_site,
    delete_esa_record,
    ensure_certificate,
    ensure_record,
    ensure_site,
    esa_credentials,
    find_site,
    purge_esa,
    reconcile_domain_rules,
)
from ..cloudflare import dns_records, upsert_txt_record
from ..errors import LumaError
from ..service import ServiceSpec
from .state import mutate_state

CDN_STATE_KEY = "cdnDomains"
SITE_STATE_KEY = "cdnSites"


def cdn_records(state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    records = state.get(CDN_STATE_KEY)
    if not isinstance(records, Mapping):
        return {}
    return {str(domain): dict(record) for domain, record in records.items() if isinstance(record, Mapping)}


def _site_records(state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    sites = state.get(SITE_STATE_KEY)
    if not isinstance(sites, Mapping):
        return {}
    return {str(name): dict(record) for name, record in sites.items() if isinstance(record, Mapping)}


def _update(key: str, name: str, changes: Mapping[str, Any]) -> Dict[str, Any]:
    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        records = state.get(key)
        if not isinstance(records, dict):
            records = {}
            state[key] = records
        record = dict(records.get(name) or {})
        record.update(changes)
        record["updatedAt"] = int(time.time())
        records[name] = record
        return record

    return mutate_state(mutate)


def _forget(name: str) -> None:
    def mutate(state: Dict[str, Any]) -> None:
        records = state.get(CDN_STATE_KEY)
        if isinstance(records, dict):
            records.pop(name, None)

    mutate_state(mutate)


def _client(state: Mapping[str, Any]) -> AliyunClient:
    key_id, key_secret = esa_credentials(state)
    return AliyunClient(key_id, key_secret)


def _secrets(state: Mapping[str, Any]) -> Mapping[str, Any]:
    return state.get("secrets") if isinstance(state.get("secrets"), Mapping) else {}


def _zone(config: Any) -> str:
    zone = str(config.dns.get("zone") or "").strip().rstrip(".").lower()
    if config.dns.get("provider") != "cloudflare" or not zone:
        raise LumaError("ESA needs the Luma Cloudflare zone (luma manager cloudflare --zone)")
    return zone


def _site_name(config: Any, domain: str) -> str:
    zone = _zone(config)
    if not (domain.lower() == zone or domain.lower().endswith("." + zone)):
        raise LumaError(f"{domain} is outside the Luma zone {zone}; ESA sites are created per Luma zone")
    return zone


def _origin_for(config: Any, service: ServiceSpec) -> str:
    target = service.dns.get("target") or config.dns.get("edgeTarget") or config.dns_target_for(
        exposure=service.exposure, region=service.region
    )
    if not target:
        raise LumaError("missing ESA origin: configure an edge node publicIp or set service dns.target")
    return str(target)


def _txt_publisher(config: Any, state: Mapping[str, Any]) -> Callable[[str, str], str]:
    def publish(name: str, content: str) -> str:
        return upsert_txt_record(config, name, content, secrets=_secrets(state))

    return publish


def dns_override(record: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not record or not record.get("cname"):
        return None
    return {"target": str(record["cname"]), "type": "CNAME", "proxied": False}


def service_dns_override(state: Mapping[str, Any], service: ServiceSpec) -> Optional[Dict[str, Any]]:
    """DNS target for a service whose ESA record was prepared by an earlier deploy."""
    if service.cdn is None or not service.domain:
        return None
    # No record means Prepare CDN never finished, so DNS still points at the edge.
    return dns_override(cdn_records(state).get(service.domain))


def prepare_service_cdn(config: Any, state: Mapping[str, Any], service: ServiceSpec) -> Dict[str, Any]:
    """Make sure the site and the proxied record exist. Runs before DNS changes."""
    if service.cdn is None or not service.domain:
        return {"message": "CDN not enabled", "override": None, "created": False}
    domain = service.domain
    known = cdn_records(state).get(domain) or {}
    owner = str(known.get("service") or "")
    if owner and owner != service.name:
        raise LumaError(f"ESA record {domain} is already managed for {owner}")
    client = _client(state)
    site_name = _site_name(config, domain)
    site, notes = ensure_site(
        client, site_name=site_name, scope=service.cdn.scope, publish_txt=_txt_publisher(config, state)
    )
    site_id = site["SiteId"]
    if not _site_records(state).get(site_name, {}).get("configured"):
        notes.append("site defaults: " + ", ".join(configure_site(client, site_id)))
        _update(SITE_STATE_KEY, site_name, {"siteId": site_id, "configured": True})
    record, record_notes = ensure_record(client, site_id=site_id, domain=domain, origin=_origin_for(config, service))
    notes.extend(record_notes)
    notes.extend(reconcile_domain_rules(client, site_id=site_id, domain=domain, cache=service.cdn.cache))
    saved = {
        "provider": "esa",
        "service": service.name,
        "site": site_name,
        "siteId": site_id,
        "recordId": record.get("RecordId"),
        "cname": str(record["RecordCname"]),
        "scope": str(site.get("Coverage") or service.cdn.scope),
        "purge": None if service.cdn.purge is None else list(service.cdn.purge),
        "cache": [{"path": item.path, "ttl": item.ttl} for item in service.cdn.cache],
        "lastError": "",
    }
    _update(CDN_STATE_KEY, domain, saved)
    return {
        "message": f"{domain} -> {saved['cname']} ({'; '.join(notes) or 'unchanged'})",
        "override": dns_override(saved),
        "created": not known,
        "siteId": site_id,
    }


def issue_service_certificate(state: Mapping[str, Any], service: ServiceSpec, prepared: Mapping[str, Any]) -> str:
    """Wait for ESA's free certificate. ESA validates over HTTP, so DNS must already point at it."""
    if service.cdn is None or not service.domain:
        return "CDN not enabled"
    site_id = prepared.get("siteId") or (cdn_records(state).get(service.domain) or {}).get("siteId")
    return ensure_certificate(_client(state), site_id=site_id, domain=service.domain)


def _purge(state: Mapping[str, Any], domain: str, paths: Optional[tuple]) -> Dict[str, Any]:
    record = cdn_records(state).get(domain) or {}
    task_ids = purge_esa(_client(state), site_id=record.get("siteId"), domain=domain, paths=paths)
    described = "whole hostname" if paths is None else ", ".join(paths)
    _update(CDN_STATE_KEY, domain, {"lastPurge": {"at": int(time.time()), "paths": described, "taskIds": task_ids}})
    return {"domain": domain, "paths": described, "taskIds": task_ids}


def purge_service_cdn(state: Mapping[str, Any], service: ServiceSpec) -> str:
    if service.cdn is None or not service.domain:
        return "CDN purge skipped: CDN not enabled"
    if service.cdn.purge == ():
        return "CDN purge skipped: purge is empty"
    result = _purge(state, service.domain, service.cdn.purge)
    return f"refresh submitted for {result['paths']} (task {', '.join(result['taskIds']) or 'n/a'})"


def purge_domain(state: Mapping[str, Any], domain: str, paths: List[str]) -> Dict[str, Any]:
    from ..cdn import parse_cdn_block

    if domain not in cdn_records(state):
        raise LumaError(f"no Luma-managed ESA domain: {domain}")
    spec = parse_cdn_block({"provider": "esa", "purge": paths or None}, label="purge")
    assert spec is not None
    return _purge(state, domain, spec.purge)


def owned_domains(state: Mapping[str, Any], service_name: str) -> List[str]:
    return sorted(domain for domain, record in cdn_records(state).items() if record.get("service") == service_name)


def remove_cdn_domain(state: Mapping[str, Any], domain: str) -> str:
    record = cdn_records(state).get(domain) or {}
    deleted = delete_esa_record(_client(state), site_id=record.get("siteId"), domain=domain)
    _forget(domain)
    return f"ESA record {domain} deleted" if deleted else f"ESA record {domain} not found; Luma record removed"


def remove_unused_domain(config: Any, state: Mapping[str, Any], domain: str) -> Dict[str, Any]:
    """Delete an ESA record only once DNS no longer points at it."""
    record = cdn_records(state).get(domain)
    if record is None:
        raise LumaError(f"no Luma-managed ESA domain: {domain}")
    cname = str(record.get("cname") or "").rstrip(".").lower()
    for item in dns_records(config, domain, secrets=_secrets(state)):
        if str(item.get("type") or "").upper() == "CNAME" and str(item.get("content") or "").rstrip(".").lower() == cname:
            raise LumaError(f"DNS for {domain} still points at ESA ({cname}); remove cdn from the manifest and redeploy first")
    return {"domain": domain, "message": remove_cdn_domain(state, domain)}


def disabled_cdn_notice(state: Mapping[str, Any], service: ServiceSpec) -> Optional[str]:
    """An ESA record kept after ``cdn`` was removed from the manifest."""
    if service.cdn is not None or not service.domain:
        return None
    record = cdn_records(state).get(service.domain)
    if not record or record.get("service") != service.name:
        return None
    return (
        f"CDN disabled for {service.domain}; DNS points at the edge again. The ESA record is kept so cached "
        f"DNS answers keep working; delete it later with: luma cdn remove {service.domain}"
    )


def cdn_status(state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for domain, record in sorted(cdn_records(state).items()):
        rows.append({
            "domain": domain,
            "service": record.get("service") or "",
            "provider": record.get("provider") or "esa",
            "site": record.get("site") or "",
            "scope": record.get("scope") or "",
            "cname": record.get("cname") or "",
            "purge": record.get("purge"),
            "cache": record.get("cache") or [],
            "lastPurge": record.get("lastPurge") or {},
            "lastError": record.get("lastError") or "",
            "updatedAt": record.get("updatedAt") or 0,
        })
    return rows


def check_setup(config: Any, state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Read-only checks for each prerequisite, for the guided setup."""
    checks: List[Dict[str, Any]] = []

    def run(name: str, action: Callable[[], str]) -> bool:
        try:
            checks.append({"name": name, "ok": True, "detail": action()})
            return True
        except LumaError as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)})
            return False

    def credentials() -> str:
        key_id, _ = esa_credentials(state)
        return f"AccessKey {key_id[:6]}… configured"

    def zone() -> str:
        return f"Cloudflare zone {_zone(config)}"

    def plan() -> str:
        result = _client(state).esa("ListUserRatePlanInstances", Status="online", PageSize=100)
        plans = [item for item in result.get("InstanceInfo") or [] if isinstance(item, dict)]
        if not plans:
            raise LumaError("no online ESA plan; buy one in the ESA console")
        described = []
        for item in plans:
            sites = ", ".join(str(site.get("SiteName")) for site in item.get("Sites") or [] if isinstance(site, dict)) or "no site yet"
            described.append(f"{item.get('PlanName')} {item.get('InstanceId')} ({item.get('Coverages')}; {sites})")
        return "; ".join(described)

    def site() -> str:
        name = _zone(config)
        current = find_site(_client(state), name)
        if current is None:
            return f"site {name} not created yet; the first deploy with cdn creates and verifies it"
        return f"site {name} is {current.get('Status')} ({current.get('AccessType')}, {current.get('Coverage')})"

    has_credentials = run("credentials", credentials)
    if has_credentials:
        run("esa-plan", plan)
    if run("dns-zone", zone) and has_credentials:
        run("esa-site", site)
    return checks
