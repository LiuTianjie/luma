from __future__ import annotations

import copy
import ipaddress
import json
import os
import shlex
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

from .bootstrap import refresh_manager_control_local
from .cloudflare import CloudflareClient
from .config import LumaConfig, load_config
from .errors import LumaError
from .io import dump_yaml
from .local import LocalExecutor


MANAGER_CONFIG = Path("/opt/luma/luma.yaml")


def manager_ip_change(
    *,
    old_ip: str,
    new_ip: str,
    domain: str,
    state: Dict[str, object],
    config_path: Path | None = None,
    dry_run: bool = False,
    emit: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Move a healthy Luma manager ingress from one public IPv4 address to another.

    The command deliberately changes only typed configuration fields and exact
    Cloudflare A-record contents. Historical strings in control state are never
    searched or replaced.
    """

    old = _ipv4(old_ip, "old")
    new = _ipv4(new_ip, "new")
    if old == new:
        raise LumaError("manager IP change requires different --old and --new addresses")
    hostname = _domain(domain)
    state_domain = str(state.get("domain") or "").strip().rstrip(".").lower()
    if state_domain and state_domain != hostname:
        raise LumaError(
            f"--domain {hostname} does not match manager control state domain {state_domain}"
        )

    path = config_path or MANAGER_CONFIG
    config = load_config(path)
    updated_raw, node_name, changed_fields, unmanaged_paths = _updated_config(
        config, old=old, new=new
    )
    updated_config = LumaConfig(updated_raw, path)
    node = updated_config.get_node(node_name)

    token, zone_id, zone_name = _cloudflare_credentials(config, state)
    if zone_name and hostname != zone_name and not hostname.endswith("." + zone_name):
        raise LumaError(
            f"control domain {hostname} is outside configured Cloudflare zone {zone_name}"
        )
    client = CloudflareClient(token)
    records = _cloudflare_a_records_for_ip(client, zone_id, old)
    running_image = _running_control_image()
    if not running_image:
        raise LumaError(
            "running luma-control image could not be determined from Nomad; "
            "refusing an IP-only recovery that might pull or upgrade a different image"
        )

    _probe_manager(hostname, new)
    plan: Dict[str, Any] = {
        "oldIp": old,
        "newIp": new,
        "domain": hostname,
        "configPath": str(path),
        "managerNode": node_name,
        "configChanges": changed_fields,
        "unmanagedConfigPaths": unmanaged_paths,
        "dnsRecords": [_public_record(record) for record in records],
        "controlImage": running_image,
        "dryRun": bool(dry_run),
    }
    _emit_plan(plan, emit=emit)
    if dry_run:
        emit("[dry-run] No configuration, DNS, Nomad job, or control state was changed")
        return plan

    backup = ""
    if changed_fields:
        backup = _install_manager_config(path, updated_raw)
        emit(f"[ok] Manager config updated (backup: {backup})")
    else:
        emit("[skip] Manager config already points to the new IP")

    updated_records = _patch_cloudflare_records(client, zone_id, records, new, emit=emit)
    if not records:
        emit("[skip] No Cloudflare A records still point to the old IP")

    previous_image = os.environ.get("LUMA_CONTROL_IMAGE")
    os.environ["LUMA_CONTROL_IMAGE"] = running_image
    try:
        emit(f"[start] Reconcile manager control plane with current image {running_image}")
        refresh_manager_control_local(updated_config, node, hostname, state, emit=emit)
    finally:
        if previous_image is None:
            os.environ.pop("LUMA_CONTROL_IMAGE", None)
        else:
            os.environ["LUMA_CONTROL_IMAGE"] = previous_image

    _flush_dns_cache()
    _probe_manager(hostname, new)
    normal_health = _probe_normal_domain(hostname)
    if normal_health:
        emit(f"[ok] Normal DNS path is healthy: https://{hostname}/v1/health")
    else:
        emit(
            "[warn] The new IP is healthy directly, but this host still resolves the normal "
            "domain through a stale DNS cache; public caches may need their remaining TTL"
        )
    plan.update(
        {
            "dryRun": False,
            "backup": backup,
            "updatedDnsRecords": updated_records,
            "directHealth": True,
            "normalHealth": normal_health,
        }
    )
    emit(
        f"[ok] Manager IP recovery applied: {len(changed_fields)} config field(s), "
        f"{updated_records} DNS record(s)"
    )
    return plan


def _ipv4(value: str, label: str) -> str:
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError as exc:
        raise LumaError(f"--{label} must be a valid IPv4 address: {value}") from exc
    if address.version != 4:
        raise LumaError(f"--{label} must be an IPv4 address")
    return str(address)


def _domain(value: str) -> str:
    domain = str(value or "").strip().rstrip(".").lower()
    if not domain or "://" in domain or "/" in domain or ":" in domain:
        raise LumaError("--domain must be a hostname without scheme, path, or port")
    if not all(part and len(part) <= 63 for part in domain.split(".")):
        raise LumaError(f"invalid control domain: {value}")
    return domain


def _updated_config(
    config: LumaConfig, *, old: str, new: str
) -> tuple[Dict[str, Any], str, list[str], list[str]]:
    manager = config.find_node(role="nomad-manager") or config.default_manager()
    if not manager:
        raise LumaError("manager config has no node with the nomad-manager role")
    if str(manager.public_ip or "") not in {old, new}:
        raise LumaError(
            f"manager node {manager.name} publicIp is {manager.public_ip or 'missing'}, "
            f"not --old {old} or --new {new}"
        )

    raw = copy.deepcopy(config.raw)
    changes: list[str] = []
    node_raw = (raw.get("nodes") or {}).get(manager.name)
    if not isinstance(node_raw, dict):
        raise LumaError(f"manager node configuration is invalid: nodes.{manager.name}")
    if str(node_raw.get("publicIp") or node_raw.get("public_ip") or "") == old:
        node_raw.pop("public_ip", None)
        node_raw["publicIp"] = new
        changes.append(f"nodes.{manager.name}.publicIp")

    providers = raw.get("providers")
    dns_path = "providers.dns"
    dns = providers.get("dns") if isinstance(providers, dict) else None
    if not isinstance(dns, dict):
        dns_path = "dns"
        dns = raw.get("dns")
    if isinstance(dns, dict):
        edge_target = str(dns.get("edgeTarget") or "")
        if edge_target and edge_target not in {old, new}:
            raise LumaError(
                f"{dns_path}.edgeTarget is {edge_target}, not --old {old} or --new {new}"
            )
        if edge_target == old:
            dns["edgeTarget"] = new
            changes.append(f"{dns_path}.edgeTarget")

    allowed = {
        f"nodes.{manager.name}.publicIp",
        f"nodes.{manager.name}.public_ip",
        f"{dns_path}.edgeTarget",
    }
    unmanaged = sorted(path for path in _exact_value_paths(config.raw, old) if path not in allowed)
    return raw, manager.name, changes, unmanaged


def _exact_value_paths(value: Any, expected: str, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(_exact_value_paths(child, expected, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_exact_value_paths(child, expected, f"{prefix}[{index}]"))
    elif str(value) == expected:
        paths.append(prefix)
    return paths


def _cloudflare_credentials(
    config: LumaConfig, state: Mapping[str, object]
) -> tuple[str, str, str]:
    dns = config.dns
    if dns.get("provider") != "cloudflare":
        raise LumaError("manager IP recovery requires providers.dns.type: cloudflare")
    secrets = state.get("secrets") if isinstance(state.get("secrets"), Mapping) else {}
    token_env = str(dns.get("apiTokenEnv") or "CLOUDFLARE_API_TOKEN")
    zone_env = str(dns.get("zoneIdEnv") or "CLOUDFLARE_ZONE_ID")
    token = str(os.environ.get(token_env) or secrets.get(token_env) or "").strip()
    zone_id = str(
        os.environ.get(zone_env) or dns.get("zoneId") or secrets.get(zone_env) or ""
    ).strip()
    if not token:
        raise LumaError(
            f"missing Cloudflare API token in environment or manager control state: {token_env}"
        )
    if not zone_id:
        raise LumaError("missing Cloudflare zone id in manager configuration/control state")
    return token, zone_id, str(dns.get("zone") or "").strip().rstrip(".").lower()


def _cloudflare_a_records_for_ip(
    client: CloudflareClient, zone_id: str, old_ip: str
) -> list[Dict[str, Any]]:
    records: list[Dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {"type": "A", "content": old_ip, "page": page, "per_page": 100}
        )
        payload = client.request("GET", f"/zones/{zone_id}/dns_records?{query}")
        batch = payload.get("result") if isinstance(payload.get("result"), list) else []
        records.extend(
            dict(record)
            for record in batch
            if isinstance(record, dict)
            and str(record.get("type") or "").upper() == "A"
            and str(record.get("content") or "") == old_ip
        )
        info = payload.get("result_info") if isinstance(payload.get("result_info"), dict) else {}
        total_pages = int(info.get("total_pages") or 1)
        if page >= total_pages:
            break
        page += 1
    return records


def _patch_cloudflare_records(
    client: CloudflareClient,
    zone_id: str,
    records: list[Dict[str, Any]],
    new_ip: str,
    *,
    emit: Callable[[str], None],
) -> int:
    updated = 0
    for record in records:
        record_id = str(record.get("id") or "").strip()
        name = str(record.get("name") or "").strip()
        if not record_id:
            raise LumaError(f"Cloudflare A record has no id: {name or '<unnamed>'}")
        client.request(
            "PATCH", f"/zones/{zone_id}/dns_records/{record_id}", {"content": new_ip}
        )
        updated += 1
        emit(f"[ok] Cloudflare A record updated: {name} -> {new_ip}")
    return updated


def _public_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "name": str(record.get("name") or ""),
        "type": "A",
        "ttl": record.get("ttl"),
        "proxied": bool(record.get("proxied")),
    }


def _running_control_image() -> str:
    result = subprocess.run(
        ["nomad", "job", "inspect", "-json", "luma-control"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        return ""
    try:
        job = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    groups = job.get("TaskGroups") if isinstance(job, dict) else []
    for group in groups or []:
        for task in (group.get("Tasks") if isinstance(group, dict) else []) or []:
            if not isinstance(task, dict) or str(task.get("Name") or "") != "luma-control":
                continue
            config = task.get("Config") if isinstance(task.get("Config"), dict) else {}
            return str(config.get("image") or "").strip()
    return ""


def _probe_manager(domain: str, ip: str) -> None:
    for path in ("/v1/health", "/dashboard/"):
        result = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--fail",
                "--max-time",
                "20",
                "--noproxy",
                "*",
                "--resolve",
                f"{domain}:443:{ip}",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                f"https://{domain}{path}",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=25,
        )
        if result.returncode != 0 or not str(result.stdout).startswith("2"):
            detail = (result.stderr or result.stdout or "request failed").strip()
            raise LumaError(
                f"new manager IP preflight failed for https://{domain}{path} via {ip}: {detail}"
            )


def _probe_normal_domain(domain: str) -> bool:
    result = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--max-time",
            "20",
            "--noproxy",
            "*",
            "--output",
            "/dev/null",
            f"https://{domain}/v1/health",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=25,
    )
    return result.returncode == 0


def _install_manager_config(path: Path, raw: Dict[str, Any]) -> str:
    remote = LocalExecutor()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.pre-ip-change-{stamp}"
    remote.sudo(
        "set -euo pipefail; "
        f"test -f {shlex.quote(str(path))}; "
        f"install -m 600 {shlex.quote(str(path))} {shlex.quote(backup)}"
    )
    remote.write_secret(dump_yaml(raw), str(path), mode="644")
    return backup


def _flush_dns_cache() -> None:
    LocalExecutor().sudo_result(
        "if command -v resolvectl >/dev/null 2>&1; then resolvectl flush-caches || true; fi",
        timeout=15,
    )


def _emit_plan(plan: Mapping[str, Any], *, emit: Callable[[str], None]) -> None:
    emit("Manager IP change plan")
    emit(f"  Address: {plan['oldIp']} -> {plan['newIp']}")
    emit(f"  Domain: {plan['domain']}")
    emit(f"  Manager node: {plan['managerNode']}")
    emit(f"  Config: {plan['configPath']}")
    changes = plan.get("configChanges") or []
    emit(f"  Config fields: {', '.join(changes) if changes else 'already updated'}")
    records = plan.get("dnsRecords") or []
    emit(f"  Cloudflare A records: {len(records)}")
    for record in records:
        emit(
            f"    - {record.get('name')} (proxied={str(bool(record.get('proxied'))).lower()}, "
            f"ttl={record.get('ttl')})"
        )
    unmanaged = plan.get("unmanagedConfigPaths") or []
    if unmanaged:
        emit(
            "  Warning: old IP also appears in untyped fields left unchanged: "
            + ", ".join(unmanaged)
        )
    emit(f"  Reconcile image: {plan['controlImage']}")
    emit("  Preflight: new IP serves both /v1/health and /dashboard/ with valid TLS")
