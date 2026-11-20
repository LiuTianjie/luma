"""Unified CLI lifecycle for pluggable Luma addons (``luma addon``)."""
from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

from ..addons import default_preset_id, get_preset
from ..errors import LumaError
from . import common


def add_parser(
    sub: argparse._SubParsersAction,
    _command,
    _commands,
    _add_control_arguments,
    _add_output_arguments,
) -> None:
    addon = _command(
        sub,
        "addon",
        "Install, configure and remove Luma addons (dashboard, builder, eye, observe)",
        description=(
            "Manage official and custom Luma addons with unified Luma Control SSO, "
            "automatic internal secret generation, hot-swappable Dashboard bundles, "
            "and schema-driven configuration."
        ),
        epilog=(
            "Examples:\n"
            "  luma addon list\n"
            "  luma addon install dashboard\n"
            "  luma addon install dashboard --source ./dashboard-src\n"
            "  luma addon rollback dashboard\n"
            "  luma addon install eye --preset node-edge --domain eye.example.com --region global --node edge-1\n"
            "  luma addon configure eye --set retentionDays=14 --apply\n"
            "  luma addon uninstall eye"
        ),
    )
    addon_sub = _commands(addon, "addon_command")

    p_list = _command(addon_sub, "list", "List available addons, install status and SSO endpoints")
    _add_control_arguments(p_list)
    _add_output_arguments(p_list)

    p_show = _command(addon_sub, "show", "Show detailed configuration, secrets status and schema for an addon")
    p_show.add_argument("addon_id", metavar="addon", help="Addon identifier (for example dashboard, builder, eye, observe)")
    _add_control_arguments(p_show)
    _add_output_arguments(p_show)

    p_cfg = _command(addon_sub, "configure", "Configure addon settings and auto-generate missing internal secrets")
    p_cfg.add_argument("addon_id", metavar="addon", help="Addon identifier")
    _add_config_flags(p_cfg)
    p_cfg.add_argument("--interactive", action="store_true", help="Prompt interactively for configuration values")
    p_cfg.add_argument("--rotate-secrets", action="store_true", help="Regenerate auto-managed internal secrets")
    p_cfg.add_argument("--apply", action="store_true", help="Redeploy the addon immediately using its pinned image or bundle")
    p_cfg.add_argument("--timeout", type=int, default=600, help="Deployment timeout in seconds when --apply is set (default: 600)")
    _add_control_arguments(p_cfg)
    _add_output_arguments(p_cfg)

    p_inst = _command(addon_sub, "install", "Configure and deploy an addon (or hot-swap a static-bundle addon)")
    p_inst.add_argument("addon_id", metavar="addon", help="Addon identifier (dashboard, builder, eye, observe)")
    _add_config_flags(p_inst)
    p_inst.add_argument("--source", type=Path, default=None, help="Local source checkout to build and deploy (static-bundle addons build locally and upload directly without Docker)")
    p_inst.add_argument("--bundle", type=Path, default=None, help="Prebuilt .tar.gz static bundle to upload and hot-swap (for static-bundle addons like dashboard)")
    p_inst.add_argument("--version", default="", help="Optional release version label when uploading a static bundle")
    p_inst.add_argument("--image", action="append", default=[], metavar="SERVICE=IMAGE", help="Pin a prebuilt image instead of building locally (repeatable)")
    p_inst.add_argument("--platform", default="linux/amd64", help="Target container platform when building from source (default: linux/amd64)")
    p_inst.add_argument("--rotate-secrets", action="store_true", help="Regenerate auto-managed internal secrets")
    p_inst.add_argument("--timeout", type=int, default=7200, help="Build/deploy timeout in seconds (default: 7200)")
    p_inst.add_argument("--yes", "-y", action="store_true", help="Skip interactive prompts and use flags/defaults")
    _add_control_arguments(p_inst)
    _add_output_arguments(p_inst)

    p_rb = _command(addon_sub, "rollback", "Roll back a static-bundle addon (such as dashboard) to a previous release")
    p_rb.add_argument("addon_id", metavar="addon", help="Addon identifier (for example dashboard)")
    p_rb.add_argument("--release", default="", help="Specific release ID to activate (defaults to previous release)")
    _add_control_arguments(p_rb)
    _add_output_arguments(p_rb)

    p_uninst = _command(addon_sub, "uninstall", "Remove an optional addon deployment and revoke its Control SSO sessions")
    p_uninst.add_argument("addon_id", metavar="addon", help="Addon identifier")
    p_uninst.add_argument("--purge-secrets", action="store_true", help="Also delete scoped secrets stored for this addon")
    p_uninst.add_argument("--delete-storage", action="store_true", help="Also delete removable storage volumes (data loss)")
    p_uninst.add_argument("--yes", "-y", action="store_true", help="Confirm without prompting")
    _add_control_arguments(p_uninst)
    _add_output_arguments(p_uninst)


def _add_config_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preset", default="", help="Deployment topology preset (see 'luma addon show <addon>')")
    parser.add_argument("--domain", default="", help="Public HTTPS hostname for the addon")
    parser.add_argument("--region", default="", help="Target scheduling region")
    parser.add_argument("--node", default="", help="Optional pinned node name")
    parser.add_argument("--exposure", default="", help="Traffic exposure mode (cn-edge, node-edge, external-edge, tailscale-relay, cloudflare-tunnel, none)")
    parser.add_argument("--set", dest="set_values", action="append", default=[], metavar="KEY=VALUE", help="Set a schema config field (repeatable)")
    parser.add_argument("--secret", dest="secret_values", action="append", default=[], metavar="NAME=VALUE", help="Set an addon secret value (repeatable)")
    parser.add_argument("--use-registry", dest="use_registry", default="", metavar="HOST", help="Reuse a saved Control registry credential (host) instead of retyping credentials")
    parser.add_argument("--use-git-provider", dest="use_git_providers", action="append", default=[], metavar="ID", help="Reuse a saved Control Git provider credential (repeatable)")


def _parse_kv_list(items: List[str], *, flag: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            raise LumaError(f"{flag} expects KEY=VALUE, got {raw!r}")
        key, value = raw.split("=", 1)
        clean_key = key.strip()
        if not clean_key:
            raise LumaError(f"{flag} key cannot be empty: {raw!r}")
        parsed[clean_key] = value.strip()
    return parsed


def _collect_flag_overrides(args: argparse.Namespace) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    preset = str(getattr(args, "preset", "") or "").strip()
    config: Dict[str, Any] = _parse_kv_list(list(getattr(args, "set_values", []) or []), flag="--set")
    secrets_map: Dict[str, str] = _parse_kv_list(list(getattr(args, "secret_values", []) or []), flag="--secret")
    for key in ("domain", "region", "node", "exposure"):
        val = str(getattr(args, key, "") or "").strip()
        if val:
            config[key] = val
    return preset, config, secrets_map


def _collect_inherit_payload(args: argparse.Namespace) -> Dict[str, Any]:
    inherit: Dict[str, Any] = {}
    reg = str(getattr(args, "use_registry", "") or "").strip()
    if reg:
        inherit["registryHost"] = reg
    git_ids = [str(item).strip() for item in (getattr(args, "use_git_providers", []) or []) if str(item or "").strip()]
    if git_ids:
        inherit["gitProviderIds"] = git_ids
    return inherit


def _prompt_line(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else (" [configured, Enter to keep]" if default and secret else "")
    prompt = f"{label}{suffix}: "
    raw = getpass.getpass(prompt) if secret else input(prompt)
    val = raw.strip()
    return val if val else ("" if secret else default)


def _fmt_gb(mb: int) -> str:
    if mb <= 0:
        return "0 GiB"
    gb = mb / 1024.0
    return f"{gb:.1f} GiB" if gb >= 1.0 else f"{mb} MiB"


def _pick_cli_default_node(
    node_details: List[Mapping[str, Any]],
    *,
    region: str,
    addon_id: str,
    preset_id: str,
    exposure: str,
    manager_node: str,
) -> str:
    in_region = [n for n in node_details if str(n.get("region") or "") == region]
    if not in_region:
        return ""
    if len(in_region) == 1:
        return str(in_region[0].get("name") or "")
    ready = [n for n in in_region if n.get("ready")]
    pool = ready if ready else in_region
    if addon_id == "eye" and (preset_id == "node-edge" or exposure == "node-edge"):
        best = sorted(
            pool,
            key=lambda n: (1 if n.get("publicIngressReady") else 0, int(n.get("memoryAvailableMb") or 0), int(n.get("memoryTotalMb") or 0)),
            reverse=True,
        )[0]
        return str(best.get("name") or "")
    if addon_id == "builder" and manager_node:
        for n in pool:
            if str(n.get("name") or "") == manager_node:
                return manager_node
    best = sorted(pool, key=lambda n: (int(n.get("memoryAvailableMb") or 0), int(n.get("memoryTotalMb") or 0)), reverse=True)[0]
    return str(best.get("name") or "")


def _resource_warning_text(
    addon: Mapping[str, Any],
    node_details: List[Mapping[str, Any]],
    *,
    manager_node: str,
    target_node_name: str,
    preset_id: str,
    same_node_redeploy: bool = False,
) -> str:
    reqs = dict(addon.get("requirements") or {})
    by_preset = reqs.get("byPreset") if isinstance(reqs.get("byPreset"), Mapping) else {}
    override = by_preset.get(preset_id) if isinstance(by_preset.get(preset_id), Mapping) else {}
    min_mb = int(override.get("minMemoryMb") or reqs.get("minMemoryMb") or 0)
    rec_mb = int(override.get("recommendedMemoryMb") or reqs.get("recommendedMemoryMb") or 0)
    if not min_mb and not rec_mb:
        return ""
    resolved_node = manager_node if reqs.get("pinPolicy") == "manager" else target_node_name
    node = next((n for n in node_details if str(n.get("name") or "") == resolved_node), None)
    if not node:
        return ""
    total_mb = int(node.get("memoryTotalMb") or 0)
    avail_mb = int(node.get("memoryAvailableMb") or 0)
    if total_mb <= 0:
        return ""
    if same_node_redeploy:
        if min_mb and total_mb < min_mb:
            return (
                f"[warn] Target node {resolved_node} has {_fmt_gb(total_mb)} total RAM, "
                f"below {addon.get('name')} minimum ({_fmt_gb(min_mb)})."
            )
        return ""
    if min_mb and (avail_mb < min_mb or total_mb < min_mb):
        return (
            f"[warn] Target node {resolved_node} has {_fmt_gb(avail_mb)} free / {_fmt_gb(total_mb)} total RAM; "
            f"{addon.get('name')} requires at least {_fmt_gb(min_mb)} (recommended >= {_fmt_gb(rec_mb or min_mb)})."
        )
    if rec_mb and (avail_mb < rec_mb or total_mb < rec_mb):
        return (
            f"[note] Target node {resolved_node} has {_fmt_gb(avail_mb)} free / {_fmt_gb(total_mb)} total RAM; "
            f"{addon.get('name')} recommends >= {_fmt_gb(rec_mb)}."
        )
    return ""


def _interactive_wizard(
    addon: Mapping[str, Any],
    *,
    available_regions: List[str],
    available_nodes: List[str],
    node_details: Optional[List[Mapping[str, Any]]] = None,
    manager_node: str = "",
    preset: str,
    config: Dict[str, Any],
    secrets_map: Dict[str, str],
) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    details = list(node_details or [])
    presets = [p for p in (addon.get("presets") or []) if isinstance(p, Mapping)]
    current_preset = preset or str(addon.get("preset") or default_preset_id(addon))
    if len(presets) > 1 and not preset:
        print(f"\nPresets for {addon.get('name')} ({addon.get('id')}):")
        for idx, item in enumerate(presets, start=1):
            desc = (item.get("description") or {}).get("en") or ""
            mark = " (current)" if item.get("id") == current_preset else ""
            print(f"  {idx}. {item.get('id')}{mark} — {desc}")
        chosen = _prompt_line("Preset", current_preset)
        if chosen.isdigit() and 1 <= int(chosen) <= len(presets):
            current_preset = str(presets[int(chosen) - 1]["id"])
        else:
            current_preset = chosen
    preset_obj = get_preset(addon, current_preset)

    merged_cfg = dict(addon.get("config") or {})
    if preset and preset != addon.get("preset"):
        if preset_obj.get("defaultRegion") and "region" not in config:
            merged_cfg["region"] = preset_obj["defaultRegion"]
        if preset_obj.get("defaultExposure") and "exposure" not in config:
            merged_cfg["exposure"] = preset_obj["defaultExposure"]
    merged_cfg.update(config)

    secrets_status = addon.get("secretsStatus") if isinstance(addon.get("secretsStatus"), Mapping) else {}
    print(f"\nConfiguring {addon.get('name')} (preset: {current_preset}):")
    if available_regions:
        print(f"  Regions: {', '.join(available_regions)}")
    if details:
        formatted_nodes = [
            f"{n.get('name')} ({n.get('region') or '?'}"
            + (f", {_fmt_gb(int(n.get('memoryAvailableMb') or 0))}/{_fmt_gb(int(n.get('memoryTotalMb') or 0))} free" if int(n.get("memoryTotalMb") or 0) > 0 else "")
            + ")"
            for n in details
        ]
        print(f"  Nodes:   {', '.join(formatted_nodes)}")
    elif available_nodes:
        print(f"  Nodes:   {', '.join(available_nodes)}")

    for field in addon.get("configSchema") or []:
        if not isinstance(field, Mapping):
            continue
        key = str(field["key"])
        ftype = str(field.get("type") or "string")
        label = str((field.get("label") or {}).get("en") or key)
        if ftype == "secret":
            env_name = str(field.get("env") or "")
            if env_name in secrets_map or not field.get("required"):
                continue
            val = _prompt_line(label, "set" if secrets_status.get(env_name) else "", secret=True)
            if val and env_name:
                secrets_map[env_name] = val
            continue
        if key in config:
            continue
        # Only prompt placement/required fields and main defaults in the quick wizard
        if not field.get("required") and field.get("group") != "placement":
            continue
        default_val = str(merged_cfg.get(key) if merged_cfg.get(key) is not None else field.get("default") or "")
        if key == "domain" and not default_val and addon.get("suggestedDomain"):
            default_val = str(addon["suggestedDomain"])
        if key == "node" and details:
            chosen_region = str(config.get("region") or merged_cfg.get("region") or "").strip()
            chosen_exposure = str(config.get("exposure") or merged_cfg.get("exposure") or "").strip()
            node_in_region = any(str(n.get("name")) == default_val and str(n.get("region")) == chosen_region for n in details)
            if not default_val or not node_in_region:
                default_val = _pick_cli_default_node(
                    details,
                    region=chosen_region,
                    addon_id=str(addon.get("id") or ""),
                    preset_id=current_preset,
                    exposure=chosen_exposure,
                    manager_node=manager_node,
                )
            if field.get("required"):
                label = f"{label} (required, stateful volumes)"
        entered = _prompt_line(label, default_val)
        while field.get("required") and not entered:
            print(f"  {key} is required.")
            entered = _prompt_line(label, default_val)
        if entered != "":
            config[key] = entered
            merged_cfg[key] = entered
    return current_preset, config, secrets_map


def _stream_addon_apply(
    client: common.ControlClient,
    addon_id: str,
    body: Dict[str, Any],
    *,
    timeout: int,
    quiet: bool,
) -> Dict[str, Any]:
    try:
        for event in client.stream("POST", f"/v1/addons/{addon_id}/apply/stream", body, timeout=timeout):
            status = str(event.get("status") or "")
            if status == "done":
                return event.get("result") if isinstance(event.get("result"), dict) else {}
            if status == "fail":
                raise LumaError(str(event.get("message") or "addon deployment failed"))
            if not quiet and event.get("name"):
                print(f"[{status or 'step'}] {event.get('name')}: {event.get('message') or ''}", flush=True)
        return {}
    except LumaError as exc:
        if "404" in str(exc):
            return client.request("POST", f"/v1/addons/{addon_id}/apply", body, timeout=timeout)
        raise


def _detect_source_dir(explicit: Optional[Path], source_rel: str) -> Optional[Path]:
    if explicit is not None:
        resolved = explicit.expanduser().resolve()
        if not resolved.is_dir():
            raise LumaError(f"addon source directory not found: {resolved}")
        return resolved
    if source_rel:
        candidate = (Path.cwd() / source_rel).resolve()
        if candidate.is_dir():
            return candidate
    return None


def _deploy_from_source(
    args: argparse.Namespace,
    client: common.ControlClient,
    addon: Mapping[str, Any],
    source_dir: Path,
    *,
    quiet: bool,
) -> Dict[str, Any]:
    from .builds import cmd_build

    addon_id = str(addon["id"])
    stack = str(addon["stack"])
    rendered_resp = client.request("POST", f"/v1/addons/{addon_id}/render", {"forSourceBuild": True})
    rendered = rendered_resp.get("rendered") or {}
    sidecar_yaml = str(rendered.get("sidecarYaml") or "")
    compose_yaml = str(rendered.get("composeYaml") or "")

    sidecar_name = ".luma.addon.compose.yml"
    compose_name = ".luma.addon.docker-compose.yml"
    sidecar_path = source_dir / sidecar_name
    compose_path = source_dir / compose_name

    # Point the temporary sidecar at the temporary compose file (except observe which uses its own compose dir)
    if addon_id == "observe":
        sidecar_doc = yaml.safe_load((source_dir / "luma.compose.yml").read_text(encoding="utf-8"))
        if isinstance(sidecar_doc, dict):
            sidecar_doc["name"] = stack
            region = str((addon.get("config") or {}).get("region") or "").strip()
            if region:
                sidecar_doc["region"] = region
            sidecar_yaml = yaml.safe_dump(sidecar_doc, sort_keys=False, allow_unicode=True)
        compose_path = None
    else:
        sidecar_doc = yaml.safe_load(sidecar_yaml)
        if isinstance(sidecar_doc, dict):
            sidecar_doc["compose"] = compose_name
            sidecar_yaml = yaml.safe_dump(sidecar_doc, sort_keys=False, allow_unicode=True)

    try:
        sidecar_path.write_text(sidecar_yaml, encoding="utf-8")
        if compose_path is not None:
            compose_path.write_text(compose_yaml, encoding="utf-8")
        from .parser import build_parser

        argv: List[str] = [
            "build",
            "local",
            str(source_dir),
            "--compose-sidecar",
            sidecar_name,
            "--repo-url",
            f"local/{stack}",
            "--workflow-app",
            stack,
            "--accept-workflow-change",
            "--platform",
            str(getattr(args, "platform", "linux/amd64") or "linux/amd64"),
            "--timeout",
            str(int(getattr(args, "timeout", 7200) or 7200)),
        ]
        if quiet:
            argv.extend(["--format", "json"])
        elif common._output_format(args) != "text":
            argv.extend(["--format", common._output_format(args)])
        if getattr(args, "control_context", None):
            argv.extend(["--control-context", str(args.control_context)])
        if getattr(args, "control_url", None):
            argv.extend(["--control-url", str(args.control_url)])
        if getattr(args, "token", None):
            argv.extend(["--token", str(args.token)])
        if getattr(args, "insecure", False):
            argv.append("--insecure")
        if getattr(args, "resolve_ip", None):
            argv.extend(["--resolve-ip", str(args.resolve_ip)])
        build_args = build_parser().parse_args(argv)
        build_args._raw_argv = list(argv)
        code = cmd_build(build_args)
        if code != 0:
            raise LumaError(f"local source build for addon {addon_id!r} failed")
        return {"builtFromSource": str(source_dir), "stack": stack}
    finally:
        sidecar_path.unlink(missing_ok=True)
        if compose_path is not None:
            compose_path.unlink(missing_ok=True)


def _deploy_static_bundle_from_source(
    args: argparse.Namespace,
    client: common.ControlClient,
    addon: Mapping[str, Any],
    source_dir: Path,
    *,
    quiet: bool,
) -> Dict[str, Any]:
    from ..control.dashboard_bundle import package_dashboard_directory

    addon_id = str(addon["id"])
    dist_dir = source_dir
    if not (dist_dir / "index.html").is_file():
        repo_root = source_dir.parent if (source_dir.parent / "package.json").is_file() else source_dir
        if not (repo_root / "package.json").is_file():
            raise LumaError(f"cannot build static bundle from {source_dir}: missing package.json and index.html")
        if not quiet:
            print(f"[step] Building dashboard bundle locally in {repo_root} (no Docker image required)...", flush=True)
        proc = subprocess.run(
            ["npm", "run", "build:dashboard"],
            cwd=str(repo_root),
            check=False,
        )
        if proc.returncode != 0:
            raise LumaError("local `npm run build:dashboard` failed")
        built_candidate = repo_root / "luma" / "assets" / "dashboard"
        if not (built_candidate / "index.html").is_file():
            raise LumaError(f"built dashboard assets not found at {built_candidate}")
        dist_dir = built_candidate

    version = str(getattr(args, "version", "") or "").strip()
    if not quiet:
        print(f"[step] Packaging static bundle from {dist_dir}...", flush=True)
    archive_bytes, manifest = package_dashboard_directory(dist_dir, version=version)
    qs = urllib.parse.urlencode(
        {
            "version": manifest["version"],
            "source": "cli-source",
            "sha256": manifest["sha256"],
        }
    )
    if not quiet:
        print(
            f"[step] Uploading {len(archive_bytes) / 1024:.1f} KiB ({manifest['fileCount']} files) to Control...",
            flush=True,
        )
    return client.upload_bytes(
        f"/v1/addons/{addon_id}/upload?{qs}",
        archive_bytes,
        timeout=min(int(getattr(args, "timeout", 300) or 300), 600),
    )


def _upload_static_bundle_file(
    args: argparse.Namespace,
    client: common.ControlClient,
    addon_id: str,
    bundle_path: Path,
    *,
    quiet: bool,
) -> Dict[str, Any]:
    resolved = bundle_path.expanduser().resolve()
    if not resolved.is_file():
        raise LumaError(f"bundle archive not found: {resolved}")
    raw_bytes = resolved.read_bytes()
    version = str(getattr(args, "version", "") or "").strip()
    params: Dict[str, str] = {"source": "cli-bundle"}
    if version:
        params["version"] = version
    qs = urllib.parse.urlencode(params)
    if not quiet:
        print(f"[step] Uploading {resolved.name} ({len(raw_bytes) / 1024:.1f} KiB) to Control...", flush=True)
    return client.upload_bytes(
        f"/v1/addons/{addon_id}/upload?{qs}",
        raw_bytes,
        timeout=min(int(getattr(args, "timeout", 300) or 300), 600),
    )


def cmd_addon(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = common._control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    output_format = common._output_format(args)
    quiet = common._quiet(args) or output_format != "text"
    cmd = args.addon_command

    if cmd == "list":
        data = client.request("GET", "/v1/addons")
        if output_format != "text":
            common._print_success(args, data)
            return 0
        addons_list = data.get("addons") or []
        rows = [
            [
                str(item.get("id") or ""),
                str(item.get("name") or ""),
                str(item.get("status") or ""),
                str((item.get("auth") or {}).get("mode") or ""),
                str(item.get("consoleUrl") or item.get("url") or "-"),
                str(item.get("preset") or ""),
            ]
            for item in addons_list
        ]
        common._print_table(["ADDON", "NAME", "STATUS", "AUTH", "CONSOLE", "PRESET"], rows)
        return 0

    if cmd == "show":
        data = client.request("GET", f"/v1/addons/{args.addon_id}")
        if output_format != "text":
            common._print_success(args, data)
            return 0
        item = data.get("addon") or {}
        print(f"{item.get('name')} ({item.get('id')})")
        print(f"  Status:     {item.get('status')}")
        print(f"  Stack:      {item.get('stack')}")
        print(f"  Preset:     {item.get('preset')}")
        print(f"  Auth mode:  {(item.get('auth') or {}).get('mode')} (ssoReady={item.get('ssoReady')})")
        print(f"  Console:    {item.get('consoleUrl') or item.get('url') or '(not configured)'}")
        bundle_status = item.get("bundleStatus") if isinstance(item.get("bundleStatus"), Mapping) else None
        if bundle_status:
            active_rel = bundle_status.get("activeRelease") if isinstance(bundle_status.get("activeRelease"), Mapping) else None
            print(f"  Bundle mode: {bundle_status.get('mode')} (store={bundle_status.get('storeDir')})")
            if active_rel:
                print(
                    f"  Release:    {active_rel.get('version')} "
                    f"(id={active_rel.get('id')}, sha256={str(active_rel.get('sha256') or '')[:12]})"
                )
            releases = bundle_status.get("releases") or []
            if releases:
                print(f"  History:    {len(releases)} release(s) cached for instant rollback")
        eff_req = item.get("effectiveRequirements") or item.get("requirements") or {}
        if eff_req.get("recommendedMemoryMb") or eff_req.get("minMemoryMb"):
            print(
                f"  Resources:  min {_fmt_gb(int(eff_req.get('minMemoryMb') or 0))}, "
                f"recommended >= {_fmt_gb(int(eff_req.get('recommendedMemoryMb') or 0))} "
                f"(pinPolicy={eff_req.get('pinPolicy') or 'optional'})"
            )
        cfg = item.get("config") or {}
        if cfg:
            print("  Config:")
            for k, v in cfg.items():
                print(f"    {k} = {v if v != '' else '(empty)'}")
        sec = item.get("secretsStatus") or {}
        if sec:
            print("  Secrets:")
            for k, configured in sec.items():
                print(f"    {k}: {'configured' if configured else 'unset'}")
        return 0

    if cmd == "configure":
        preset, config, secrets_map = _collect_flag_overrides(args)
        if getattr(args, "interactive", False) and sys.stdin.isatty():
            info = client.request("GET", f"/v1/addons/{args.addon_id}")
            preset, config, secrets_map = _interactive_wizard(
                info.get("addon") or {},
                available_regions=list(info.get("availableRegions") or []),
                available_nodes=list(info.get("availableNodes") or []),
                node_details=list(info.get("nodeDetails") or []),
                manager_node=str(info.get("managerNode") or ""),
                preset=preset,
                config=config,
                secrets_map=secrets_map,
            )
        payload: Dict[str, Any] = {
            "config": config,
            "secrets": secrets_map,
            "rotateSecrets": bool(getattr(args, "rotate_secrets", False)),
        }
        inherit = _collect_inherit_payload(args)
        if inherit:
            payload["inherit"] = inherit
        if preset:
            payload["preset"] = preset
        result = client.request("POST", f"/v1/addons/{args.addon_id}/configure", payload)
        if getattr(args, "apply", False):
            apply_res = _stream_addon_apply(client, args.addon_id, {"origin": "cli"}, timeout=args.timeout, quiet=quiet)
            result["deployment"] = apply_res
        if output_format != "text":
            common._print_success(args, result)
            return 0
        addon_view = result.get("addon") or {}
        print(f"[ok] Configured {addon_view.get('name')} ({addon_view.get('id')})")
        if result.get("generatedSecrets"):
            print(f"  Auto-generated secrets: {', '.join(result['generatedSecrets'])}")
        if result.get("inheritedCredentials"):
            print(f"  Inherited Control credentials: {', '.join(result['inheritedCredentials'])}")
        if addon_view.get("consoleUrl"):
            print(f"  Console: {addon_view['consoleUrl']} (auth: {(addon_view.get('auth') or {}).get('mode')})")
        if getattr(args, "apply", False):
            print("[ok] Addon redeployed with updated configuration.")
        return 0

    if cmd == "install":
        info = client.request("GET", f"/v1/addons/{args.addon_id}")
        addon_view = info.get("addon") or {}
        node_details = list(info.get("nodeDetails") or [])
        manager_node = str(info.get("managerNode") or "")
        was_installed = bool(addon_view.get("installed"))
        prev_node = str((addon_view.get("config") or {}).get("node") or "")
        preset, config, secrets_map = _collect_flag_overrides(args)
        images_map = _parse_kv_list(list(getattr(args, "image", []) or []), flag="--image")
        if (
            str(addon_view.get("kind") or "compose") != "static-bundle"
            and sys.stdin.isatty()
            and not getattr(args, "yes", False)
            and output_format == "text"
        ):
            preset, config, secrets_map = _interactive_wizard(
                addon_view,
                available_regions=list(info.get("availableRegions") or []),
                available_nodes=list(info.get("availableNodes") or []),
                node_details=node_details,
                manager_node=manager_node,
                preset=preset,
                config=config,
                secrets_map=secrets_map,
            )
        cfg_payload: Dict[str, Any] = {
            "config": config,
            "secrets": secrets_map,
            "rotateSecrets": bool(getattr(args, "rotate_secrets", False)),
            "requireComplete": True,
        }
        inherit = _collect_inherit_payload(args)
        if inherit:
            cfg_payload["inherit"] = inherit
        if preset:
            cfg_payload["preset"] = preset
        configured = client.request("POST", f"/v1/addons/{args.addon_id}/configure", cfg_payload)
        addon_view = configured.get("addon") or addon_view
        if not quiet:
            chosen_node = str((addon_view.get("config") or {}).get("node") or "")
            warn_text = _resource_warning_text(
                addon_view,
                node_details,
                manager_node=manager_node,
                target_node_name=chosen_node,
                preset_id=str(addon_view.get("preset") or ""),
                same_node_redeploy=bool(was_installed and prev_node and prev_node == chosen_node),
            )
            if warn_text:
                print(warn_text)
        if not quiet and configured.get("generatedSecrets"):
            print(f"[ok] Auto-generated internal secrets: {', '.join(configured['generatedSecrets'])}")
        if not quiet and configured.get("inheritedCredentials"):
            print(f"[ok] Inherited Control credentials: {', '.join(configured['inheritedCredentials'])}")

        addon_kind = str(addon_view.get("kind") or "compose")
        explicit_bundle = getattr(args, "bundle", None)
        explicit_source = getattr(args, "source", None)
        if addon_kind == "static-bundle":
            if explicit_bundle is not None:
                deploy_res = _upload_static_bundle_file(args, client, args.addon_id, explicit_bundle, quiet=quiet)
            elif explicit_source is not None:
                source_dir = _detect_source_dir(explicit_source, str(addon_view.get("sourceDir") or ""))
                assert source_dir is not None
                deploy_res = _deploy_static_bundle_from_source(args, client, addon_view, source_dir, quiet=quiet)
            else:
                deploy_res = _stream_addon_apply(
                    client,
                    args.addon_id,
                    {"origin": "cli"},
                    timeout=min(int(getattr(args, "timeout", 300) or 300), 600),
                    quiet=quiet,
                )
        else:
            source_dir = _detect_source_dir(explicit_source, str(addon_view.get("sourceDir") or ""))
            if not images_map and source_dir is not None and (explicit_source is not None or not addon_view.get("canApplyInPlace")):
                deploy_res = _deploy_from_source(args, client, addon_view, source_dir, quiet=quiet)
            else:
                deploy_res = _stream_addon_apply(
                    client,
                    args.addon_id,
                    {"images": images_map, "origin": "cli"},
                    timeout=int(getattr(args, "timeout", 7200) or 7200),
                    quiet=quiet,
                )
        final_info = client.request("GET", f"/v1/addons/{args.addon_id}")
        final_addon = final_info.get("addon") or addon_view
        out = {"addon": final_addon, "generatedSecrets": configured.get("generatedSecrets") or [], "deployment": deploy_res}
        if output_format != "text":
            common._print_success(args, out)
            return 0
        print(f"\n[ok] {final_addon.get('name')} is ready.")
        if final_addon.get("consoleUrl"):
            print(f"  Console: {final_addon['consoleUrl']}")
        auth_mode = (final_addon.get("auth") or {}).get("mode")
        if auth_mode == "control-sso":
            print("  Sign-in: Unified Luma Control SSO (reuses your Control Passkey/session and `luma login` token)")
        elif auth_mode == "forward-auth":
            print("  Sign-in: Luma Control forwardAuth (reuses your Control session)")
        if str(final_addon.get("id") or "") == "builder":
            online_runners = -1
            try:
                bstatus = client.request("GET", "/v1/builder/status")
                online_runners = sum(1 for r in (bstatus.get("runners") or []) if isinstance(r, dict) and r.get("online") and not r.get("revoked"))
            except Exception:
                online_runners = 0
            if online_runners == 0:
                builder_origin = str(final_addon.get("url") or "https://build.example.com").rstrip("/")
                print("\nNext step: enroll at least one build runner (0 currently online):")
                print("  1. luma builder token create my-runner")
                print(f"  2. luma runner join --url {builder_origin} --token <RUNNER_TOKEN>")
        return 0

    if cmd == "rollback":
        payload: Dict[str, Any] = {}
        release_id = str(getattr(args, "release", "") or "").strip()
        if release_id:
            payload["releaseId"] = release_id
        result = client.request("POST", f"/v1/addons/{args.addon_id}/rollback", payload)
        if output_format != "text":
            common._print_success(args, result)
            return 0
        rel = result.get("release") or {}
        print(
            f"[ok] Rolled back {args.addon_id} to {rel.get('version') or rel.get('id')} "
            f"(sha256={str(rel.get('sha256') or '')[:12]})."
        )
        return 0

    if cmd == "uninstall":
        if not getattr(args, "yes", False) and sys.stdin.isatty():
            answer = input(f"Uninstall addon {args.addon_id!r}? Existing data volumes are preserved unless --delete-storage is set [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Cancelled.")
                return 0
        result = client.request("POST", f"/v1/addons/{args.addon_id}/uninstall", {
            "purgeSecrets": bool(getattr(args, "purge_secrets", False)),
            "deleteStorage": bool(getattr(args, "delete_storage", False)),
        })
        if output_format != "text":
            common._print_success(args, result)
            return 0
        print(f"[ok] Uninstalled addon {result.get('addonId')} (SSO sessions revoked).")
        return 0

    raise LumaError(f"unknown addon command: {cmd}")
