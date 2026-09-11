"""Derive deployment prerequisites from an already parsed Luma service."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .registry import registry_host_from_image
from .errors import LumaError

SECRET_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SUPPORTED_CAPABILITIES = frozenset({"cloudflare", "tailscale", "egress", "registry"})
SUPPORTED_INIT_ACTIONS = frozenset({"cloudflare-dns", "cloudflare-tunnel", "tailscale-node", "egress", "registry", "secrets"})


def validate_requirement_block(requirements: Any, *, label: str = "requirements") -> dict[str, Any]:
    """Validate and copy the declarative dependency contract.

    Unknown capabilities/actions are rejected at parse time so a typo cannot
    silently turn a required integration into an ignored annotation.
    """
    if requirements is None:
        return {}
    if not isinstance(requirements, Mapping):
        raise LumaError(f"{label} must be a mapping")
    allowed_fields = {"capabilities", "secrets", "init", "notes"}
    unknown_fields = sorted(str(key) for key in requirements if str(key) not in allowed_fields)
    if unknown_fields:
        raise LumaError(f"unsupported {label} field(s): {', '.join(unknown_fields)}")
    result = dict(requirements)
    for key in allowed_fields:
        values = result.get(key) or []
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise LumaError(f"{label}.{key} must be a list of non-empty strings")
        result[key] = [str(value).strip() for value in values]
    unknown_capabilities = sorted(set(result["capabilities"]) - SUPPORTED_CAPABILITIES)
    if unknown_capabilities:
        raise LumaError(f"unsupported {label}.capabilities value(s): {', '.join(unknown_capabilities)}")
    unknown_actions = sorted(set(result["init"]) - SUPPORTED_INIT_ACTIONS)
    if unknown_actions:
        raise LumaError(f"unsupported {label}.init action(s): {', '.join(unknown_actions)}")
    return result


def _secret_names(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(SECRET_REF.findall(value))
    elif isinstance(value, Mapping):
        for nested in value.values():
            found.update(_secret_names(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found.update(_secret_names(nested))
    return found


def _ready_nodes(state: Mapping[str, Any], region: str) -> list[Mapping[str, Any]]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), Mapping) else {}
    result: list[Mapping[str, Any]] = []
    for record in nodes.values():
        if not isinstance(record, Mapping):
            continue
        if str(record.get("region") or "") != region:
            continue
        if str(record.get("state") or record.get("status") or "ready").lower() in {"drain", "down", "failed"}:
            continue
        result.append(record)
    return result


def service_requirements(
    service: Any,
    state: Mapping[str, Any],
    config: Any,
    *,
    resolved_secrets: Mapping[str, Any] | None = None,
    require_verified: bool = False,
) -> dict[str, Any]:
    """Return stable, secret-free prerequisite information for preview UIs."""
    secrets = dict(state.get("secrets") if isinstance(state.get("secrets"), Mapping) else {})
    if resolved_secrets:
        secrets.update({str(key): value for key, value in resolved_secrets.items()})
    env = getattr(service, "environment", {}) or {}
    explicit = validate_requirement_block(getattr(service, "requirements", {}) or {})
    explicit_secrets = explicit.get("secrets") if isinstance(explicit, Mapping) else []
    required_secrets = sorted(_secret_names(env) | {str(value).strip() for value in (explicit_secrets or []) if str(value).strip()})
    checks: list[dict[str, Any]] = []

    def add(kind: str, required: bool, configured: bool, detail: str, *, missing: list[str] | None = None, explicit: bool = False) -> None:
        checks.append({
            "kind": kind,
            "required": required,
            "status": "ready" if configured else ("missing" if required else "skipped"),
            "detail": detail,
            "missing": missing or [],
            "explicit": explicit,
        })

    region = str(getattr(service, "region", "") or "")
    region_nodes = _ready_nodes(state, region)
    add("region-capacity", True, bool(region_nodes), f"{len(region_nodes)} ready node(s) in region {region}", missing=[f"node in region {region}"] if not region_nodes else [])

    exposure = str(getattr(service, "exposure", "none") or "none")
    dns_provider = str(getattr(config, "dns", {}).get("provider") or "")
    explicit_capabilities = {str(value).strip() for value in (explicit.get("capabilities") or [])} if isinstance(explicit, Mapping) else set()
    needs_cloudflare = exposure in {"cn-edge", "external-edge", "cloudflare-tunnel"} or "cloudflare" in explicit_capabilities
    cloudflare_token = str(secrets.get(str(getattr(config, "dns", {}).get("apiTokenEnv", "CLOUDFLARE_API_TOKEN"))) or "")
    setup_checks = state.get("setupChecks") if isinstance(state.get("setupChecks"), Mapping) else {}
    setup_check_items = setup_checks.get("checks") if isinstance(setup_checks, Mapping) else {}
    cloudflare_check = setup_check_items.get("cloudflare") if isinstance(setup_check_items, Mapping) else {}
    verified_cloudflare = isinstance(cloudflare_check, Mapping) and str(cloudflare_check.get("status") or "") == "ready"
    cloudflare_configured = dns_provider == "cloudflare" and bool(cloudflare_token)
    cloudflare_ready = cloudflare_configured and (verified_cloudflare or not require_verified)
    cloudflare_missing: list[str] = []
    if needs_cloudflare and not cloudflare_configured:
        cloudflare_missing = ["providers.dns.provider=cloudflare", "CLOUDFLARE_API_TOKEN"]
    elif needs_cloudflare and require_verified and not verified_cloudflare:
        cloudflare_missing = ["run Dashboard setup check for Cloudflare"]
    add("cloudflare", needs_cloudflare, cloudflare_ready, "Cloudflare DNS provider and token available" if not require_verified or verified_cloudflare else "Cloudflare configuration is not verified", missing=cloudflare_missing, explicit="cloudflare" in explicit_capabilities)

    needs_tailnet = exposure == "tailscale-relay" or region == "home" or "tailscale" in explicit_capabilities
    tailnet_ready = any(bool(record.get("tailscaleIP") or record.get("tailscaleName")) for record in region_nodes)
    add("tailscale", needs_tailnet, tailnet_ready, "A node in the target region has a Tailscale address", missing=["Tailscale-connected node"] if needs_tailnet and not tailnet_ready else [], explicit="tailscale" in explicit_capabilities)

    needs_egress = bool(getattr(service, "proxy", False)) or "egress" in explicit_capabilities
    egress_ready = bool(str(secrets.get("EGRESS_SUBSCRIPTION_URL") or "").strip())
    egress_check = setup_check_items.get("egress") if isinstance(setup_check_items, Mapping) else {}
    verified_egress = isinstance(egress_check, Mapping) and str(egress_check.get("status") or "") == "ready"
    egress_ready = egress_ready and (verified_egress or not require_verified)
    egress_missing: list[str] = []
    if needs_egress and not str(secrets.get("EGRESS_SUBSCRIPTION_URL") or "").strip():
        egress_missing = ["EGRESS_SUBSCRIPTION_URL"]
    elif needs_egress and require_verified and not verified_egress:
        egress_missing = ["run Dashboard setup check for Egress"]
    add("egress", needs_egress, egress_ready, "Egress subscription is available" if not require_verified or verified_egress else "Egress subscription is not verified", missing=egress_missing, explicit="egress" in explicit_capabilities)

    secret_missing = [name for name in required_secrets if name not in secrets]
    add("secrets", bool(required_secrets), not secret_missing, "Referenced service secrets are present", missing=secret_missing, explicit=bool(explicit_secrets))

    image = str(getattr(service, "image", "") or "")
    registry = registry_host_from_image(image) if image else ""
    registry_auth = state.get("registries") if isinstance(state.get("registries"), Mapping) else {}
    public_registries = {"docker.io", "ghcr.io", "quay.io", "gcr.io", "registry-1.docker.io"}
    private_registry_missing = bool(
        ("registry" in explicit_capabilities or (registry and registry not in public_registries))
        and registry not in registry_auth
    )
    add("registry", private_registry_missing, not private_registry_missing, f"Image registry {registry or 'default'} is available", missing=[f"registry credential for {registry}"] if private_registry_missing else [], explicit="registry" in explicit_capabilities)

    blocking = [item for item in checks if item["required"] and item["status"] != "ready"]
    return {
        "ready": not blocking,
        "checks": checks,
        "requiredSecrets": required_secrets,
        "init": explicit.get("init") if isinstance(explicit, Mapping) else [],
        "notes": explicit.get("notes") if isinstance(explicit, Mapping) else [],
    }


def require_service_requirements(requirements: Mapping[str, Any], *, component: str, explicit_only: bool = False) -> None:
    failures = [item for item in requirements.get("checks") or [] if item.get("required") and item.get("status") != "ready" and (not explicit_only or item.get("explicit"))]
    if not failures:
        return
    details = []
    for item in failures:
        missing = ", ".join(str(value) for value in item.get("missing") or [])
        details.append(f"{item.get('kind')}: {item.get('detail') or missing or 'not ready'}")
    raise LumaError(f"deployment prerequisites are not ready for {component}: " + "; ".join(details))


def initialize_service_requirements(requirements: Mapping[str, Any], *, component: str) -> list[str]:
    """Validate the declarative init plan before deployment side effects.

    The actual provider operations remain owned by the deployment steps (DNS
    sync, image resolution, route writing). This function makes those actions
    explicit and rejects unknown init names instead of silently ignoring them.
    """
    checks = {str(item.get("kind")): item for item in requirements.get("checks") or []}
    actions = requirements.get("init") or []
    if not isinstance(actions, list):
        raise LumaError(f"requirements.init for {component} must be a list")
    action_kinds = {
        "cloudflare-dns": "cloudflare",
        "cloudflare-tunnel": "cloudflare",
        "tailscale-node": "tailscale",
        "egress": "egress",
        "registry": "registry",
        "secrets": "secrets",
    }
    completed: list[str] = []
    for raw_action in actions:
        action = str(raw_action).strip()
        if action not in action_kinds:
            raise LumaError(f"unsupported requirements.init action for {component}: {action}")
        check = checks.get(action_kinds[action])
        if check and check.get("status") in {"missing", "error"}:
            detail = ", ".join(str(value) for value in check.get("missing") or []) or str(check.get("detail") or "not ready")
            raise LumaError(f"initialization {action} is not ready for {component}: {detail}")
        completed.append(action)
    return completed
