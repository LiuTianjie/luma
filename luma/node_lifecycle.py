"""Independent node-agent cutover: switch, verify, and roll the shim back.

The managed installer prepares a candidate and publishes the command shim. This
module owns the service restart from a process that is not the running agent.
A start/heartbeat that still belongs to the previous runtime is not success.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any, Callable

from .errors import LumaError
from .installation import (
    lifecycle_dir,
    read_operation,
    restore_shim,
    write_json,
    write_operation,
)

SCHEMA = "luma.lifecycle-update/v1"
DEFAULT_AGENT_CONFIG = Path("/opt/luma/node-agent/agent.json")
LINUX_UNIT = "luma-node-agent.service"
DARWIN_LABEL = "io.luma.node-agent"
SWITCH_TIMEOUT_SECONDS = 90
STABLE_SECONDS = 8


def new_operation_id() -> str:
    return f"update-{int(time.time())}-{secrets.token_hex(4)}"


def start_cutover(
    *,
    install_home: Path,
    bin_dir: Path,
    target_runtime: Path,
    target_version: str,
    config_path: Path = DEFAULT_AGENT_CONFIG,
    previous_runtime: str = "",
    spawner: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Record a cutover and start an independent supervisor. Does not restart here."""
    install_home = Path(install_home)
    bin_dir = Path(bin_dir)
    target_runtime = Path(target_runtime)
    python = target_runtime / "bin" / "python"
    if not python.is_file():
        python = target_runtime / "bin" / "python3"
    if not python.is_file():
        raise LumaError(f"target runtime has no python: {target_runtime}")
    if not (bin_dir / "luma").is_file():
        raise LumaError("updated Luma command shim is missing; refusing to restart the node agent")
    nonce = secrets.token_hex(16)
    record = {
        "schemaVersion": SCHEMA,
        "id": new_operation_id(),
        "phase": "switching",
        "installHome": str(install_home),
        "binDir": str(bin_dir),
        "configPath": str(config_path),
        "targetRuntime": str(target_runtime),
        "targetVersion": str(target_version),
        "previousRuntime": str(previous_runtime or ""),
        "targetPython": str(python),
        "nonce": nonce,
        "createdAt": int(time.time()),
        "updatedAt": int(time.time()),
    }
    write_operation(install_home, record)
    write_json(lifecycle_dir(install_home) / "expected.json", {
        "operationId": record["id"],
        "nonce": nonce,
        "targetRuntime": str(target_runtime),
        "targetVersion": str(target_version),
    })
    try:
        (spawner or spawn_supervisor)(record)
    except Exception:
        record["phase"] = "rollback_failed"
        record["error"] = "supervisor failed to start"
        record["updatedAt"] = int(time.time())
        write_operation(install_home, record)
        try:
            restore_shim(install_home, bin_dir)
        except (ValueError, OSError):
            pass
        raise
    return record


def spawn_supervisor(record: dict[str, Any]) -> None:
    python = record["targetPython"]
    operation_id = record["id"]
    install_home = record["installHome"]
    args = [
        python, "-m", "luma.node_lifecycle", "switch",
        "--operation-id", operation_id,
        "--install-home", install_home,
    ]
    if sys.platform == "darwin":
        _spawn_launchd(record, args)
        return
    if not shutil.which("systemd-run"):
        raise LumaError("managed node-agent cutover requires systemd-run or macOS launchd")
    unit = f"luma-lifecycle-{operation_id}"
    command = [
        "systemd-run",
        f"--unit={unit}",
        "--collect",
        "--no-block",
        "--property=Type=oneshot",
        "--property=TimeoutStartSec=180",
        *args,
    ]
    completed = _run_privileged(command)
    if completed.returncode != 0:
        raise LumaError(f"unable to start lifecycle supervisor: {(completed.stdout or '')[-800:]}")


def _spawn_launchd(record: dict[str, Any], args: list[str]) -> None:
    label = f"io.luma.lifecycle-{record['id']}"
    plist = Path("/Library/LaunchDaemons") / f"{label}.plist"
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        "<plist version=\"1.0\"><dict>\n"
        f"  <key>Label</key><string>{label}</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        + "".join(f"    <string>{_xml(arg)}</string>\n" for arg in args)
        + "  </array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><false/>\n"
        f"  <key>StandardOutPath</key><string>{_xml(str(lifecycle_dir(Path(record['installHome'])) / (record['id'] + '.log')))}</string>\n"
        f"  <key>StandardErrorPath</key><string>{_xml(str(lifecycle_dir(Path(record['installHome'])) / (record['id'] + '.err')))}</string>\n"
        "</dict></plist>\n"
    )
    completed = _run_privileged([
        "sh", "-c",
        "printf '%s' \"$1\" > \"$2\" && chmod 644 \"$2\" && "
        "launchctl bootout system/\"$3\" >/dev/null 2>&1 || true && "
        "launchctl bootstrap system \"$2\" && launchctl kickstart -k system/\"$3\"",
        "luma-lifecycle",
        body,
        str(plist),
        label,
    ])
    if completed.returncode != 0:
        raise LumaError(f"unable to start lifecycle supervisor: {(completed.stdout or '')[-800:]}")


def _xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _run_privileged(command: list[str]) -> subprocess.CompletedProcess[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        invocation = command
    else:
        invocation = ["sudo", "-n", *command]
    return subprocess.run(
        invocation, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def switch_operation(
    record: dict[str, Any],
    *,
    restarter: Callable[[dict[str, Any]], None] | None = None,
    pid_reader: Callable[[], int | None] | None = None,
    runtime_reader: Callable[[int], str | None] | None = None,
    observed_reader: Callable[[Path], dict[str, Any] | None] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    timeout: float = SWITCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    install_home = Path(record["installHome"])
    bin_dir = Path(record["binDir"])
    record["phase"] = "verifying"
    record["updatedAt"] = int(time.time())
    write_operation(install_home, record)
    try:
        wait_for_target(
            record,
            pid_reader=pid_reader,
            runtime_reader=runtime_reader,
            observed_reader=observed_reader,
            sleeper=sleeper,
            clock=clock,
            timeout=timeout,
        )
        record["phase"] = "succeeded"
        record["updatedAt"] = int(time.time())
        write_operation(install_home, record)
        return record
    except Exception as exc:
        record["error"] = str(exc)[:800]
        record["phase"] = "rolling_back"
        record["updatedAt"] = int(time.time())
        write_operation(install_home, record)
        try:
            restore_shim(install_home, bin_dir)
            (restarter or restart_agent)(record)
            record["phase"] = "rolled_back"
        except Exception as rollback_exc:
            record["phase"] = "rollback_failed"
            record["rollbackError"] = str(rollback_exc)[:800]
        record["updatedAt"] = int(time.time())
        write_operation(install_home, record)
        if record["phase"] != "rolled_back":
            raise LumaError(
                f"node-agent cutover failed and rollback did not complete: {record.get('rollbackError') or record.get('error')}"
            ) from exc
        raise LumaError(
            f"node-agent cutover failed; previous command shim restored: {record.get('error')}"
        ) from exc


def restart_agent(record: dict[str, Any]) -> None:
    if sys.platform == "darwin":
        label = DARWIN_LABEL
        plist = "/Library/LaunchDaemons/io.luma.node-agent.plist"
        command = (
            f"launchctl bootout system/{label} >/dev/null 2>&1 || true; "
            f"launchctl bootstrap system {plist}; "
            f"launchctl kickstart -k system/{label}"
        )
        completed = _run_privileged(["sh", "-c", command])
    else:
        completed = _run_privileged([
            "sh", "-c",
            "systemctl daemon-reload && "
            f"systemctl reset-failed {LINUX_UNIT} >/dev/null 2>&1 || true && "
            f"systemctl restart {LINUX_UNIT}",
        ])
    if completed.returncode != 0:
        raise LumaError(f"node agent restart failed: {(completed.stdout or '')[-800:]}")


def agent_pid() -> int | None:
    if sys.platform == "darwin":
        completed = subprocess.run(
            ["launchctl", "print", f"system/{DARWIN_LABEL}"],
            check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        for line in (completed.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("pid = "):
                try:
                    pid = int(stripped.split("=", 1)[1].strip())
                except ValueError:
                    return None
                return pid if pid > 0 else None
        return None
    completed = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", LINUX_UNIT],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        pid = int((completed.stdout or "0").strip() or "0")
    except ValueError:
        return None
    return pid if pid > 0 else None


def process_runtime(pid: int) -> str | None:
    proc = Path(f"/proc/{pid}/exe")
    try:
        if proc.exists() or proc.is_symlink():
            target = os.readlink(proc)
            target = target.split(" (deleted)", 1)[0]
            path = Path(target).resolve()
            if path.parent.name == "bin":
                return str(path.parent.parent)
            return str(path)
    except OSError:
        pass
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "args="],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    for token in (completed.stdout or "").split():
        if "python" in Path(token).name.lower() and "/" in token:
            path = Path(token).resolve()
            if path.parent.name == "bin":
                return str(path.parent.parent)
            return str(path)
    return None


def wait_for_target(
    record: dict[str, Any],
    *,
    pid_reader: Callable[[], int | None] | None = None,
    runtime_reader: Callable[[int], str | None] | None = None,
    observed_reader: Callable[[Path], dict[str, Any] | None] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    timeout: float = SWITCH_TIMEOUT_SECONDS,
) -> None:
    install_home = Path(record["installHome"])
    target_runtime = str(Path(record["targetRuntime"]).resolve())
    nonce = str(record.get("nonce") or "")
    deadline = (clock or time.monotonic)() + timeout
    stable_since: float | None = None
    matched_pid: int | None = None
    while (clock or time.monotonic)() < deadline:
        observed = (observed_reader or read_observed)(install_home)
        pid = (pid_reader or agent_pid)()
        raw_runtime = (runtime_reader or process_runtime)(pid) if pid else ""
        runtime = str(Path(raw_runtime).resolve()) if raw_runtime else ""
        observed_raw = str((observed or {}).get("runtime") or "") if observed else ""
        observed_runtime = str(Path(observed_raw).resolve()) if observed_raw else ""
        nonce_ok = bool(
            observed
            and observed.get("nonce") == nonce
            and observed.get("operationId") == record["id"]
            and observed_runtime
            and (observed_runtime == target_runtime or observed_runtime.startswith(target_runtime + os.sep))
        )
        runtime_ok = bool(pid and runtime and (runtime == target_runtime or runtime.startswith(target_runtime + os.sep)))
        if (nonce_ok or runtime_ok) and pid:
            now = (clock or time.monotonic)()
            if matched_pid == pid and stable_since is not None:
                if now - stable_since >= STABLE_SECONDS:
                    return
            else:
                matched_pid = pid
                stable_since = now
        else:
            stable_since = None
            matched_pid = None
        (sleeper or time.sleep)(0.4)
    raise LumaError(
        "updated node agent did not prove it is running from the target runtime; previous heartbeat is not success"
    )


def read_observed(install_home: Path) -> dict[str, Any] | None:
    path = lifecycle_dir(install_home) / "observed.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def note_agent_started(install_home: Path | None = None) -> None:
    """Called by the new agent process. Never raises into the poll loop."""
    try:
        from .installation import runtime_record
        record = runtime_record()
        home = Path(install_home or (record or {}).get("installHome") or "")
        if not home:
            return
        expected_path = lifecycle_dir(home) / "expected.json"
        if not expected_path.is_file():
            return
        expected = json.loads(expected_path.read_text())
        if not expected.get("nonce"):
            return
        write_json(lifecycle_dir(home) / "observed.json", {
            "operationId": expected.get("operationId", ""),
            "nonce": expected.get("nonce", ""),
            "runtime": str(Path(sys.prefix).resolve()),
            "version": str((record or {}).get("version") or ""),
            "pid": os.getpid(),
            "ts": int(time.time()),
        })
    except Exception:
        return


def cmd_switch(operation_id: str, install_home: str) -> int:
    record = read_operation(Path(install_home), operation_id)
    if not record:
        raise LumaError(f"lifecycle operation not found: {operation_id}")
    switch_operation(record)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    switch = sub.add_parser("switch")
    switch.add_argument("--operation-id", required=True)
    switch.add_argument("--install-home", required=True)
    args = parser.parse_args(argv)
    if args.command == "switch":
        return cmd_switch(args.operation_id, args.install_home)
    raise LumaError(f"unknown lifecycle command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LumaError, ValueError, OSError) as exc:
        print(f"Luma lifecycle failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
