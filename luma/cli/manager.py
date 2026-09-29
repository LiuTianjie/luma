"""Luma CLI: manager commands."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict
from ..bootstrap import bootstrap_manager_local, refresh_manager_control_local, setup_egress
from ..cloudflare import find_zone
from ..config import LumaConfig, load_config, save_config
from ..control.state import is_initialized as control_state_is_initialized, load_state, new_state, state_path
from ..agent import DEFAULT_AGENT_CONFIG, _current_install_layout, install_node_agent
from ..errors import LumaError
from ..installer import luma_installer_command
from ..installation import runtime_record, installer_environment
from ..local import LocalExecutor
from ..manager import manager_ip_change
from ..profiles import PROFILES
from ..userconfig import ensure_interactive_config
from . import common
from .common import UPDATE_DETACHED_ENV, UPDATE_REEXEC_ENV, _control_context, _output_format, _print_success, _print_table, log


def _host_from_hostport(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    if text.count(":") == 1:
        return text.rsplit(":", 1)[0]
    if text.count(":") > 1:
        return text
    return text


def _find_nomad_cli() -> str:
    candidates = [
        shutil.which("nomad"),
        "/usr/local/bin/nomad",
        "/opt/homebrew/bin/nomad",
        "/usr/bin/nomad",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def local_nomad_node_info() -> tuple[str, str]:
    nomad = _find_nomad_cli()
    if not nomad:
        raise LumaError("Nomad CLI not found after install")
    result = subprocess.run(
        [nomad, "node", "status", "-self", "-json"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise LumaError(f"Nomad local node is not ready: {detail or 'nomad node status failed'}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise LumaError("Nomad local node status returned invalid JSON") from exc
    node_id = str(data.get("ID") or "").strip()
    meta = data.get("Meta") if isinstance(data.get("Meta"), dict) else {}
    node_name = str(meta.get("luma_node_name") or data.get("Name") or "").strip()
    if not node_id:
        raise LumaError("Nomad local node status did not include a node ID")
    return node_name or os.uname().nodename, node_id


def _install_node_agent_from_token(
    *,
    endpoint: str,
    agent_token: str,
    node_name: str,
    node_id: str = "",
    insecure: bool = False,
    resolve_ip: str | None = None,
) -> None:
    install_node_agent(
        endpoint=endpoint,
        token=agent_token,
        node_name=node_name,
        node_id=node_id,
        insecure=insecure,
        resolve_ip=resolve_ip,
    )


def cmd_bootstrap(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.bootstrap_command == "manager":
        _apply_bootstrap_port_overrides(config, http_port=args.http_port, https_port=args.https_port)
        node = config.get_node(args.node) if args.node else (config.default_manager() or _local_node(args.profile))
        if not node:
            raise LumaError("no manager node configured. Add a node or pass --node.")
        profile = PROFILES[args.profile]
        keys = ["CLOUDFLARE_API_TOKEN", "TRAEFIK_ACME_EMAIL", "TAILSCALE_AUTHKEY", "LUMA_SUDO_PASSWORD"]
        if not _dns_target_for_bootstrap(config, node) and sys.stdin.isatty():
            keys.append("LUMA_DNS_EDGE_TARGET")
        if not args.skip_egress and "egress" in profile.roles:
            keys.append("EGRESS_SUBSCRIPTION_URL")
        ensure_interactive_config("manager", keys=keys)
        _ensure_cloudflare_dns_from_local_config(config, args.domain, node)
        state = _control_state_for_bootstrap(args.domain, overwrite=args.overwrite_control_state)
        _attach_control_secrets(state, config)
        bootstrap_manager_local(config, node, profile, args.domain, state, run_egress=not args.skip_egress, emit=log, overwrite_control_state=args.overwrite_control_state)
        control_url = _control_url(args.domain, args.https_port or _config_https_port(config))
        print("Bootstrap complete")
        print(f"Control domain: {args.domain}")
        print(f"Control URL: {control_url}")
        print(f"Cluster: {state['clusterId']}")
        print(f"Management token: {state['deployToken']}")
        print(f"Node join token: {state['joinToken']}")
        print(f"Dashboard: {control_url.rstrip('/')}/dashboard/")
        print("Next:")
        print("  1. Open the dashboard and paste the management token")
        print("  2. Applications → Create application → hello-world first install")
        print("     (internal smoke service; no extra DNS, Tailscale, or registry)")
        print("  3. Optional later: Dashboard → First install for Cloudflare extras, Tailscale, egress, or registry")
        print("If a step failed, fix the printed cause and rerun:")
        print(f"  luma bootstrap manager --domain {args.domain}")
        print("Layer repair: luma egress setup | luma tailscale connect | luma doctor")
        print("Join additional nodes:")
        for label, command in _node_join_examples(control_url, str(state["joinToken"])):
            print(f"  {label}: {command}")
        return 0
    raise LumaError(f"unknown bootstrap command: {args.bootstrap_command}")


def cmd_update(args: argparse.Namespace) -> int:
    if args.update_command not in {None, "manager", "fleet"}:
        raise LumaError(f"unknown update command: {args.update_command}")
    if bool(getattr(args, "detach", False)) and os.environ.get(UPDATE_DETACHED_ENV) != "1":
        if args.update_command == "fleet":
            raise LumaError("--detach is only supported for manager updates")
        should_refresh = args.update_command == "manager" or _manager_refresh_decision(args)[0]
        if not should_refresh:
            raise LumaError("--detach requires a manager host with local control state, or the explicit `update manager` target")
        return _start_detached_manager_update(args)
    if os.environ.get(UPDATE_REEXEC_ENV) == "1":
        print("[skip] Luma CLI already updated in this run")
    else:
        print("[start] Update Luma CLI")
        manager_refresh = args.update_command == "manager" or _manager_refresh_decision(args)[0]
        _run_luma_installer(
            install_ref=_effective_update_install_ref(args),
            skip_node_agent_refresh=manager_refresh,
        )
        print("[ok] Luma CLI updated")
        _reexec_after_luma_update()
    if args.update_command == "fleet":
        return _cmd_update_fleet(args)
    if args.update_command == "manager":
        print("[info] Role: manager")
        print("[info] Manager control-plane refresh forced")
        print("[start] Refresh manager control plane")
        _refresh_manager_control(args)
        print("[ok] Manager control plane refreshed")
        _try_refresh_manager_agent(args)
        print("[ok] Manager update complete")
        return 0

    should_refresh, reason = _manager_refresh_decision(args)
    if should_refresh:
        print("[info] Role: manager")
        print(f"[info] Manager control-plane refresh required: {reason}")
        print("[start] Refresh manager control plane")
        _refresh_manager_control(args)
        print("[ok] Manager control plane refreshed")
        _try_refresh_manager_agent(args)
        print("[ok] Manager update complete")
        return 0

    if _local_agent_config() or _safe_local_nomad_node_id():
        print("[info] Role: joined node")
        _refresh_joined_node_agent(args)
        print("[ok] Joined node update complete")
        return 0

    print("[info] Role: client")
    print(f"[skip] Manager control-plane refresh skipped: {reason}")
    print("[skip] Node agent refresh skipped: no local joined-node metadata found")
    return 0


def _effective_update_install_ref(args: argparse.Namespace) -> str | None:
    return getattr(args, "fleet_install_ref", None) or getattr(args, "install_ref", None)


def _cmd_update_fleet(args: argparse.Namespace) -> int:
    should_refresh, reason = _manager_refresh_decision(args)
    if should_refresh:
        print("[info] Role: manager")
        print(f"[info] Manager control-plane refresh required: {reason}")
        print("[start] Refresh manager control plane")
        _refresh_manager_control(args)
        print("[ok] Manager control plane refreshed")
        _try_refresh_manager_agent(args)
    else:
        print(f"[skip] Local manager control-plane refresh skipped: {reason}")
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    print("[start] Update Luma on registered non-manager nodes")
    result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).update_fleet(
        install_ref=str(_effective_update_install_ref(args) or ""),
        include_all=bool(getattr(args, "all", False)),
        include_manager=bool(getattr(args, "include_manager", False)),
        timeout=int(getattr(args, "timeout", 900) or 900),
    )
    if _output_format(args) != "text":
        _print_success(args, result)
        return 1 if int(result.get("failed") or 0) else 0
    rows = []
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            [
                str(item.get("nodeName") or ""),
                str(item.get("region") or "-"),
                str(item.get("os") or "-"),
                str(item.get("status") or "-"),
                str(item.get("message") or "-"),
            ]
        )
    if rows:
        _print_table(["node", "region", "os", "status", "message"], rows)
    else:
        print("No ready node agents found")
    print(
        f"[ok] Fleet update finished: {int(result.get('succeeded') or 0)} succeeded, "
        f"{int(result.get('failed') or 0)} failed, {int(result.get('skipped') or 0)} skipped"
    )
    return 1 if int(result.get("failed") or 0) else 0


def _try_refresh_manager_agent(args: argparse.Namespace) -> None:
    state = _existing_control_state()
    if not state:
        print("[skip] Manager node agent skipped: local manager control state not found")
        return
    domain = str(state.get("domain") or "").strip()
    token = str(state.get("joinToken") or state.get("deployToken") or "").strip()
    if not domain or not token:
        print("[skip] Manager node agent skipped: control domain or token is missing")
        return
    try:
        endpoint = _control_url(domain, args.https_port or _config_https_port(load_config(args.config)))
        _refresh_local_node_agent(endpoint=endpoint, token=token, insecure=bool(getattr(args, "insecure", False)), resolve_ip=getattr(args, "resolve_ip", None), allow_skip=True)
    except LumaError as exc:
        print(f"[skip] Manager node agent skipped: {exc}")


def _refresh_joined_node_agent(args: argparse.Namespace) -> None:
    if getattr(args, "control_url", None) or getattr(args, "token", None):
        try:
            endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        except LumaError as exc:
            print(f"[skip] Luma node agent skipped: joined node control context is unavailable ({exc})")
            return
        try:
            _refresh_local_node_agent(endpoint=endpoint, token=token, insecure=insecure, resolve_ip=resolve_ip, allow_skip=False)
        except LumaError as exc:
            if _node_agent_credentials_unsupported(exc) or _node_agent_credentials_unregistered(exc):
                print(f"[skip] Luma node agent skipped: {exc}")
                return
            raise
        return

    config = _local_agent_config()
    if config:
        endpoint = str(config.get("endpoint") or "")
        token = str(config.get("token") or "")
        node_name = str(config.get("nodeName") or "")
        node_id = str(config.get("nodeId") or "")
        if endpoint and token and node_name:
            print("[start] Refresh Luma node agent from local metadata")
            _install_node_agent_from_token(
                endpoint=endpoint,
                agent_token=token,
                node_name=node_name,
                node_id=node_id,
                insecure=bool(config.get("insecure")),
                resolve_ip=str(config.get("resolveIp") or "") or None,
            )
            print("[ok] Luma node agent refreshed")
            return
    try:
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    except LumaError as exc:
        print(f"[skip] Luma node agent skipped: joined node control context is unavailable ({exc})")
        return
    try:
        _refresh_local_node_agent(endpoint=endpoint, token=token, insecure=insecure, resolve_ip=resolve_ip, allow_skip=False)
    except LumaError as exc:
        if _node_agent_credentials_unsupported(exc) or _node_agent_credentials_unregistered(exc):
            print(f"[skip] Luma node agent skipped: {exc}")
            return
        raise


def _refresh_local_node_agent(
    *,
    endpoint: str,
    token: str,
    insecure: bool,
    resolve_ip: str | None,
    allow_skip: bool,
) -> None:
    local_name, local_id = _safe_local_nomad_node_info()
    if not local_id:
        message = "local Nomad node id is unavailable"
        if allow_skip:
            raise LumaError(message)
        raise LumaError(message + "; run this command on a joined node")
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    print("[start] Request node agent credentials")
    issued = client.issue_agent_token(node_name=local_name, node_id=local_id)
    node_name = str(issued.get("nodeName") or "")
    if not node_name:
        raise LumaError(f"this Nomad node is not registered in Luma Control: hostname={local_name}, nodeId={local_id}")
    agent_token = str(issued.get("agentToken") or "")
    if not agent_token:
        raise LumaError("control API did not return node agent credentials")
    print("[start] Install Luma node agent")
    _install_node_agent_from_token(
        endpoint=endpoint,
        agent_token=agent_token,
        node_name=node_name,
        node_id=local_id,
        insecure=insecure,
        resolve_ip=resolve_ip,
    )
    print("[ok] Luma node agent installed")


def _node_agent_credentials_unsupported(exc: LumaError) -> bool:
    message = str(exc)
    return "does not support node-agent credentials" in message or (
        "control API error 404" in message and "not found" in message
    )


def _node_agent_credentials_unregistered(exc: LumaError) -> bool:
    return "nodeName or nodeId must match a registered node" in str(exc)


def _local_agent_config() -> Dict[str, Any] | None:
    path = DEFAULT_AGENT_CONFIG
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        pass
    result = LocalExecutor().sudo_result(f"test -f {shlex.quote(str(path))} && cat {shlex.quote(str(path))}")
    if result.code != 0 or not result.output.strip():
        return None
    try:
        raw = result.output.strip()
        start = raw.find("{")
        end = raw.rfind("}")
        data = json.loads(raw[start : end + 1] if start >= 0 and end >= start else raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _safe_local_nomad_node_info() -> tuple[str, str]:
    try:
        return local_nomad_node_info()
    except LumaError:
        return "", ""


def _safe_local_nomad_node_id() -> str:
    return _safe_local_nomad_node_info()[1]


def _manager_refresh_decision(args: argparse.Namespace) -> tuple[bool, str]:
    if _manager_update_options_provided(args):
        return True, "manager update options were provided"
    state = _existing_control_state()
    if not state:
        if _manager_state_requires_privilege():
            raise LumaError(
                "manager control state exists but is not readable by this user. "
                "Run `sudo luma update` (or set LUMA_SUDO_PASSWORD), then retry; "
                "the 401 from the joined-node path is not a management-token rotation."
            )
        return False, "no local manager control state found"
    domain = str(state.get("domain") or "").strip()
    if not domain:
        return False, "local manager control state has no domain; run luma update manager --domain <control-domain>"
    return True, "local manager control state found"


def _manager_state_requires_privilege() -> bool:
    """Detect a root-owned manager state directory before falling back to login context.

    A manager's state is intentionally private.  When a non-root operator lacks
    sudo credentials, treating the unreadable state as a joined node makes
    ``luma update`` call the public node-agent API with an unrelated client
    token, producing a misleading 401 instead of an actionable local error.
    """
    directory = state_path().parent
    try:
        return directory.is_dir() and not os.access(directory, os.R_OK | os.X_OK)
    except OSError:
        return False


def _manager_update_options_provided(args: argparse.Namespace) -> bool:
    if args.domain or args.node or args.http_port is not None or args.https_port is not None:
        return True
    if args.skip_egress or args.overwrite_control_state:
        return True
    if getattr(args, "profile", "single-node") != "single-node":
        return True
    return False


def _run_luma_installer(*, install_ref: str | None = None, skip_node_agent_refresh: bool = False) -> None:
    env = os.environ.copy()
    command, exact_ref = luma_installer_command(install_ref, environ=env)
    env["LUMA_INSTALL_REF"] = exact_ref
    try:
        env = installer_environment(env, runtime_record())
    except (ValueError, OSError) as exc:
        raise LumaError(f"Cannot prepare Luma installation: {exc}") from exc
    # A manager update can be launched from the root-owned node-agent terminal
    # even though the supported Luma installation belongs to the operator. Keep
    # the running executable's layout instead of silently creating /root/.local
    # and rewriting the healthy systemd unit to that incomplete environment.
    layout = _current_install_layout()
    if layout:
        user_home, install_home, bin_dir = layout
        env.setdefault("LUMA_USER_HOME", str(user_home))
        env.setdefault("LUMA_INSTALL_HOME", str(install_home))
        env.setdefault("LUMA_BIN_DIR", str(bin_dir))
    # Manager refresh already reinstalls the node agent after Control is healthy.
    # Restarting it from the bootstrap installer can kill an update launched by
    # that same service before the control image and routes are reconciled.
    if skip_node_agent_refresh:
        env["LUMA_SKIP_NODE_AGENT_SERVICE_REFRESH"] = "1"
    subprocess.run(command, shell=True, check=True, env=env)


def _start_detached_manager_update(args: argparse.Namespace) -> int:
    command = _current_luma_command()
    if not command:
        raise LumaError("unable to locate the Luma executable for detached manager update")
    raw_argv = list(getattr(args, "_raw_argv", ()) or sys.argv[1:])
    state_root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    update_dir = state_root / "luma" / "updates"
    try:
        update_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LumaError(f"unable to create detached update state directory {update_dir}: {exc}") from exc
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"manager-{stamp}-{os.getpid()}"
    log_path = update_dir / f"{run_id}.log"
    status_path = update_dir / f"{run_id}.status"
    try:
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise LumaError(f"unable to create detached update log {log_path}: {exc}") from exc
    log_file = os.fdopen(log_fd, "w", encoding="utf-8")
    env = os.environ.copy()
    env[UPDATE_DETACHED_ENV] = "1"
    env["LUMA_UPDATE_STATUS_PATH"] = str(status_path)
    env["LUMA_UPDATE_LOG_PATH"] = str(log_path)
    wrapper = (
        'umask 077; "$@"; code=$?; '
        'printf "%s\\n" "$code" > "$LUMA_UPDATE_STATUS_PATH"; exit "$code"'
    )
    if _manager_update_needs_transient_unit():
        # A terminal session is a child of luma-node-agent.service. Merely
        # calling setsid() does not escape that service's cgroup, so refreshing
        # the agent would SIGTERM the manager update half way through its own
        # control rollout. Run it as a transient unit with its own cgroup.
        log_file.close()
        unit = f"luma-manager-update-{stamp}-{os.getpid()}"
        unit_wrapper = (
            'umask 077; exec >> "$LUMA_UPDATE_LOG_PATH" 2>&1; '
            '"$@"; code=$?; printf "%s\\n" "$code" > "$LUMA_UPDATE_STATUS_PATH"; exit "$code"'
        )
        forwarded = (
            UPDATE_DETACHED_ENV,
            "LUMA_UPDATE_STATUS_PATH",
            "LUMA_UPDATE_LOG_PATH",
            "LUMA_CONTROL_IMAGE",
            "LUMA_USER_HOME",
            "LUMA_INSTALL_HOME",
            "LUMA_BIN_DIR",
            "LUMA_INSTALL_OWNER",
            "LUMA_PIP_BUILD_ISOLATION",
            "LUMA_PIP_INDEX_URL",
            "LUMA_PIP_WHEELHOUSE",
            "LUMA_PIP_CA_BUNDLE",
        )
        invocation = [
            "systemd-run",
            f"--unit={unit}",
            "--collect",
            "--no-block",
            "--property=Type=exec",
        ]
        invocation.extend(f"--setenv={key}={env[key]}" for key in forwarded if env.get(key) is not None)
        invocation.extend(["sh", "-c", unit_wrapper, "luma-detached-update", *command, *raw_argv])
        try:
            subprocess.run(invocation, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except Exception:
            log_path.unlink(missing_ok=True)
            raise
        print(f"[ok] Detached manager update started (unit {unit})")
        print(f"[info] Log: {log_path}")
        print(f"[info] Status: {status_path} (0 = succeeded; non-zero = failed)")
        print(f"[info] Follow progress: tail -f {shlex.quote(str(log_path))}")
        return 0
    try:
        process = subprocess.Popen(
            ["sh", "-c", wrapper, "luma-detached-update", *command, *raw_argv],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except Exception as exc:
        log_file.close()
        log_path.unlink(missing_ok=True)
        raise LumaError(f"unable to start detached manager update: {exc}") from exc
    log_file.close()
    print(f"[ok] Detached manager update started (pid {process.pid})")
    print(f"[info] Log: {log_path}")
    print(f"[info] Status: {status_path} (0 = succeeded; non-zero = failed)")
    print(f"[info] Follow progress: tail -f {shlex.quote(str(log_path))}")
    return 0


def _manager_update_needs_transient_unit() -> bool:
    return bool(
        sys.platform.startswith("linux")
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
        and os.environ.get("INVOCATION_ID")
        and shutil.which("systemd-run")
    )


def _reexec_after_luma_update() -> None:
    # Managed updates preserve the old interpreter/source. Re-running sys.argv
    # or python -m would therefore execute the old release, not the candidate.
    try:
        identity = runtime_record()
    except (ValueError, OSError) as exc:
        raise LumaError(f"Cannot restart the updated Luma installation: {exc}") from exc
    if identity:
        shim = Path(identity["binDir"]) / "luma"
        if not shim.is_file() or not os.access(shim, os.X_OK):
            raise LumaError("Updated Luma command is unavailable; refusing to continue with the old runtime")
        command = [str(shim)]
    else:
        command = _current_luma_command()
    if not command:
        print("[warn] Unable to re-exec updated Luma CLI; continuing in current process")
        return
    env = os.environ.copy()
    env[UPDATE_REEXEC_ENV] = "1"
    os.execvpe(command[0], [*command, *sys.argv[1:]], env)


def _current_luma_command() -> list[str]:
    candidate = str(sys.argv[0] or "").strip()
    if candidate and (Path(candidate).is_absolute() or "/" in candidate):
        path = Path(candidate)
        if path.suffix == ".py" and not os.access(path, os.X_OK):
            return [sys.executable, "-m", "luma.cli"]
        return [candidate]
    found = shutil.which("luma") or candidate
    return [found] if found else []


def _refresh_manager_control(args: argparse.Namespace) -> None:
    _reject_bootstrap_only_update_options(args)
    domain = _manager_update_domain(args.domain)
    state = _existing_control_state()
    if not state:
        if _manager_state_requires_privilege():
            raise LumaError(
                "manager control state exists but is not readable by this user. "
                "Run `sudo luma update manager` (or set LUMA_SUDO_PASSWORD), then retry."
            )
        raise LumaError("manager control state not found. Run luma bootstrap manager --domain <control-domain> for first install or repair.")
    state["domain"] = domain
    # A managed update runs in a transient systemd unit with no meaningful
    # working directory.  Falling back to the installer checkout's example
    # luma.yaml silently renders production ingress with example settings.
    # The manager-owned config is authoritative unless the operator supplied
    # an explicit --config path.
    manager_config = Path("/opt/luma/luma.yaml")
    config_path = args.config
    if config_path is None and manager_config.exists():
        config_path = manager_config
    config = load_config(config_path)
    node = config.get_node(args.node) if args.node else (config.default_manager() or _local_node(args.profile))
    if not node:
        raise LumaError("no manager node configured. Add a node or pass --node.")
    _ensure_cloudflare_dns_from_local_config(config, domain, node)
    _attach_control_secrets(state, config)
    refresh_manager_control_local(config, node, domain, state, emit=log)


def _reject_bootstrap_only_update_options(args: argparse.Namespace) -> None:
    if args.http_port is not None or args.https_port is not None:
        raise LumaError(
            "luma update manager refreshes existing ingress from control state; "
            "HTTP/HTTPS port overrides are bootstrap-only. Use luma bootstrap manager "
            "--domain <control-domain> for explicit ingress repair."
        )
    if args.skip_egress:
        raise LumaError("luma update no longer runs egress setup. Use luma bootstrap manager --skip-egress only during full bootstrap repair.")
    if args.overwrite_control_state:
        raise LumaError("luma update preserves control state. Use luma bootstrap manager --overwrite-control-state only for explicit repair.")


def _manager_update_domain(explicit_domain: str | None) -> str:
    if explicit_domain:
        return explicit_domain
    state = _existing_control_state()
    if state:
        domain = str(state.get("domain") or "").strip()
        if domain:
            return domain
    raise LumaError("update manager could not infer the control domain. Pass --domain <control-domain> once.")


def _existing_control_state() -> Dict[str, object] | None:
    path = state_path()
    try:
        if control_state_is_initialized():
            # Role detection/prefetch must not cut over a still-running JSON
            # Control. Only the installer imports after stopping its writer.
            legacy_only = path.is_file() and not any((path.parent / name).exists() for name in (
                "control-sqlite-authority.json", "control-sqlite-migration.json",
            ))
            sqlite_path = path.parent / "control.sqlite3"
            if legacy_only and sqlite_path.exists():
                import sqlite3
                from contextlib import closing
                try:
                    with closing(sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
                        metadata = reader.execute("SELECT 1 FROM sqlite_master WHERE name='database_meta'").fetchone()
                        legacy_only = not (metadata and reader.execute("SELECT 1 FROM database_meta WHERE key='state_initialized'").fetchone())
                except sqlite3.Error as exc:
                    raise LumaError("cannot inspect existing Control database; refusing legacy fallback") from exc
            if legacy_only:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise LumaError("invalid legacy Control state")
                return data
            return load_state()
    except PermissionError:
        pass
    # Use the same read-only legacy decision under sudo. Once SQLite exists,
    # legacy JSON must never be used as an authority fallback.
    # The state directory is an argv value, and credentials stay in captured
    # subprocess output; they are never interpolated into commands or errors.
    code = (
        "import json,os,sys; "
        "os.environ['LUMA_CONTROL_STATE_DIR']=sys.argv[1]; "
        "from luma.cli import _existing_control_state; "
        "from luma.control.state import is_initialized,load_state; "
        "sys.exit(3) if not is_initialized() else None; "
        "print(json.dumps(_existing_control_state()))"
    )
    command = shlex.join([sys.executable, "-c", code, str(path.parent)])
    result = LocalExecutor().sudo_result(command)
    if result.code != 0 or not result.output.strip():
        return None
    raw = result.output.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    try:
        data = json.loads(raw[start : end + 1] if start >= 0 and end >= start else raw)
    except ValueError as exc:
        raise LumaError(
            "could not read authoritative Control state through the installed Python runtime; "
            "run this command as the manager owner"
        ) from exc
    return data if isinstance(data, dict) else None


def _apply_bootstrap_port_overrides(config: LumaConfig, *, http_port: int | None, https_port: int | None) -> None:
    if http_port is None and https_port is None:
        return
    defaults = config.raw.setdefault("defaults", {})
    ports = defaults.setdefault("ports", {})
    if http_port is not None:
        ports["traefikHttp"] = http_port
    if https_port is not None:
        ports["traefikHttps"] = https_port


def _config_https_port(config: LumaConfig) -> int:
    ports = config.defaults.get("ports") or {}
    if isinstance(ports, dict):
        return int(ports.get("traefikHttps") or ports.get("https") or 443)
    return 443


def _control_url(domain: str, https_port: int) -> str:
    port = "" if https_port == 443 else f":{https_port}"
    return f"https://{domain}{port}"


def _node_join_examples(control_url: str, join_token: str) -> list[tuple[str, str]]:
    base = f"luma node join {control_url} --token {join_token}"
    return [
        ("cn worker", f"{base} --region cn --name cn-worker-1"),
        ("global worker", f"{base} --region global --name global-sg-1"),
        ("home node", f"{base} --region home --name home-mac-mini"),
    ]


def _control_state_for_bootstrap(domain: str, *, overwrite: bool) -> Dict[str, object]:
    if overwrite:
        return new_state(domain=domain)
    data = _existing_control_state()
    if data:
        data["domain"] = domain
        return data
    return new_state(domain=domain)


def _attach_control_secrets(state: Dict[str, object], config: LumaConfig) -> None:
    names: set[str] = set()
    dns = config.dns
    if dns:
        names.add(str(dns.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN")))
        names.add(str(dns.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID")))
    names.update(key for key in os.environ if key in {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID"})

    existing = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    secrets = dict(existing or {})
    for name in names:
        value = os.environ.get(name)
        if value:
            secrets[name] = value
    if secrets:
        state["secrets"] = secrets


def _ensure_cloudflare_dns_from_local_config(config: LumaConfig, domain: str, node=None) -> None:
    dns = config.dns
    if dns.get("provider"):
        _ensure_dns_edge_target(config, node)
        return
    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        _ensure_dns_edge_target(config, node)
        return
    dns_config = _writable_dns_config(config)
    for zone_name in _zone_candidates(domain):
        try:
            zone = find_zone(LumaConfig({"providers": {"dns": {"type": "cloudflare"}}}, None), zone_name)
        except LumaError:
            continue
        dns_config["type"] = "cloudflare"
        dns_config["zone"] = zone_name
        dns_config["zoneId"] = zone["id"]
        dns_config.setdefault("apiTokenEnv", "CLOUDFLARE_API_TOKEN")
        _ensure_dns_edge_target(config, node, dns_config=dns_config)
        if config.path:
            save_config(config)
        return
    raise LumaError(
        "CLOUDFLARE_API_TOKEN is configured, but Cloudflare zone could not be inferred from "
        f"{domain!r}. Run: luma cloudflare connect --zone <zone>, then rerun bootstrap/update manager."
    )


def _writable_dns_config(config: LumaConfig) -> Dict[str, object]:
    providers = config.raw.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise LumaError("providers must be a mapping to configure Cloudflare DNS")
    current = providers.get("dns")
    if current is None and isinstance(config.raw.get("dns"), dict):
        current = dict(config.raw["dns"])
        providers["dns"] = current
    if current is None:
        current = {}
        providers["dns"] = current
    if not isinstance(current, dict):
        raise LumaError("providers.dns must be a mapping to configure Cloudflare DNS")
    return current


def _ensure_dns_edge_target(config: LumaConfig, node=None, *, dns_config: Dict[str, object] | None = None) -> None:
    target = _dns_target_for_bootstrap(config, node)
    if not target:
        return
    dns_config = dns_config or _writable_dns_config(config)
    if dns_config.get("edgeTarget"):
        return
    dns_config["edgeTarget"] = target
    if config.path:
        save_config(config)


def _dns_target_for_bootstrap(config: LumaConfig, node=None) -> str:
    env_target = os.environ.get("LUMA_DNS_EDGE_TARGET", "").strip()
    if env_target:
        return env_target
    config_target = config.default_dns_target()
    if config_target:
        return str(config_target)
    node_target = getattr(node, "public_ip", None) if node is not None else None
    if not node_target and node is not None:
        raw = getattr(node, "raw", {}) or {}
        node_target = raw.get("edgeTarget") or raw.get("publicIp") or raw.get("public_ip")
    if not node_target:
        return ""
    target = str(node_target).strip()
    if target in {"localhost", "127.0.0.1", "::1"}:
        return ""
    return target


def _zone_candidates(domain: str) -> list[str]:
    labels = [part for part in domain.strip(".").split(".") if part]
    candidates: list[str] = []
    for index in range(0, max(0, len(labels) - 1)):
        candidate = ".".join(labels[index:])
        if "." in candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _local_node(profile_name: str, *, name: str | None = None, region: str | None = None):
    from ..config import NodeConfig

    profile = PROFILES[profile_name]
    return NodeConfig(
        name=name or os.uname().nodename,
        host="localhost",
        region=region or profile.labels.get("region", "cn"),
        roles=list(profile.roles),
        raw={},
    )


def cmd_cloudflare(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.cloudflare_command == "connect":
        zone = find_zone(config, args.zone)
        providers = config.raw.setdefault("providers", {})
        dns = providers.setdefault("dns", {})
        dns["type"] = "cloudflare"
        dns["zone"] = args.zone
        dns["zoneId"] = zone["id"]
        dns.setdefault("apiTokenEnv", "CLOUDFLARE_API_TOKEN")
        save_config(config)
        print(f"Cloudflare connected: {args.zone} ({zone['id']})")
        return 0
    raise LumaError(f"unknown cloudflare command: {args.cloudflare_command}")


def cmd_manager(args: argparse.Namespace) -> int:
    if args.manager_command != "ip-change":
        raise LumaError(f"unknown manager command: {args.manager_command}")
    state = _existing_control_state()
    if not state:
        raise LumaError(
            "manager control state not found; run this command on the manager host "
            "after the original Luma bootstrap"
        )
    config_path = args.config
    if config_path is None:
        config_path = Path("/opt/luma/luma.yaml")
    manager_ip_change(
        old_ip=args.old_ip,
        new_ip=args.new_ip,
        domain=args.domain,
        state=state,
        config_path=config_path,
        dry_run=bool(args.dry_run),
        emit=log,
    )
    return 0


def cmd_egress(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ensure_interactive_config("manager", keys=["EGRESS_SUBSCRIPTION_URL"])
    node = _local_node("egress-gateway")
    subscription_url = os.environ.get("EGRESS_SUBSCRIPTION_URL")
    if not subscription_url:
        raise LumaError("missing EGRESS_SUBSCRIPTION_URL")
    setup_egress(config, node, subscription_url, emit=log, executor=LocalExecutor())
    if args.egress_command == "refresh":
        print("Egress refreshed")
    else:
        print("Egress setup complete")
    return 0
