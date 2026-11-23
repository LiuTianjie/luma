"""Declarative Luma Addon catalog, config validation, and manifest rendering.

Official addons (``builder``, ``eye``, ``observe``) ship their declarative specs
in ``luma/assets/addons/*.yaml``. External third-party addons can also supply
their own ``addon.yaml`` following the same ``luma.addon/v1`` contract.
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

import yaml

from .errors import LumaError

ADDON_API_VERSION = "luma.addon/v1"
ADDON_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
OFFICIAL_ORDER = ("dashboard", "builder", "eye", "observe")
VALID_EXPOSURES = frozenset({"cn-edge", "node-edge", "external-edge", "tailscale-relay", "cloudflare-tunnel", "none"})
VALID_ADDON_KINDS = frozenset({"compose", "static-bundle"})


def addons_assets_dir() -> Path:
    return Path(__file__).resolve().parent / "assets" / "addons"


def validate_addon_id(addon_id: str) -> str:
    clean = str(addon_id or "").strip().lower()
    if not ADDON_ID_RE.fullmatch(clean):
        raise LumaError(f"invalid addon id: {addon_id!r}")
    return clean


def _normalize_spec(raw: Any, *, source: str = "<addon>") -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise LumaError(f"invalid addon spec in {source}: expected a YAML mapping")
    api_version = str(raw.get("apiVersion") or "").strip()
    if api_version != ADDON_API_VERSION:
        raise LumaError(f"unsupported addon apiVersion in {source}: {api_version!r} (expected {ADDON_API_VERSION})")
    addon_id = validate_addon_id(str(raw.get("id") or ""))
    name = str(raw.get("name") or addon_id).strip()
    stack = str(raw.get("stack") or f"luma-{addon_id}").strip()
    auth = raw.get("auth") if isinstance(raw.get("auth"), dict) else {}
    ui = raw.get("ui") if isinstance(raw.get("ui"), dict) else {}
    presets = raw.get("presets") if isinstance(raw.get("presets"), list) else []
    schema = raw.get("configSchema") if isinstance(raw.get("configSchema"), list) else []
    auto_secrets = raw.get("autoSecrets") if isinstance(raw.get("autoSecrets"), list) else []
    raw_caps = raw.get("uiCapabilities") if isinstance(raw.get("uiCapabilities"), list) else []
    ui_capabilities = [str(c).strip() for c in raw_caps if str(c or "").strip()]
    raw_defaults = raw.get("defaultImages") if isinstance(raw.get("defaultImages"), dict) else {}
    default_images = {
        str(k).strip(): str(v).strip()
        for k, v in raw_defaults.items()
        if str(k or "").strip() and str(v or "").strip()
    }
    raw_req = raw.get("requirements") if isinstance(raw.get("requirements"), dict) else {}
    raw_by_preset = raw_req.get("byPreset") if isinstance(raw_req.get("byPreset"), dict) else {}
    by_preset: Dict[str, Dict[str, int]] = {}
    for pid, pval in raw_by_preset.items():
        if isinstance(pval, dict):
            entry: Dict[str, int] = {}
            for metric in ("minMemoryMb", "recommendedMemoryMb", "minCpu"):
                if pval.get(metric) not in (None, ""):
                    entry[metric] = int(pval[metric])
            if entry:
                by_preset[str(pid).strip()] = entry
    has_required_node = any(
        isinstance(f, dict) and str(f.get("key") or "") == "node" and bool(f.get("required"))
        for f in schema
    )
    requirements = {
        "minMemoryMb": int(raw_req.get("minMemoryMb") or 0),
        "recommendedMemoryMb": int(raw_req.get("recommendedMemoryMb") or 0),
        "minCpu": int(raw_req.get("minCpu") or 0),
        "stateful": bool(raw_req.get("stateful", False)),
        "pinPolicy": str(raw_req.get("pinPolicy") or ("required" if has_required_node else "optional")).strip(),
        "note": raw_req.get("note") if isinstance(raw_req.get("note"), dict) else {"en": "", "zh": ""},
        "byPreset": by_preset,
    }
    raw_bundle = raw.get("bundle") if isinstance(raw.get("bundle"), dict) else {}
    kind = str(raw.get("kind") or "compose").strip().lower()
    if kind not in VALID_ADDON_KINDS:
        raise LumaError(f"unsupported addon kind in {source}: {kind!r}")
    return {
        "apiVersion": ADDON_API_VERSION,
        "id": addon_id,
        "name": name,
        "kind": kind,
        "required": bool(raw.get("required", False)),
        "category": str(raw.get("category") or "utility").strip(),
        "stack": stack,
        "primaryService": str(raw.get("primaryService") or addon_id).strip(),
        "sourceDir": str(raw.get("sourceDir") or "").strip(),
        "defaultImage": str(raw.get("defaultImage") or "").strip(),
        "defaultImages": default_images,
        "bundle": {
            "defaultUrl": str(raw_bundle.get("defaultUrl") or "").strip(),
            "manifestUrl": str(raw_bundle.get("manifestUrl") or "").strip(),
        },
        "uiCapabilities": ui_capabilities,
        "requirements": requirements,
        "description": raw.get("description") if isinstance(raw.get("description"), dict) else {"en": "", "zh": ""},
        "auth": {
            "mode": str(auth.get("mode") or "none").strip(),
            "callbackPath": str(auth.get("callbackPath") or "/auth/callback").strip(),
            "consolePath": str(auth.get("consolePath") or "/").strip(),
            "cliAuth": str(auth.get("cliAuth") or "none").strip(),
            "controlUrlEnv": str(auth.get("controlUrlEnv") or "").strip(),
            "publicUrlEnv": str(auth.get("publicUrlEnv") or "").strip(),
            "needsControlDeployToken": bool(auth.get("needsControlDeployToken", False)),
        },
        "ui": {
            "icon": str(ui.get("icon") or addon_id).strip(),
            "openMode": str(ui.get("openMode") or "external").strip(),
            "dashboardPath": str(ui.get("dashboardPath") or "").strip(),
            "healthPath": str(ui.get("healthPath") or "/healthz").strip(),
        },
        "presets": [dict(p) for p in presets if isinstance(p, dict) and p.get("id")],
        "configSchema": [dict(f) for f in schema if isinstance(f, dict) and f.get("key")],
        "autoSecrets": [dict(s) for s in auto_secrets if isinstance(s, dict) and s.get("name")],
    }


def load_addon_spec(path: Path) -> Dict[str, Any]:
    target = Path(path).expanduser()
    if not target.is_file():
        raise LumaError(f"addon spec file not found: {target}")
    try:
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LumaError(f"failed to parse addon spec {target}: {exc}") from exc
    return _normalize_spec(loaded, source=str(target))


def load_addon_catalog() -> Dict[str, Dict[str, Any]]:
    directory = addons_assets_dir()
    found: Dict[str, Dict[str, Any]] = {}
    if directory.is_dir():
        for path in sorted(directory.glob("*.yaml")):
            spec = load_addon_spec(path)
            found[spec["id"]] = spec
    ordered: Dict[str, Dict[str, Any]] = {}
    for addon_id in OFFICIAL_ORDER:
        if addon_id in found:
            ordered[addon_id] = found.pop(addon_id)
    for addon_id in sorted(found):
        ordered[addon_id] = found[addon_id]
    return ordered


def get_addon_spec(addon_id: str) -> Dict[str, Any]:
    clean = validate_addon_id(addon_id)
    catalog = load_addon_catalog()
    if clean not in catalog:
        available = ", ".join(catalog.keys()) or "(none)"
        raise LumaError(f"unknown addon {clean!r}; available addons: {available}")
    return catalog[clean]


def default_preset_id(spec: Mapping[str, Any]) -> str:
    presets = spec.get("presets") or []
    if presets and isinstance(presets[0], Mapping):
        return str(presets[0].get("id") or "default")
    return "default"


def get_preset(spec: Mapping[str, Any], preset_id: str = "") -> Dict[str, Any]:
    presets = [p for p in (spec.get("presets") or []) if isinstance(p, Mapping)]
    if not presets:
        return {"id": preset_id or "default"}
    wanted = str(preset_id or "").strip() or str(presets[0].get("id") or "default")
    for preset in presets:
        if str(preset.get("id") or "") == wanted:
            return dict(preset)
    valid = ", ".join(str(p.get("id")) for p in presets)
    raise LumaError(f"unknown preset {wanted!r} for addon {spec.get('id')}; valid presets: {valid}")


def default_config_for_preset(spec: Mapping[str, Any], preset_id: str = "") -> Dict[str, Any]:
    preset = get_preset(spec, preset_id)
    defaults: Dict[str, Any] = {}
    for field in spec.get("configSchema") or []:
        if not isinstance(field, Mapping) or field.get("type") == "secret":
            continue
        key = str(field["key"])
        if "default" in field:
            defaults[key] = field["default"]
    if "defaultRegion" in preset and "region" in defaults:
        defaults["region"] = preset["defaultRegion"]
    if "defaultExposure" in preset and "exposure" in defaults:
        defaults["exposure"] = preset["defaultExposure"]
    if "defaultNode" in preset and "node" in defaults:
        defaults["node"] = preset["defaultNode"]
    return defaults


def effective_requirements(spec: Mapping[str, Any], preset_id: str = "") -> Dict[str, Any]:
    """Return effective resource requirements for ``spec`` and ``preset_id``."""
    raw = spec.get("requirements") if isinstance(spec.get("requirements"), Mapping) else {}
    out = {
        "minMemoryMb": int(raw.get("minMemoryMb") or 0),
        "recommendedMemoryMb": int(raw.get("recommendedMemoryMb") or 0),
        "minCpu": int(raw.get("minCpu") or 0),
        "stateful": bool(raw.get("stateful", False)),
        "pinPolicy": str(raw.get("pinPolicy") or "optional").strip(),
        "note": dict(raw.get("note") or {}) if isinstance(raw.get("note"), Mapping) else {"en": "", "zh": ""},
    }
    by_preset = raw.get("byPreset") if isinstance(raw.get("byPreset"), Mapping) else {}
    resolved = str(preset_id or "").strip() or default_preset_id(spec)
    override = by_preset.get(resolved) if isinstance(by_preset.get(resolved), Mapping) else None
    if override:
        for metric in ("minMemoryMb", "recommendedMemoryMb", "minCpu"):
            if override.get(metric) not in (None, ""):
                out[metric] = int(override[metric])
    return out


def derive_origin_suffixes(domain: str) -> str:
    """Derive sensible default allowed origin suffixes from an Eye domain."""
    clean = str(domain or "").strip().lower().rstrip(".")
    if not clean or not DOMAIN_RE.fullmatch(clean):
        return "localhost,127.0.0.1"
    parts = [part for part in clean.split(".") if part]
    if len(parts) >= 3:
        root = "." + ".".join(parts[-2:])
    elif len(parts) == 2:
        root = "." + clean
    else:
        return "localhost,127.0.0.1"
    return f"{root},localhost,127.0.0.1"


def validate_addon_config(
    spec: Mapping[str, Any],
    incoming_config: Mapping[str, Any],
    *,
    incoming_secrets: Optional[Mapping[str, Any]] = None,
    existing_config: Optional[Mapping[str, Any]] = None,
    preset_id: str = "",
    regions: Optional[set[str]] = None,
    nodes: Optional[set[str]] = None,
    require_complete: bool = False,
) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """Validate and normalize non-secret addon config and secret updates.

    Returns ``(resolved_preset, clean_config, secret_updates)`` where
    ``secret_updates`` maps uppercase environment variable names to string values
    (only non-empty updates; empty string signals leave-unchanged unless explicit).
    """
    resolved_preset = str(preset_id or "").strip() or default_preset_id(spec)
    preset = get_preset(spec, resolved_preset)
    base = default_config_for_preset(spec, resolved_preset)
    if existing_config:
        for k, v in existing_config.items():
            if v is not None:
                base[k] = v

    schema_by_key: Dict[str, Mapping[str, Any]] = {
        str(field["key"]): field
        for field in (spec.get("configSchema") or [])
        if isinstance(field, Mapping) and field.get("key")
    }
    clean_config: Dict[str, Any] = dict(base)
    secret_updates: Dict[str, str] = {}

    for key, raw_val in (incoming_config or {}).items():
        if key not in schema_by_key:
            raise LumaError(f"unknown config field {key!r} for addon {spec.get('id')}")
        field = schema_by_key[key]
        ftype = str(field.get("type") or "string")
        if ftype == "secret":
            env_name = str(field.get("env") or "").strip()
            if env_name and raw_val is not None and str(raw_val).strip():
                secret_updates[env_name] = str(raw_val).strip()
            continue
        clean_config[key] = _coerce_field_value(key, ftype, raw_val, field, regions=regions, nodes=nodes)

    for env_key, raw_val in (incoming_secrets or {}).items():
        clean_key = str(env_key or "").strip()
        if not ENV_NAME_RE.fullmatch(clean_key):
            raise LumaError(f"invalid secret name: {env_key!r}")
        if raw_val is not None:
            text = str(raw_val).strip()
            if text:
                secret_updates[clean_key] = text

    # Re-validate all fields and requiredness
    for key, field in schema_by_key.items():
        ftype = str(field.get("type") or "string")
        if ftype == "secret":
            continue
        val = clean_config.get(key)
        if val not in (None, ""):
            clean_config[key] = _coerce_field_value(key, ftype, val, field, regions=regions, nodes=nodes)
        elif field.get("required") and (require_complete or key in (incoming_config or {})):
            if str(spec.get("id")) == "eye" and key == "node":
                raise LumaError("addon 'eye' requires a pinned 'node' because ClickHouse and Eye use local stateful volumes")
            raise LumaError(f"addon {spec.get('id')} requires config field {key!r}")

    # Preset-specific ergonomic checks
    if str(spec.get("id")) == "eye":
        if require_complete and not str(clean_config.get("node") or "").strip():
            raise LumaError("addon 'eye' requires a pinned 'node' because ClickHouse and Eye use local stateful volumes")
    if preset.get("defaultNode") and not clean_config.get("node") and str(spec.get("id")) == "observe":
        clean_config["node"] = str(preset["defaultNode"])

    return resolved_preset, clean_config, secret_updates


def _coerce_field_value(
    key: str,
    ftype: str,
    raw_val: Any,
    field: Mapping[str, Any],
    *,
    regions: Optional[set[str]] = None,
    nodes: Optional[set[str]] = None,
) -> Any:
    if ftype == "integer":
        try:
            num = int(str(raw_val).strip())
        except (ValueError, TypeError) as exc:
            raise LumaError(f"config field {key!r} must be an integer") from exc
        if "min" in field and num < int(field["min"]):
            raise LumaError(f"config field {key!r} must be >= {field['min']}")
        if "max" in field and num > int(field["max"]):
            raise LumaError(f"config field {key!r} must be <= {field['max']}")
        return num
    if ftype == "boolean":
        if isinstance(raw_val, bool):
            return raw_val
        low = str(raw_val).strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
        raise LumaError(f"config field {key!r} must be a boolean")
    text = str(raw_val if raw_val is not None else "").strip()
    if not text:
        return ""
    if ftype == "domain":
        if text.startswith(("http://", "https://")):
            text = re.sub(r"^https?://", "", text).rstrip("/")
        if "/" in text or ":" in text or not DOMAIN_RE.fullmatch(text):
            raise LumaError(f"config field {key!r} must be a bare domain hostname (e.g. eye.example.com)")
        return text.lower()
    if ftype == "select":
        options = [str(opt) for opt in (field.get("options") or [])]
        if options and text not in options:
            raise LumaError(f"config field {key!r} must be one of: {', '.join(options)}")
        return text
    if ftype == "region":
        if regions is not None and text not in regions:
            raise LumaError(f"unknown region {text!r}; available regions: {', '.join(sorted(regions))}")
        return text
    if ftype == "node":
        if nodes is not None and text not in nodes:
            raise LumaError(f"unknown node {text!r}; available nodes: {', '.join(sorted(nodes))}")
        return text
    return text


def ensure_auto_secrets(
    spec: Mapping[str, Any],
    existing_scoped: Mapping[str, Any],
    *,
    rotate: bool = False,
) -> Dict[str, str]:
    """Generate any missing internal secrets declared in ``autoSecrets``.

    Returns only the newly generated key/value pairs so existing secrets remain
    untouched unless ``rotate=True``.
    """
    generated: Dict[str, str] = {}
    for entry in spec.get("autoSecrets") or []:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").strip()
        if not name or not ENV_NAME_RE.fullmatch(name):
            continue
        if not rotate and str(existing_scoped.get(name) or "").strip():
            continue
        nbytes = max(16, int(entry.get("bytes") or 24))
        fmt = str(entry.get("format") or "hex").strip().lower()
        prefix = str(entry.get("prefix") or "")
        raw = secrets.token_urlsafe(nbytes) if fmt == "urlsafe" else secrets.token_hex(nbytes)
        generated[name] = f"{prefix}{raw}"
    return generated


def extract_compose_images(compose_yaml: str) -> Dict[str, str]:
    """Extract pinned service images from a previously deployed compose YAML."""
    if not compose_yaml or not compose_yaml.strip():
        return {}
    try:
        parsed = yaml.safe_load(compose_yaml)
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    services = parsed.get("services")
    if not isinstance(services, dict):
        return {}
    images: Dict[str, str] = {}
    for svc_name, svc in services.items():
        if isinstance(svc, dict):
            image = str(svc.get("image") or "").strip()
            if image:
                images[str(svc_name)] = image
    return images


def infer_config_from_deployment(
    spec: Mapping[str, Any],
    sidecar_yaml: str,
    compose_yaml: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """Infer preset and non-secret addon config from an existing deployed manifest."""
    addon_id = str(spec.get("id") or "")
    preset_id = default_preset_id(spec)
    inferred: Dict[str, Any] = {}

    sidecar: Dict[str, Any] = {}
    if sidecar_yaml and sidecar_yaml.strip():
        try:
            loaded = yaml.safe_load(sidecar_yaml)
            if isinstance(loaded, dict):
                sidecar = loaded
        except yaml.YAMLError:
            sidecar = {}

    compose: Dict[str, Any] = {}
    if compose_yaml and compose_yaml.strip():
        try:
            loaded = yaml.safe_load(compose_yaml)
            if isinstance(loaded, dict):
                compose = loaded
        except yaml.YAMLError:
            compose = {}

    if sidecar:
        if sidecar.get("region"):
            inferred["region"] = str(sidecar["region"]).strip()
        services = sidecar.get("services") if isinstance(sidecar.get("services"), dict) else {}
        primary_name = str(spec.get("primaryService") or addon_id)
        primary = services.get(primary_name) if isinstance(services.get(primary_name), dict) else sidecar
        if isinstance(primary, dict):
            if primary.get("domain"):
                inferred["domain"] = str(primary["domain"]).strip().lower()
            if primary.get("exposure"):
                inferred["exposure"] = str(primary["exposure"]).strip()
            if primary.get("node"):
                inferred["node"] = str(primary["node"]).strip()

    if addon_id == "eye" and inferred.get("exposure") == "node-edge":
        preset_id = "node-edge"

    # Extract plain (non-${VAR}) environment values from primary compose service
    compose_services = compose.get("services") if isinstance(compose.get("services"), dict) else {}
    primary_compose = compose_services.get(str(spec.get("primaryService") or addon_id))
    env_map: Dict[str, str] = {}
    if isinstance(primary_compose, dict):
        raw_env = primary_compose.get("environment")
        if isinstance(raw_env, dict):
            for k, v in raw_env.items():
                if v is not None:
                    env_map[str(k)] = str(v)
        elif isinstance(raw_env, list):
            for entry in raw_env:
                if isinstance(entry, str) and "=" in entry:
                    k, v = entry.split("=", 1)
                    env_map[k.strip()] = v.strip()

    for field in spec.get("configSchema") or []:
        if not isinstance(field, Mapping) or field.get("type") == "secret":
            continue
        env_name = str(field.get("env") or "").strip()
        if not env_name or env_name not in env_map:
            continue
        val = env_map[env_name].strip()
        if not val or (val.startswith("${") and val.endswith("}")):
            continue
        key = str(field["key"])
        ftype = str(field.get("type") or "string")
        try:
            inferred[key] = _coerce_field_value(key, ftype, val, field)
        except LumaError:
            pass

    defaults = default_config_for_preset(spec, preset_id)
    defaults.update(inferred)
    return preset_id, defaults


def render_addon_manifests(
    spec: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    preset_id: str = "",
    control_url: str = "",
    scoped_secrets: Optional[Mapping[str, Any]] = None,
    existing_compose_yaml: str = "",
    existing_sidecar_yaml: str = "",
    image_overrides: Optional[Mapping[str, str]] = None,
    for_source_build: bool = False,
    source_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Render Luma Compose sidecar + Docker Compose YAML for an official addon.

    Preserves exact volume names so migrating or reconfiguring live stacks keeps
    all persistent data intact. When ``for_source_build=False``, preserves pinned
    immutable images from ``existing_compose_yaml`` or ``image_overrides``.
    """
    addon_id = str(spec["id"])
    stack = str(spec.get("stack") or f"luma-{addon_id}")
    resolved_preset = str(preset_id or "").strip() or default_preset_id(spec)
    secrets_present = {str(k) for k, v in (scoped_secrets or {}).items() if str(v or "").strip()}
    pinned_images: Dict[str, str] = {}
    if not for_source_build:
        for k, v in (spec.get("defaultImages") or {}).items():
            if str(v or "").strip():
                pinned_images[str(k)] = str(v).strip()
    pinned_images.update(extract_compose_images(existing_compose_yaml))
    if image_overrides:
        for k, v in image_overrides.items():
            if str(v or "").strip():
                pinned_images[str(k)] = str(v).strip()

    if addon_id == "eye":
        sidecar_doc, compose_doc = _render_eye(
            stack=stack,
            preset_id=resolved_preset,
            config=config,
            control_url=control_url,
            secrets_present=secrets_present,
            pinned_images=pinned_images,
            for_source_build=for_source_build,
        )
    elif addon_id == "builder":
        sidecar_doc, compose_doc = _render_builder(
            stack=stack,
            config=config,
            control_url=control_url,
            secrets_present=secrets_present,
            pinned_images=pinned_images,
            for_source_build=for_source_build,
        )
    elif addon_id == "observe":
        sidecar_yaml, compose_yaml = _render_observe(
            stack=stack,
            config=config,
            secrets_present=secrets_present,
            pinned_images=pinned_images,
            existing_sidecar_yaml=existing_sidecar_yaml,
            existing_compose_yaml=existing_compose_yaml,
            for_source_build=for_source_build,
            source_dir=source_dir,
        )
        return {
            "stack": stack,
            "preset": resolved_preset,
            "sidecarYaml": sidecar_yaml,
            "composeYaml": compose_yaml,
            "composeFilename": "docker-compose.yml",
        }
    else:
        raise LumaError(f"addon {addon_id!r} does not have a built-in manifest renderer")

    sidecar_yaml = yaml.safe_dump(sidecar_doc, sort_keys=False, allow_unicode=True)
    compose_yaml = yaml.safe_dump(compose_doc, sort_keys=False, allow_unicode=True)
    return {
        "stack": stack,
        "preset": resolved_preset,
        "sidecarYaml": sidecar_yaml,
        "composeYaml": compose_yaml,
        "composeFilename": str(sidecar_doc.get("compose") or "docker-compose.yml"),
    }


def _secret_or_empty(env_name: str, secrets_present: set[str]) -> str:
    return f"${{{env_name}}}" if env_name in secrets_present else ""


def _render_eye(
    *,
    stack: str,
    preset_id: str,
    config: Mapping[str, Any],
    control_url: str,
    secrets_present: set[str],
    pinned_images: Mapping[str, str],
    for_source_build: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    domain = str(config.get("domain") or "").strip().lower()
    if not domain:
        raise LumaError("addon 'eye' requires 'domain' before rendering manifests")
    region = str(config.get("region") or ("global" if preset_id == "node-edge" else "cn")).strip()
    exposure = str(config.get("exposure") or ("node-edge" if preset_id == "node-edge" else "cn-edge")).strip()
    node = str(config.get("node") or "").strip()
    if not node:
        raise LumaError("addon 'eye' requires a pinned 'node' before rendering manifests")
    retention_days = int(config.get("retentionDays") or 7)
    max_storage_gb = int(config.get("maxStorageGb") or 15)
    suffixes = str(config.get("autoOriginSuffixes") or "").strip() or derive_origin_suffixes(domain)
    public_url = f"https://{domain}"
    node_edge = preset_id == "node-edge" or exposure == "node-edge"

    cdn = str(config.get("cdn") or "").strip().lower()
    eye_svc_sidecar: Dict[str, Any] = {
        "node": node,
        "exposure": exposure,
        "domain": domain,
        "port": 8090,
        "replicas": 1,
    }
    if cdn and cdn != "none":
        eye_svc_sidecar["cdn"] = cdn
    ch_svc_sidecar: Dict[str, Any] = {
        "node": node,
        "exposure": "none",
        "replicas": 1,
    }

    sidecar_doc: Dict[str, Any] = {
        "name": stack,
        "compose": "docker-compose.yml",
        "region": region,
        "volumes": {
            "eye-data": {"initialize": "empty"},
            "clickhouse-data": {"initialize": "empty"},
        },
        "services": {
            "eye": eye_svc_sidecar,
            "clickhouse": ch_svc_sidecar,
        },
    }

    eye_env: Dict[str, str] = {
        "LUMA_EYE_MODE": "all",
        "LUMA_EYE_LISTEN": ":8090",
        "LUMA_EYE_DATA_DIR": "/var/lib/luma-eye",
        "LUMA_EYE_PUBLIC_URL": public_url,
        "LUMA_CONTROL_URL": control_url.rstrip("/"),
        "LUMA_ADDON_ID": "eye",
        "LUMA_EYE_ADMIN_TOKEN": _secret_or_empty("LUMA_EYE_ADMIN_TOKEN", secrets_present),
        "LUMA_EYE_RELAY_SECRET": _secret_or_empty("LUMA_EYE_RELAY_SECRET", secrets_present),
        "LUMA_EYE_AUTO_KEY": _secret_or_empty("LUMA_EYE_AUTO_KEY", secrets_present),
        "LUMA_EYE_AUTO_ORIGIN_SUFFIXES": suffixes,
        "LUMA_EYE_FEISHU_WEBHOOK": _secret_or_empty("LUMA_EYE_FEISHU_WEBHOOK", secrets_present),
        "LUMA_EYE_RETENTION_DAYS": str(retention_days),
        "LUMA_EYE_MAX_STORAGE_GB": str(max_storage_gb),
        "LUMA_EYE_QUEUE_CAP": "8192" if node_edge else "32768",
        "LUMA_EYE_CLICKHOUSE_URL": "http://clickhouse:8123",
        "LUMA_EYE_CLICKHOUSE_DB": "luma_eye",
        "LUMA_EYE_CLICKHOUSE_USER": "default",
        "LUMA_EYE_CLICKHOUSE_PASSWORD": "",
        "GOMEMLIMIT": "280MiB" if node_edge else "512MiB",
    }

    eye_service: Dict[str, Any] = {
        "image": pinned_images.get("eye", "luma-eye:local"),
        "restart": "unless-stopped",
        "depends_on": {"clickhouse": {"condition": "service_healthy"}},
        "environment": eye_env,
        "volumes": ["eye-data:/var/lib/luma-eye"],
        "healthcheck": {
            "test": ["CMD", "luma-eye", "healthcheck", "--url", "http://127.0.0.1:8090/healthz"],
            "interval": "15s",
            "timeout": "5s",
            "start_period": "20s",
            "retries": 5,
        },
        "deploy": {
            "resources": {
                "reservations": {"cpus": "0.15" if node_edge else "0.20", "memory": "128M"},
                "limits": {"memory": "384M" if node_edge else "768M"},
            }
        },
    }
    if for_source_build:
        eye_service["build"] = {"context": ".", "dockerfile": "Dockerfile"}
    elif "eye" not in pinned_images:
        raise LumaError("addon 'eye' has no deployed image yet; install from source with `luma addon install eye --source ./eye` or pass `--image`")

    ch_service: Dict[str, Any] = {
        "image": pinned_images.get("clickhouse", "luma-eye-clickhouse:local" if node_edge else "clickhouse/clickhouse-server:24.8-alpine"),
        "restart": "unless-stopped",
        "environment": {
            "CLICKHOUSE_DB": "luma_eye",
            "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT": "1",
        },
        "volumes": ["clickhouse-data:/var/lib/clickhouse"],
        "healthcheck": {
            "test": ["CMD", "wget", "-qO-", "http://127.0.0.1:8123/ping"],
            "interval": "10s",
            "timeout": "5s",
            "start_period": "20s",
            "retries": 10,
        },
        "deploy": {
            "resources": {
                "reservations": {"cpus": "0.25", "memory": "384M" if node_edge else "512M"},
                "limits": {"memory": "1024M" if node_edge else "2560M"},
            }
        },
    }
    if for_source_build:
        ch_service["build"] = {
            "context": "deploy/node-edge",
            "dockerfile": "Dockerfile.clickhouse",
        }
    elif "clickhouse" not in pinned_images and not node_edge:
        ch_service["image"] = "clickhouse/clickhouse-server:24.8-alpine"

    compose_doc: Dict[str, Any] = {
        "services": {
            "eye": eye_service,
            "clickhouse": ch_service,
        },
        "volumes": {
            "eye-data": None,
            "clickhouse-data": None,
        },
    }
    return sidecar_doc, compose_doc


def _render_builder(
    *,
    stack: str,
    config: Mapping[str, Any],
    control_url: str,
    secrets_present: set[str],
    pinned_images: Mapping[str, str],
    for_source_build: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    domain = str(config.get("domain") or "").strip().lower()
    if not domain:
        raise LumaError("addon 'builder' requires 'domain' before rendering manifests")
    region = str(config.get("region") or "cn").strip()
    exposure = str(config.get("exposure") or "cn-edge").strip()
    node = str(config.get("node") or "").strip()

    server_sidecar: Dict[str, Any] = {}
    if node:
        server_sidecar["node"] = node
    server_sidecar.update({
        "exposure": exposure,
        "domain": domain,
        "port": 8090,
        "replicas": 1,
    })

    sidecar_doc: Dict[str, Any] = {
        "name": stack,
        "compose": "docker-compose.yml",
        "region": region,
        "volumes": {
            "luma-build-data": {"initialize": "empty"},
        },
        "services": {
            "server": server_sidecar,
        },
    }

    reg_host = str(config.get("registryHost") or "").strip()
    push_host = str(config.get("pushHost") or "").strip() or reg_host
    reg_ns = str(config.get("registryNamespace") or "").strip()
    reg_user = str(config.get("registryUsername") or "").strip()
    gitea_host = str(config.get("giteaHost") or "").strip()
    gitea_user = str(config.get("giteaUsername") or "").strip()
    affinity = str(int(config.get("affinitySeconds") if config.get("affinitySeconds") not in (None, "") else 3))

    server_env: Dict[str, str] = {
        "LUMA_CONTROL_URL": control_url.rstrip("/") or _secret_or_empty("LUMA_CONTROL_URL", secrets_present),
        "LUMA_ADDON_ID": "builder",
        "LUMA_DEPLOY_TOKEN": _secret_or_empty("LUMA_DEPLOY_TOKEN", secrets_present),
        "LUMA_BUILD_REGISTRY_HOST": reg_host or _secret_or_empty("LUMA_BUILD_REGISTRY_HOST", secrets_present),
        "LUMA_BUILD_PUSH_HOST": push_host or _secret_or_empty("LUMA_BUILD_PUSH_HOST", secrets_present),
        "LUMA_BUILD_REGISTRY_NAMESPACE": reg_ns or _secret_or_empty("LUMA_BUILD_REGISTRY_NAMESPACE", secrets_present),
        "LUMA_BUILD_REGISTRY_USERNAME": reg_user or _secret_or_empty("LUMA_BUILD_REGISTRY_USERNAME", secrets_present),
        "LUMA_BUILD_REGISTRY_PASSWORD": _secret_or_empty("LUMA_BUILD_REGISTRY_PASSWORD", secrets_present),
        "GITHUB_TOKEN": _secret_or_empty("GITHUB_TOKEN", secrets_present),
        "GITEA_HOST": gitea_host or _secret_or_empty("GITEA_HOST", secrets_present),
        "GITEA_USERNAME": gitea_user or _secret_or_empty("GITEA_USERNAME", secrets_present),
        "GITEA_TOKEN": _secret_or_empty("GITEA_TOKEN", secrets_present),
        "LUMA_BUILD_AFFINITY_SECONDS": affinity,
    }

    server_service: Dict[str, Any] = {
        "image": pinned_images.get("server", "luma-build-server:local"),
        "restart": "unless-stopped",
        "ports": ["8090:8090"],
        "environment": server_env,
        "volumes": ["luma-build-data:/var/lib/luma-build"],
        "healthcheck": {
            "test": ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3)"],
            "interval": "15s",
            "timeout": "5s",
            "retries": 3,
        },
        "deploy": {
            "resources": {
                "reservations": {"cpus": "0.10", "memory": "128M"},
                "limits": {"memory": "512M"},
            }
        },
    }
    if for_source_build:
        server_service["build"] = {"context": ".", "dockerfile": "Dockerfile.server"}
    elif "server" not in pinned_images:
        raise LumaError("addon 'builder' has no deployed image yet; install from source with `luma addon install builder --source ./builder` or pass `--image`")

    compose_doc: Dict[str, Any] = {
        "services": {"server": server_service},
        "volumes": {"luma-build-data": None},
    }
    return sidecar_doc, compose_doc


def _render_observe(
    *,
    stack: str,
    config: Mapping[str, Any],
    secrets_present: set[str],
    pinned_images: Mapping[str, str],
    existing_sidecar_yaml: str,
    existing_compose_yaml: str,
    for_source_build: bool,
    source_dir: Optional[Path],
) -> Tuple[str, str]:
    region = str(config.get("region") or "cn").strip()
    if for_source_build and source_dir is not None:
        sidecar_path = source_dir / "luma.compose.yml"
        compose_path = source_dir / "docker-compose.yml"
        if not sidecar_path.is_file() or not compose_path.is_file():
            raise LumaError(f"observe source directory missing luma.compose.yml or docker-compose.yml: {source_dir}")
        sidecar_doc = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
        if isinstance(sidecar_doc, dict):
            sidecar_doc["name"] = stack
            sidecar_doc["region"] = region
        return yaml.safe_dump(sidecar_doc, sort_keys=False, allow_unicode=True), compose_path.read_text(encoding="utf-8")

    raw_sidecar = existing_sidecar_yaml if existing_sidecar_yaml.strip() else ""
    raw_compose = existing_compose_yaml if existing_compose_yaml.strip() else ""
    if not raw_sidecar or not raw_compose:
        bundled = addons_assets_dir() / "observe"
        bundled_sidecar = bundled / "luma.compose.yml"
        bundled_compose = bundled / "docker-compose.yml"
        if bundled_sidecar.is_file() and bundled_compose.is_file():
            raw_sidecar = bundled_sidecar.read_text(encoding="utf-8")
            raw_compose = bundled_compose.read_text(encoding="utf-8")

    if raw_sidecar.strip() and raw_compose.strip():
        sidecar_doc = yaml.safe_load(raw_sidecar)
        if isinstance(sidecar_doc, dict):
            sidecar_doc["name"] = stack
            sidecar_doc["region"] = region
        compose_doc = yaml.safe_load(raw_compose)
        if isinstance(compose_doc, dict) and isinstance(compose_doc.get("services"), dict):
            for svc_name, svc_cfg in compose_doc["services"].items():
                if isinstance(svc_cfg, dict):
                    svc_cfg.pop("build", None)
                    if svc_name in pinned_images:
                        svc_cfg["image"] = pinned_images[svc_name]
            feishu = compose_doc["services"].get("feishu-webhook")
            if isinstance(feishu, dict) and "FEISHU_WEBHOOK_URL" in secrets_present:
                env = feishu.setdefault("environment", {})
                if isinstance(env, dict):
                    env["FEISHU_WEBHOOK_URL"] = "${FEISHU_WEBHOOK_URL}"
            access = compose_doc["services"].get("observe-access")
            if isinstance(access, dict):
                env = access.setdefault("environment", {})
                if isinstance(env, dict):
                    if "LUMA_OBSERVE_READ_TOKEN" in secrets_present:
                        env["LUMA_OBSERVE_READ_TOKEN"] = "${LUMA_OBSERVE_READ_TOKEN}"
                    if "LUMA_OBSERVE_INGEST_TOKEN" in secrets_present:
                        env["LUMA_OBSERVE_INGEST_TOKEN"] = "${LUMA_OBSERVE_INGEST_TOKEN}"
        return (
            yaml.safe_dump(sidecar_doc, sort_keys=False, allow_unicode=True),
            yaml.safe_dump(compose_doc, sort_keys=False, allow_unicode=True),
        )

    raise LumaError("addon 'observe' requires deploying from source (`luma addon install observe --source ./observe`) on first install")
