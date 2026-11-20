"""Control-plane Addon Registry, discovery, configuration, and lifecycle."""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import yaml

from ..addons import (
    DOMAIN_RE,
    default_config_for_preset,
    default_preset_id,
    derive_origin_suffixes,
    ensure_auto_secrets,
    get_addon_spec,
    infer_config_from_deployment,
    load_addon_catalog,
    render_addon_manifests,
    validate_addon_config,
    validate_addon_id,
)
from ..errors import LumaError
from .state import load_auth_state, load_state, mutate_state, require_token

_PUBLIC_EXPOSURES = frozenset({"cn-edge", "node-edge", "external-edge", "tailscale-relay", "cloudflare-tunnel"})
_ENV_URL_OVERRIDES = {
    "builder": "LUMA_BUILD_URL",
    "eye": "LUMA_EYE_URL",
}


def _control_url(state: Mapping[str, Any]) -> str:
    domain = str(state.get("domain") or "").strip().rstrip(".")
    if domain and DOMAIN_RE.fullmatch(domain):
        return f"https://{domain}"
    return ""


def _deployment_record(state: Mapping[str, Any], stack: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    deployments = state.get("deployments")
    if not isinstance(deployments, Mapping):
        return "", None
    for kind in ("compose", "services"):
        records = deployments.get(kind)
        record = records.get(stack) if isinstance(records, Mapping) else None
        if isinstance(record, dict):
            return kind, record
    return "", None


def _deployment_active(record: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(record, Mapping):
        return False
    status = str(record.get("status") or "").strip().lower()
    if status in {"removed", "deleted"}:
        return False
    return bool(status == "active" or record.get("lastSuccessfulAt") or record.get("manifest"))


def _domain_from_deployment(kind: str, record: Mapping[str, Any], primary_service: str) -> str:
    manifest_text = str(record.get("manifest") or "")
    if not manifest_text.strip():
        return ""
    try:
        manifest = yaml.safe_load(manifest_text)
    except yaml.YAMLError:
        return ""
    if not isinstance(manifest, dict):
        return ""
    if kind == "compose":
        services = manifest.get("services")
        service = services.get(primary_service) if isinstance(services, dict) else None
    else:
        service = manifest
    if not isinstance(service, dict):
        return ""
    if str(service.get("exposure") or "").strip() not in _PUBLIC_EXPOSURES:
        return ""
    domain = str(service.get("domain") or "").strip().lower()
    if DOMAIN_RE.fullmatch(domain):
        return domain
    return ""


def _saved_addons(state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = state.get("addons")
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, Mapping)}


def _saved_addon_entry(state: Mapping[str, Any], addon_id: str) -> Dict[str, Any]:
    return dict(_saved_addons(state).get(addon_id) or {})


def resolve_addon_url(state: Mapping[str, Any], addon_id: str) -> str:
    """Resolve an addon's trusted public origin (or internal URL) without contacting it."""
    clean_id = validate_addon_id(addon_id)
    env_var = _ENV_URL_OVERRIDES.get(clean_id, "")
    if env_var:
        override = str(os.environ.get(env_var) or "").strip().rstrip("/")
        if override:
            parsed = urlsplit(override)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment:
                return override
            raise LumaError(f"invalid configured URL in {env_var}")

    catalog = load_addon_catalog()
    spec = catalog.get(clean_id)
    saved = _saved_addon_entry(state, clean_id)

    if spec and spec["auth"]["mode"] == "forward-auth":
        ctl = _control_url(state)
        console_path = str(spec["auth"].get("consolePath") or "/")
        return f"{ctl}{console_path}" if ctl else ""

    stack = str(saved.get("stack") or (spec["stack"] if spec else f"luma-{clean_id}"))
    primary = str(saved.get("primaryService") or (spec["primaryService"] if spec else clean_id))
    kind, record = _deployment_record(state, stack)
    if record and _deployment_active(record):
        domain = _domain_from_deployment(kind, record, primary)
        if domain:
            return f"https://{domain}"

    saved_url = str(saved.get("url") or "").strip().rstrip("/")
    if saved_url:
        parsed = urlsplit(saved_url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment:
            return saved_url

    saved_cfg = saved.get("config") if isinstance(saved.get("config"), Mapping) else {}
    domain = str(saved_cfg.get("domain") or "").strip().lower()
    if domain and DOMAIN_RE.fullmatch(domain):
        return f"https://{domain}"
    return ""


def resolve_addon_sso_target(state: Mapping[str, Any], addon_id: str) -> Dict[str, str]:
    """Return trusted ``{addonId, name, audience, callbackPath}`` for Control SSO."""
    clean_id = validate_addon_id(addon_id)
    catalog = load_addon_catalog()
    spec = catalog.get(clean_id)
    saved = _saved_addon_entry(state, clean_id)
    if spec is None and not saved:
        raise LumaError(f"addon {clean_id!r} is not registered")

    auth_mode = str(saved.get("authMode") or (spec["auth"]["mode"] if spec else "control-sso"))
    if auth_mode != "control-sso":
        raise LumaError(f"addon {clean_id!r} does not use Control browser SSO (auth mode: {auth_mode})")

    url = resolve_addon_url(state, clean_id)
    parsed = urlsplit(url)
    if not url or parsed.scheme != "https" or parsed.path or not parsed.hostname:
        raise LumaError(f"{(spec['name'] if spec else clean_id)} browser sign-in requires a configured HTTPS origin")

    callback_path = str(saved.get("callbackPath") or (spec["auth"].get("callbackPath") if spec else "/auth/callback") or "/auth/callback").strip()
    if not callback_path.startswith("/") or callback_path.startswith("//"):
        callback_path = "/auth/callback"
    name = str(saved.get("name") or (spec["name"] if spec else clean_id))
    return {
        "addonId": clean_id,
        "name": name,
        "audience": url,
        "callbackPath": callback_path,
    }


def _addon_view(state: Mapping[str, Any], spec: Mapping[str, Any]) -> Dict[str, Any]:
    addon_id = str(spec["id"])
    stack = str(spec["stack"])
    saved = _saved_addon_entry(state, addon_id)
    kind, record = _deployment_record(state, stack)
    installed = _deployment_active(record)

    sidecar_yaml = str(record.get("manifest") or "") if isinstance(record, Mapping) else ""
    compose_yaml = str(record.get("composeContent") or "") if isinstance(record, Mapping) else ""

    inferred_preset, inferred_config = infer_config_from_deployment(spec, sidecar_yaml, compose_yaml) if installed else (default_preset_id(spec), default_config_for_preset(spec))
    preset = str(saved.get("preset") or inferred_preset or default_preset_id(spec))
    merged_config = dict(inferred_config)
    saved_cfg = saved.get("config") if isinstance(saved.get("config"), Mapping) else {}
    for k, v in saved_cfg.items():
        if v is not None:
            merged_config[k] = v

    scoped_all = state.get("scopedSecrets") if isinstance(state.get("scopedSecrets"), Mapping) else {}
    scoped = scoped_all.get(stack) if isinstance(scoped_all.get(stack), Mapping) else {}

    secrets_status: Dict[str, bool] = {}
    for field in spec.get("configSchema") or []:
        if isinstance(field, Mapping) and field.get("type") == "secret" and field.get("env"):
            env_name = str(field["env"])
            secrets_status[env_name] = bool(str(scoped.get(env_name) or "").strip())
    for entry in spec.get("autoSecrets") or []:
        if isinstance(entry, Mapping) and entry.get("name"):
            env_name = str(entry["name"])
            secrets_status[env_name] = bool(str(scoped.get(env_name) or "").strip())
    if spec["auth"].get("controlUrlEnv"):
        env_name = str(spec["auth"]["controlUrlEnv"])
        secrets_status[env_name] = bool(str(scoped.get(env_name) or "").strip() or _control_url(state))

    try:
        url = resolve_addon_url(state, addon_id)
    except LumaError:
        url = ""

    console_path = str(spec["auth"].get("consolePath") or "/")
    if spec["auth"]["mode"] == "forward-auth":
        console_url = url
    elif url:
        console_url = url.rstrip("/") + (console_path if console_path.startswith("/") else "/" + console_path)
    else:
        console_url = ""

    sso_ready = False
    if spec["auth"]["mode"] == "control-sso":
        parsed = urlsplit(url)
        sso_ready = bool(url and parsed.scheme == "https" and not parsed.path and parsed.hostname)
    elif spec["auth"]["mode"] == "forward-auth":
        sso_ready = bool(_control_url(state))

    has_saved = bool(saved)
    status = "running" if installed else ("configured" if has_saved else "not_installed")
    default_images = spec.get("defaultImages") if isinstance(spec.get("defaultImages"), Mapping) else {}
    can_apply_in_place = bool((installed and compose_yaml.strip()) or default_images)

    return {
        "id": addon_id,
        "name": spec["name"],
        "category": spec["category"],
        "description": spec["description"],
        "stack": stack,
        "primaryService": spec["primaryService"],
        "sourceDir": spec["sourceDir"],
        "installed": installed,
        "configured": has_saved or installed,
        "autoDiscovered": installed and not has_saved,
        "status": status,
        "preset": preset,
        "url": url,
        "consoleUrl": console_url,
        "auth": spec["auth"],
        "ssoReady": sso_ready,
        "ui": spec["ui"],
        "uiCapabilities": list(spec.get("uiCapabilities") or []),
        "defaultImages": dict(default_images),
        "presets": spec["presets"],
        "configSchema": spec["configSchema"],
        "autoSecrets": spec["autoSecrets"],
        "config": merged_config,
        "secretsStatus": secrets_status,
        "canApplyInPlace": can_apply_in_place,
        "deploymentKind": kind if installed else "",
        "updatedAt": int(saved.get("updatedAt") or 0),
    }


def _cluster_regions_and_nodes(state: Mapping[str, Any]) -> Tuple[set[str], set[str]]:
    regions_map = state.get("regions") if isinstance(state.get("regions"), Mapping) else {}
    regions = {"cn", "global", "home"} | {str(k) for k in regions_map.keys()}
    nodes_map = state.get("nodes") if isinstance(state.get("nodes"), Mapping) else {}
    nodes = {str(k) for k in nodes_map.keys()}
    return regions, nodes


def _available_credentials(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Secret-free catalog of saved Control registry & Git credentials for addon reuse."""
    registries = state.get("registries") if isinstance(state.get("registries"), Mapping) else {}
    reg_items = [
        {
            "host": str(host),
            "username": str(item.get("username") or ""),
            "configured": bool(item.get("username") and item.get("password")),
        }
        for host, item in sorted(registries.items())
        if isinstance(item, Mapping)
    ]
    git_providers = state.get("gitProviders") if isinstance(state.get("gitProviders"), Mapping) else {}
    git_items = [
        {
            "id": str(pid),
            "type": str(item.get("type") or "").strip().lower(),
            "account": str(item.get("account") or str(pid).split(":", 1)[-1]),
            "baseUrl": str(item.get("baseUrl") or ""),
            "username": str(item.get("username") or ""),
            "configured": bool(item.get("token")),
        }
        for pid, item in sorted(git_providers.items())
        if isinstance(item, Mapping)
    ]
    build_cfg = state.get("build") if isinstance(state.get("build"), Mapping) else {}
    return {
        "registries": reg_items,
        "gitProviders": git_items,
        "defaultBuildRegistry": {
            "registryHost": str(build_cfg.get("registryHost") or ""),
            "pushHost": str(build_cfg.get("pushHost") or ""),
        },
    }


def addon_capabilities_summary(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Compact, zero-I/O summary of installed addons and their active UI capabilities."""
    catalog = load_addon_catalog()
    active: List[str] = []
    capabilities: set[str] = set()
    urls: Dict[str, str] = {}
    items: Dict[str, Dict[str, Any]] = {}
    for addon_id, spec in catalog.items():
        stack = str(spec["stack"])
        _kind, record = _deployment_record(state, stack)
        installed = _deployment_active(record)
        saved = _saved_addon_entry(state, addon_id)
        try:
            url = resolve_addon_url(state, addon_id)
        except LumaError:
            url = ""
        console_path = str(spec["auth"].get("consolePath") or "/")
        if spec["auth"]["mode"] == "forward-auth":
            console_url = url
        elif url:
            console_url = url.rstrip("/") + (console_path if console_path.startswith("/") else "/" + console_path)
        else:
            console_url = ""
        caps = [str(c).strip() for c in (spec.get("uiCapabilities") or []) if str(c or "").strip()]
        items[addon_id] = {
            "id": addon_id,
            "name": str(spec["name"]),
            "installed": installed,
            "status": "running" if installed else ("configured" if saved else "not_installed"),
            "consoleUrl": console_url,
            "capabilities": caps,
        }
        if installed:
            active.append(addon_id)
            capabilities.update(caps)
            if console_url:
                urls[addon_id] = console_url
    return {
        "known": True,
        "active": sorted(active),
        "capabilities": sorted(capabilities),
        "urls": urls,
        "items": items,
    }


def handle_addons_summary(token: str) -> Dict[str, Any]:
    state = load_auth_state()
    require_token(state, token, token_type="deploy")
    return addon_capabilities_summary(state)


def handle_addons_list(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    catalog = load_addon_catalog()
    items = [_addon_view(state, spec) for spec in catalog.values()]
    regions, nodes = _cluster_regions_and_nodes(state)
    return {
        "addons": items,
        "controlUrl": _control_url(state),
        "availableRegions": sorted(regions),
        "availableNodes": sorted(nodes),
        "availableCredentials": _available_credentials(state),
    }


def handle_addon_get(token: str, addon_id: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    spec = get_addon_spec(addon_id)
    regions, nodes = _cluster_regions_and_nodes(state)
    return {
        "addon": _addon_view(state, spec),
        "controlUrl": _control_url(state),
        "availableRegions": sorted(regions),
        "availableNodes": sorted(nodes),
        "availableCredentials": _available_credentials(state),
    }


def handle_addon_configure(token: str, addon_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and persist addon config + auto-generated/derived scoped secrets."""
    state = load_state()
    require_token(state, token, token_type="deploy")
    spec = get_addon_spec(addon_id)
    clean_id = spec["id"]
    stack = spec["stack"]

    regions, nodes = _cluster_regions_and_nodes(state)
    current_view = _addon_view(state, spec)
    raw_config = body.get("config") if isinstance(body.get("config"), Mapping) else {}
    raw_secrets = body.get("secrets") if isinstance(body.get("secrets"), Mapping) else {}
    preset_req = str(body.get("preset") or current_view["preset"] or default_preset_id(spec)).strip()
    rotate_secrets = bool(body.get("rotateSecrets", False))
    require_complete = bool(body.get("requireComplete", False))

    resolved_preset, clean_config, secret_updates = validate_addon_config(
        spec,
        raw_config,
        incoming_secrets=raw_secrets,
        existing_config=current_view["config"],
        preset_id=preset_req,
        regions=regions if regions else None,
        nodes=nodes if nodes else None,
        require_complete=require_complete,
    )

    scoped_all = state.get("scopedSecrets") if isinstance(state.get("scopedSecrets"), Mapping) else {}
    existing_scoped = dict(scoped_all.get(stack) if isinstance(scoped_all.get(stack), Mapping) else {})

    # 1. Auto-generate missing internal secrets (never overwriting existing unless rotate=True)
    generated = ensure_auto_secrets(spec, existing_scoped, rotate=rotate_secrets)
    merged_secrets = dict(existing_scoped)
    merged_secrets.update(generated)
    merged_secrets.update(secret_updates)

    # 2. Auto-wire derived Control / public URL secrets
    ctl_url = _control_url(state)
    control_url_env = str(spec["auth"].get("controlUrlEnv") or "").strip()
    if control_url_env and ctl_url:
        merged_secrets[control_url_env] = ctl_url

    if spec["auth"].get("needsControlDeployToken"):
        deploy_tok = str(state.get("deployToken") or "").strip()
        if deploy_tok and not str(merged_secrets.get("LUMA_DEPLOY_TOKEN") or "").strip():
            merged_secrets["LUMA_DEPLOY_TOKEN"] = deploy_tok

    domain = str(clean_config.get("domain") or "").strip().lower()
    public_url_env = str(spec["auth"].get("publicUrlEnv") or "").strip()
    if public_url_env and domain:
        merged_secrets[public_url_env] = f"https://{domain}"

    if clean_id == "eye" and domain and not str(clean_config.get("autoOriginSuffixes") or "").strip():
        clean_config["autoOriginSuffixes"] = derive_origin_suffixes(domain)

    # Also inherit saved Control registry / Git credentials when requested for builder
    # so operators never have to retype registry passwords or Git tokens.
    inherited: List[str] = []
    if clean_id == "builder":
        inherit_obj = body.get("inherit") if isinstance(body.get("inherit"), Mapping) else {}
        inherit_reg = str(
            inherit_obj.get("registryHost")
            or body.get("inheritRegistryHost")
            or ""
        ).strip()
        if inherit_reg:
            registries = state.get("registries") if isinstance(state.get("registries"), Mapping) else {}
            reg_cred = registries.get(inherit_reg)
            if not isinstance(reg_cred, Mapping):
                raise LumaError(f"saved Control registry credential not found: {inherit_reg}")
            clean_config["registryHost"] = inherit_reg
            build_cfg = state.get("build") if isinstance(state.get("build"), Mapping) else {}
            if not clean_config.get("pushHost") and str(build_cfg.get("registryHost") or "").strip() == inherit_reg and str(build_cfg.get("pushHost") or "").strip():
                clean_config["pushHost"] = str(build_cfg["pushHost"]).strip()
            if reg_cred.get("username") and not str(raw_config.get("registryUsername") or "").strip():
                clean_config["registryUsername"] = str(reg_cred["username"]).strip()
            if reg_cred.get("password") and "LUMA_BUILD_REGISTRY_PASSWORD" not in secret_updates:
                merged_secrets["LUMA_BUILD_REGISTRY_PASSWORD"] = str(reg_cred["password"])
            inherited.append(f"registry:{inherit_reg}")

        raw_git_ids = inherit_obj.get("gitProviderIds")
        if raw_git_ids is None:
            raw_git_ids = body.get("inheritGitProviderIds")
        if isinstance(raw_git_ids, (list, tuple)):
            git_providers = state.get("gitProviders") if isinstance(state.get("gitProviders"), Mapping) else {}
            for raw_pid in raw_git_ids:
                pid = str(raw_pid or "").strip()
                if not pid:
                    continue
                provider = git_providers.get(pid)
                if not isinstance(provider, Mapping):
                    raise LumaError(f"saved Control Git provider not found: {pid}")
                ptype = str(provider.get("type") or "").strip().lower()
                ptoken = str(provider.get("token") or "").strip()
                if ptype == "github" and ptoken and "GITHUB_TOKEN" not in secret_updates:
                    merged_secrets["GITHUB_TOKEN"] = ptoken
                    inherited.append(f"git:{pid}")
                elif ptype == "gitea":
                    base_url = str(provider.get("cloneBaseUrl") or provider.get("baseUrl") or "").strip()
                    parsed_git = urlsplit(base_url if "://" in base_url else f"https://{base_url}") if base_url else None
                    gitea_host = parsed_git.netloc if parsed_git and parsed_git.netloc else ""
                    if gitea_host and not str(raw_config.get("giteaHost") or "").strip():
                        clean_config["giteaHost"] = gitea_host
                    gitea_user = str(provider.get("username") or provider.get("account") or "").strip()
                    if gitea_user and not str(raw_config.get("giteaUsername") or "").strip():
                        clean_config["giteaUsername"] = gitea_user
                    if ptoken and "GITEA_TOKEN" not in secret_updates:
                        merged_secrets["GITEA_TOKEN"] = ptoken
                    inherited.append(f"git:{pid}")

        # Mirror non-empty builder registry/gitea config values into builder's scopedSecrets
        # so existing compose templates referencing ${VAR} continue working seamlessly.
        for field in spec.get("configSchema") or []:
            env_name = str(field.get("env") or "").strip()
            key = str(field.get("key") or "")
            if not env_name or field.get("type") == "secret":
                continue
            val = clean_config.get(key)
            if val not in (None, ""):
                merged_secrets[env_name] = str(val)
        if clean_config.get("registryHost") and not clean_config.get("pushHost"):
            merged_secrets["LUMA_BUILD_PUSH_HOST"] = str(clean_config["registryHost"])

    # Determine canonical URL for SSO audience
    if spec["auth"]["mode"] == "forward-auth":
        addon_url = f"{ctl_url}{spec['auth'].get('consolePath') or '/grafana'}" if ctl_url else ""
    else:
        addon_url = f"https://{domain}" if domain else current_view["url"]

    previous_url = current_view["url"]
    now = int(time.time())
    addon_record = {
        "id": clean_id,
        "name": spec["name"],
        "stack": stack,
        "primaryService": spec["primaryService"],
        "preset": resolved_preset,
        "url": addon_url,
        "authMode": spec["auth"]["mode"],
        "callbackPath": spec["auth"]["callbackPath"],
        "consolePath": spec["auth"]["consolePath"],
        "config": clean_config,
        "updatedAt": now,
    }

    def mutate(persisted: Dict[str, Any]) -> None:
        addons_map = persisted.setdefault("addons", {})
        if not isinstance(addons_map, dict):
            addons_map = {}
            persisted["addons"] = addons_map
        addons_map[clean_id] = addon_record

        persisted_scoped = persisted.setdefault("scopedSecrets", {})
        if not isinstance(persisted_scoped, dict):
            persisted_scoped = {}
            persisted["scopedSecrets"] = persisted_scoped
        scope_bucket = persisted_scoped.setdefault(stack, {})
        if not isinstance(scope_bucket, dict):
            scope_bucket = {}
            persisted_scoped[stack] = scope_bucket
        for k, v in merged_secrets.items():
            if v is not None and str(v) != "":
                scope_bucket[str(k)] = str(v)

    mutate_state(mutate)

    # If the SSO audience changed, revoke old addon SSO sessions bound to the prior domain.
    if previous_url and addon_url and previous_url != addon_url and spec["auth"]["mode"] == "control-sso":
        from . import addon_auth
        addon_auth.revoke_addon(clean_id)

    updated_state = load_state()
    return {
        "addon": _addon_view(updated_state, spec),
        "generatedSecrets": sorted(generated.keys()),
        "updatedSecrets": sorted(secret_updates.keys()),
        "inheritedCredentials": inherited,
    }


def handle_addon_render(token: str, addon_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    spec = get_addon_spec(addon_id)
    view = _addon_view(state, spec)
    stack = spec["stack"]
    _kind, record = _deployment_record(state, stack)
    existing_sidecar = str(record.get("manifest") or "") if isinstance(record, Mapping) else ""
    existing_compose = str(record.get("composeContent") or "") if isinstance(record, Mapping) else ""
    scoped_all = state.get("scopedSecrets") if isinstance(state.get("scopedSecrets"), Mapping) else {}
    scoped = scoped_all.get(stack) if isinstance(scoped_all.get(stack), Mapping) else {}

    image_overrides = body.get("images") if isinstance(body.get("images"), Mapping) else None
    for_source_build = bool(body.get("forSourceBuild", False))
    rendered = render_addon_manifests(
        spec,
        view["config"],
        preset_id=view["preset"],
        control_url=_control_url(state),
        scoped_secrets=scoped,
        existing_compose_yaml=existing_compose,
        existing_sidecar_yaml=existing_sidecar,
        image_overrides=image_overrides,
        for_source_build=for_source_build,
    )
    return {"addon": view, "rendered": rendered}


def prepare_addon_apply_body(token: str, addon_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    """Configure (if provided) and return a Compose deployment request body."""
    if (
        body.get("config")
        or body.get("secrets")
        or body.get("preset")
        or body.get("rotateSecrets")
        or body.get("inherit")
        or body.get("inheritRegistryHost")
        or body.get("inheritGitProviderIds")
    ):
        handle_addon_configure(token, addon_id, {**dict(body), "requireComplete": True})
    else:
        # Ensure auto-secrets and derived Control URL exist even on direct apply
        handle_addon_configure(token, addon_id, {"requireComplete": True})

    rendered_resp = handle_addon_render(token, addon_id, {
        "images": body.get("images"),
        "forSourceBuild": False,
    })
    rendered = rendered_resp["rendered"]
    return {
        "manifest": rendered["sidecarYaml"],
        "composeContent": rendered["composeYaml"],
        "composeName": rendered["composeFilename"],
        "sourceName": f"addon:{validate_addon_id(addon_id)}",
        "origin": str(body.get("origin") or "addon"),
    }


def handle_addon_uninstall(token: str, addon_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    from . import addon_auth, server

    state = load_state()
    require_token(state, token, token_type="deploy")
    spec = get_addon_spec(addon_id)
    clean_id = spec["id"]
    stack = spec["stack"]
    purge_secrets = bool(body.get("purgeSecrets", False))
    delete_storage = bool(body.get("deleteStorage", False))

    _kind, record = _deployment_record(state, stack)
    remove_result: Optional[Dict[str, Any]] = None
    if record and _deployment_active(record):
        remove_result = server.handle_service_remove(token, {
            "name": stack,
            "deleteStorage": delete_storage,
        })

    addon_auth.revoke_addon(clean_id)

    def mutate(persisted: Dict[str, Any]) -> None:
        addons_map = persisted.get("addons")
        if isinstance(addons_map, dict):
            addons_map.pop(clean_id, None)
        if purge_secrets:
            scoped = persisted.get("scopedSecrets")
            if isinstance(scoped, dict):
                scoped.pop(stack, None)

    mutate_state(mutate)
    return {
        "addonId": clean_id,
        "stack": stack,
        "uninstalled": True,
        "purgedSecrets": purge_secrets,
        "deploymentRemoved": remove_result is not None,
    }


def addon_login_info(addon_id: str) -> Dict[str, str]:
    """Minimal display info for ``/dashboard/addon-login`` (no secrets)."""
    target = resolve_addon_sso_target(load_auth_state(), addon_id)
    return {
        "addonId": target["addonId"],
        "name": target["name"],
    }


_EYE_CACHE_LOCK = threading.Lock()
_EYE_PORTFOLIO_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_EYE_CACHE_TTL_SECONDS = 20.0
_EYE_VALID_SINCE = frozenset({"1h", "6h", "24h", "7d", "30d"})
_SLUG_NORM_RE = re.compile(r"[^a-z0-9]+")


def _eye_fetch_json(base_url: str, path: str, bearer: str, *, timeout: float = 4.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {bearer}"},
        method="GET",
    )
    insecure = str(os.environ.get("LUMA_EYE_INSECURE") or "").strip().lower() in {"1", "true", "yes"}
    ctx = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read().decode("utf-8")
        parsed = json.loads(raw) if raw.strip() else {}
        return parsed if isinstance(parsed, dict) else {}


def _eye_portfolio_cached(base_url: str, bearer: str, since: str) -> Dict[str, Any]:
    now = time.monotonic()
    key = (base_url, since)
    with _EYE_CACHE_LOCK:
        cached = _EYE_PORTFOLIO_CACHE.get(key)
        if cached and now - cached[0] < _EYE_CACHE_TTL_SECONDS:
            return cached[1]
    qs = urllib.parse.urlencode({"since": since})
    data = _eye_fetch_json(base_url, f"/api/v1/query/portfolio?{qs}", bearer)
    with _EYE_CACHE_LOCK:
        if len(_EYE_PORTFOLIO_CACHE) >= 32:
            _EYE_PORTFOLIO_CACHE.clear()
        _EYE_PORTFOLIO_CACHE[key] = (now, data)
    return data


def _app_domains(state: Mapping[str, Any], app: str) -> set[str]:
    """Extract configured public hostnames for a Luma application from state."""
    domains: set[str] = set()
    kind, record = _deployment_record(state, app)
    if not isinstance(record, Mapping):
        return domains
    manifest_text = str(record.get("manifest") or "")
    if not manifest_text.strip():
        return domains
    try:
        manifest = yaml.safe_load(manifest_text)
    except yaml.YAMLError:
        return domains
    if not isinstance(manifest, dict):
        return domains
    candidates: List[Mapping[str, Any]] = []
    if kind == "compose" and isinstance(manifest.get("services"), dict):
        candidates.extend(v for v in manifest["services"].values() if isinstance(v, Mapping))
    else:
        candidates.append(manifest)
    for item in candidates:
        for key in ("domain", "domains"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                domains.add(val.strip().lower())
            elif isinstance(val, list):
                for entry in val:
                    if isinstance(entry, str) and entry.strip():
                        domains.add(entry.strip().lower())
    return domains


def _normalize_eye_project_entry(item: Mapping[str, Any], base_url: str) -> Dict[str, Any]:
    proj = item.get("project") if isinstance(item.get("project"), Mapping) else {}
    slug = str(proj.get("slug") or "").strip()
    console_base = f"{base_url.rstrip('/')}/console/"
    return {
        "slug": slug,
        "name": str(proj.get("name") or slug),
        "publicKey": str(proj.get("public_key") or ""),
        "allowedOrigins": [str(o) for o in (proj.get("allowed_origins") or []) if str(o or "").strip()],
        "consoleUrl": f"{console_base}?project={urllib.parse.quote(slug)}" if slug else console_base,
        "uniqueVisitors": int(item.get("unique_visitors") or 0),
        "pageviews": int(item.get("pageviews") or 0),
        "errorCount": int(item.get("error_count") or 0),
        "crashFreeRate": round(float(item.get("crash_free_rate") if item.get("crash_free_rate") is not None else 100.0), 2),
    }


def handle_addon_eye_portfolio(token: str, *, since: str = "24h") -> Dict[str, Any]:
    """Proxy read-only Luma Eye portfolio summary for the Control Dashboard."""
    state = load_auth_state()
    require_token(state, token, token_type="deploy")
    window = since if since in _EYE_VALID_SINCE else "24h"
    _kind, record = _deployment_record(state, "luma-eye")
    installed = _deployment_active(record)
    try:
        eye_url = resolve_addon_url(state, "eye")
    except LumaError:
        eye_url = ""
    if not installed or not eye_url:
        return {"available": False, "installed": installed, "reason": "not_installed", "consoleUrl": "", "since": window, "projects": []}
    bearer = str(state.get("deployToken") or token or "").strip()
    console_url = f"{eye_url.rstrip('/')}/console/"
    try:
        raw = _eye_portfolio_cached(eye_url, bearer, window)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"available": False, "installed": True, "reason": "unreachable", "error": str(exc), "consoleUrl": console_url, "since": window, "projects": []}
    raw_items = raw.get("projects") if isinstance(raw.get("projects"), list) else []
    projects = [_normalize_eye_project_entry(item, eye_url) for item in raw_items if isinstance(item, Mapping)]
    totals = {
        "projects": len(projects),
        "uniqueVisitors": sum(p["uniqueVisitors"] for p in projects),
        "pageviews": sum(p["pageviews"] for p in projects),
        "errorCount": sum(p["errorCount"] for p in projects),
    }
    return {
        "available": True,
        "installed": True,
        "consoleUrl": console_url,
        "eyeUrl": eye_url,
        "since": window,
        "totals": totals,
        "projects": projects,
    }


def _match_eye_project(projects: List[Dict[str, Any]], app: str, domains: set[str]) -> Optional[Dict[str, Any]]:
    app_low = app.strip().lower()
    app_norm = _SLUG_NORM_RE.sub("", app_low)
    for proj in projects:
        if proj["slug"].lower() == app_low:
            return proj
    if app_norm:
        for proj in projects:
            if _SLUG_NORM_RE.sub("", proj["slug"].lower()) == app_norm:
                return proj
    if domains:
        for proj in projects:
            for origin in proj["allowedOrigins"]:
                host = (urlsplit(origin).hostname or "").lower()
                if host and (host in domains or (host.startswith("www.") and host[4:] in domains) or f"www.{host}" in domains):
                    return proj
    return None


def handle_addon_eye_app(token: str, app: str, *, since: str = "24h") -> Dict[str, Any]:
    """Match a Luma application to its Luma Eye project and return live RUM stats."""
    portfolio = handle_addon_eye_portfolio(token, since=since)
    clean_app = str(app or "").strip()
    if not portfolio.get("available") or not clean_app:
        return {
            **{k: v for k, v in portfolio.items() if k != "projects"},
            "app": clean_app,
            "matched": False,
            "project": None,
            "projectsCount": len(portfolio.get("projects") or []),
        }
    state = load_auth_state()
    domains = _app_domains(state, clean_app)
    matched = _match_eye_project(portfolio["projects"], clean_app, domains)
    return {
        "available": True,
        "installed": True,
        "app": clean_app,
        "since": portfolio["since"],
        "consoleUrl": portfolio["consoleUrl"],
        "eyeUrl": portfolio.get("eyeUrl", ""),
        "matched": matched is not None,
        "project": matched,
        "projectsCount": len(portfolio["projects"]),
    }
