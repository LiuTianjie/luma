"""Control-side CDN lifecycle: state records, deploy steps and certificate sync.

``luma.cdn`` talks to Alibaba Cloud; this module binds it to Control state
(``cdnDomains`` in control_config), the Cloudflare zone and the Traefik
certificate store.  Provider errors surface as ``LumaError`` so deployment
steps can decide whether they are fatal (prepare) or advisory (purge).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..cdn import (
    AliyunClient,
    aliyun_credentials,
    delete_aliyun_domain,
    load_edge_certificate,
    prepare_aliyun_domain,
    purge_aliyun,
    sync_aliyun_certificate,
)
from ..cloudflare import dns_records, upsert_txt_record
from ..errors import LumaError
from ..service import ServiceSpec
from .state import load_auth_state, mutate_state

CDN_STATE_KEY = "cdnDomains"
CERT_SYNC_INTERVAL_SECONDS = 3600
CERT_EXPIRY_WARNING_SECONDS = 14 * 86400


def cdn_records(state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    records = state.get(CDN_STATE_KEY)
    if not isinstance(records, Mapping):
        return {}
    return {str(domain): dict(record) for domain, record in records.items() if isinstance(record, Mapping)}


def _mutable_records(state: Dict[str, Any]) -> Dict[str, Any]:
    records = state.get(CDN_STATE_KEY)
    if not isinstance(records, dict):
        records = {}
        state[CDN_STATE_KEY] = records
    return records


def _update_record(domain: str, changes: Mapping[str, Any]) -> Dict[str, Any]:
    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        records = _mutable_records(state)
        record = dict(records.get(domain) or {})
        record.update(changes)
        record["updatedAt"] = int(time.time())
        records[domain] = record
        return record

    return mutate_state(mutate)


def _client(state: Mapping[str, Any]) -> AliyunClient:
    key_id, key_secret = aliyun_credentials(state)
    return AliyunClient(key_id, key_secret)


def _origin_for(config: Any, service: ServiceSpec) -> str:
    target = service.dns.get("target") or config.dns.get("edgeTarget") or config.dns_target_for(
        exposure=service.exposure, region=service.region
    )
    if not target:
        raise LumaError("missing CDN origin: configure an edge node publicIp or set service dns.target")
    return str(target)


def _txt_publisher(config: Any, state: Mapping[str, Any], domain: str) -> Callable[[str, str], str]:
    zone = str(config.dns.get("zone") or "").strip().rstrip(".").lower()
    if not zone or not (domain.lower() == zone or domain.lower().endswith("." + zone)):
        raise LumaError(
            f"Alibaba Cloud ownership verification for {domain} needs its root domain in the Luma Cloudflare zone; "
            "add the TXT record shown in the CDN console manually and redeploy"
        )
    secrets = state.get("secrets") if isinstance(state.get("secrets"), Mapping) else {}

    def publish(host: str, content: str) -> str:
        return upsert_txt_record(config, f"{host}.{zone}", content, secrets=secrets)

    return publish


def dns_override(record: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not record or not record.get("cname"):
        return None
    return {"target": str(record["cname"]), "type": "CNAME", "proxied": False}


def service_dns_override(state: Mapping[str, Any], service: ServiceSpec) -> Optional[Dict[str, Any]]:
    """DNS target for a service whose CDN was prepared by an earlier deploy."""
    if service.cdn is None or not service.domain:
        return None
    override = dns_override(cdn_records(state).get(service.domain))
    if override is None:
        raise LumaError(f"CDN for {service.domain} has not been prepared yet; redeploy the service")
    return override


def prepare_service_cdn(config: Any, state: Mapping[str, Any], service: ServiceSpec) -> Dict[str, Any]:
    """Create or reconcile the service's CDN domain. Fatal on failure."""
    if service.cdn is None or not service.domain:
        return {"message": "CDN not enabled", "override": None, "created": False}
    domain = service.domain
    known = cdn_records(state).get(domain) or {}
    owner = str(known.get("service") or "")
    if owner and owner != service.name:
        raise LumaError(f"CDN domain {domain} is already managed for {owner}")
    certificate = load_edge_certificate(domain, resolver=config.cert_resolver)
    record = prepare_aliyun_domain(
        _client(state),
        domain=domain,
        scope=service.cdn.scope,
        origin=_origin_for(config, service),
        certificate=certificate,
        known=known,
        publish_txt=_txt_publisher(config, state, domain),
    )
    notes = record.pop("notes", [])
    created = bool(record.pop("created", False))
    _update_record(domain, {**record, "service": service.name, "lastError": ""})
    summary = "; ".join(notes) if notes else "unchanged"
    return {
        "message": f"{domain} -> {record['cname']} ({summary})",
        "override": dns_override(record),
        "created": created,
    }


def purge_service_cdn(state: Mapping[str, Any], service: ServiceSpec) -> str:
    if service.cdn is None or not service.domain:
        return "CDN purge skipped: CDN not enabled"
    task_ids = purge_aliyun(_client(state), domain=service.domain, paths=service.cdn.purge)
    _update_record(
        service.domain,
        {"lastPurge": {"at": int(time.time()), "paths": list(service.cdn.purge), "taskIds": task_ids}},
    )
    return f"refresh submitted for {', '.join(service.cdn.purge)} (task {', '.join(task_ids) or 'n/a'})"


def purge_domain(state: Mapping[str, Any], domain: str, paths: List[str]) -> Dict[str, Any]:
    from ..cdn import parse_cdn_block

    if domain not in cdn_records(state):
        raise LumaError(f"no Luma-managed CDN domain: {domain}")
    spec = parse_cdn_block({"provider": "aliyun", "purge": paths or ["/"]}, label="purge")
    assert spec is not None
    task_ids = purge_aliyun(_client(state), domain=domain, paths=spec.purge)
    _update_record(domain, {"lastPurge": {"at": int(time.time()), "paths": list(spec.purge), "taskIds": task_ids}})
    return {"domain": domain, "paths": list(spec.purge), "taskIds": task_ids}


def remove_unused_domain(config: Any, state: Mapping[str, Any], domain: str) -> Dict[str, Any]:
    """Delete a CDN domain only once DNS no longer points at it."""
    record = cdn_records(state).get(domain)
    if record is None:
        raise LumaError(f"no Luma-managed CDN domain: {domain}")
    cname = str(record.get("cname") or "").rstrip(".").lower()
    secrets = state.get("secrets") if isinstance(state.get("secrets"), Mapping) else {}
    for item in dns_records(config, domain, secrets=secrets):
        if str(item.get("type") or "").upper() == "CNAME" and str(item.get("content") or "").rstrip(".").lower() == cname:
            raise LumaError(
                f"DNS for {domain} still points at the CDN ({cname}); remove cdn from the manifest and redeploy first"
            )
    return {"domain": domain, "message": remove_cdn_domain(state, domain)}


def check_setup(config: Any, state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Read-only checks for each prerequisite, for the guided setup."""
    from ..cdn import DEFAULT_TRAEFIK_ACME_FILE, TRAEFIK_ACME_FILE_ENV
    import json
    import os
    from pathlib import Path

    checks: List[Dict[str, Any]] = []

    def run(name: str, action: Callable[[], str]) -> None:
        try:
            checks.append({"name": name, "ok": True, "detail": action()})
        except LumaError as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)})

    def credentials() -> str:
        key_id, _ = aliyun_credentials(state)
        return f"AccessKey {key_id[:6]}… configured"

    def cdn_api() -> str:
        result = _client(state).cdn("DescribeUserDomains", PageSize=1)
        return f"CDN API reachable; {int(result.get('TotalCount') or 0)} domain(s) in the account"

    def cas_api() -> str:
        _client(state).cas("ListUserCertificateOrder", OrderType="UPLOAD", ShowSize=1)
        return "Certificate (CAS) API reachable"

    def acme_store() -> str:
        path = Path(os.environ.get(TRAEFIK_ACME_FILE_ENV) or DEFAULT_TRAEFIK_ACME_FILE)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise LumaError(f"{path} not found; update the manager (luma update) so Control mounts the Traefik certificate volume") from exc
        except (OSError, ValueError) as exc:
            raise LumaError(f"cannot read {path}: {exc}") from exc
        resolver = data.get(config.cert_resolver) if isinstance(data, dict) else None
        certificates = resolver.get("Certificates") if isinstance(resolver, dict) else None
        if not certificates:
            raise LumaError(f"no certificates for resolver {config.cert_resolver} in {path}")
        names = sorted({str((item.get("domain") or {}).get("main") or "") for item in certificates if isinstance(item, dict)} - {""})
        return f"{len(certificates)} certificate(s): {', '.join(names[:5])}"

    def zone() -> str:
        value = str(config.dns.get("zone") or "").strip()
        if config.dns.get("provider") != "cloudflare" or not value:
            raise LumaError("Cloudflare DNS zone is not configured (luma manager cloudflare --zone)")
        return f"Cloudflare zone {value}"

    run("credentials", credentials)
    if checks[-1]["ok"]:
        run("cdn-api", cdn_api)
        run("cas-api", cas_api)
    run("certificate-store", acme_store)
    run("dns-zone", zone)
    return checks


def owned_domains(state: Mapping[str, Any], service_name: str) -> List[str]:
    return sorted(domain for domain, record in cdn_records(state).items() if record.get("service") == service_name)


def remove_cdn_domain(state: Mapping[str, Any], domain: str) -> str:
    deleted = delete_aliyun_domain(_client(state), domain=domain)

    def mutate(current: Dict[str, Any]) -> None:
        _mutable_records(current).pop(domain, None)

    mutate_state(mutate)
    return f"CDN domain {domain} deleted" if deleted else f"CDN domain {domain} not found in Alibaba Cloud; record removed"


def disabled_cdn_notice(state: Mapping[str, Any], service: ServiceSpec) -> Optional[str]:
    """A CDN domain kept after ``cdn`` was removed from the manifest."""
    if service.cdn is not None or not service.domain:
        return None
    record = cdn_records(state).get(service.domain)
    if not record or record.get("service") != service.name:
        return None
    return (
        f"CDN disabled for {service.domain}; DNS points at the edge again. The Alibaba Cloud domain is kept so "
        f"cached DNS answers keep working; delete it later with: luma cdn remove {service.domain}"
    )


def cdn_status(state: Mapping[str, Any], *, now: Optional[int] = None) -> List[Dict[str, Any]]:
    now = int(now or time.time())
    rows = []
    for domain, record in sorted(cdn_records(state).items()):
        not_after = int(record.get("certNotAfter") or 0)
        rows.append({
            "domain": domain,
            "service": record.get("service") or "",
            "provider": record.get("provider") or "aliyun",
            "scope": record.get("scope") or "",
            "cname": record.get("cname") or "",
            "certNotAfter": not_after,
            "certExpiresSoon": bool(not_after and not_after - now < CERT_EXPIRY_WARNING_SECONDS),
            "lastPurge": record.get("lastPurge") or {},
            "lastError": record.get("lastError") or "",
            "updatedAt": record.get("updatedAt") or 0,
        })
    return rows


def sync_cdn_certificates(config: Any, *, state: Optional[Mapping[str, Any]] = None) -> List[str]:
    """Push renewed Traefik certificates to every CDN domain; never raises LumaError."""
    state = state if state is not None else load_auth_state()
    records = cdn_records(state)
    if not records:
        return []
    messages: List[str] = []
    try:
        client = _client(state)
    except LumaError as exc:
        return [f"CDN certificate sync skipped: {exc}"]
    for domain, record in records.items():
        try:
            certificate = load_edge_certificate(domain, resolver=config.cert_resolver)
            cert_id, changed = sync_aliyun_certificate(client, domain=domain, certificate=certificate, known=record)
            if changed:
                _update_record(domain, {
                    "certId": cert_id,
                    "certSha256": certificate.sha256,
                    "certNotAfter": certificate.not_after,
                    "lastError": "",
                })
                messages.append(f"{domain}: certificate {certificate.sha256[:12]} installed")
        except LumaError as exc:
            _update_record(domain, {"lastError": f"certificate sync: {exc}"})
            messages.append(f"{domain}: {exc}")
    return messages
