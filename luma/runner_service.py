"""One persistent build-runner service per machine (systemd or launchd).

The service uses the invoking user's Docker environment and the current Luma
installation. Credentials live in a private config, never in the service argv.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from .errors import LumaError
from .installation import runtime_record

CONFIG = Path("/opt/luma/runner/config.json")
UNIT = "luma-build-runner.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT
LABEL = "io.luma.build-runner"
PLIST = Path("/Library/LaunchDaemons") / f"{LABEL}.plist"
LOG = Path("/opt/luma/runner/logs/runner.log")
ENV_KEYS = (
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "LUMA_RUNNER_DOCKER_MIRRORS",
)


def platform_backend() -> str:
    if sys.platform == "linux" and shutil.which("systemctl"):
        return "systemd"
    if sys.platform == "darwin" and shutil.which("launchctl"):
        return "launchd"
    raise LumaError("runner services require Linux with systemd or macOS with launchd; use 'luma runner run' in other environments")


def _command(argv: list[str], *, privileged: bool = False, check: bool = True,
             stream: bool = False) -> subprocess.CompletedProcess:
    operation = Path(argv[0]).name
    if len(argv) > 1 and argv[1] in {"start", "restart", "stop", "enable", "disable", "daemon-reload", "bootstrap", "bootout", "kickstart"}:
        operation += " " + argv[1]
    if privileged and os.geteuid() != 0:
        if not shutil.which("sudo"):
            raise LumaError("Installing or controlling the runner service requires sudo")
        # Authenticate on the real terminal, not through captured subprocess IO.
        auth = subprocess.run(["sudo", "-v"], check=False)
        if auth.returncode:
            raise LumaError("sudo authentication failed; runner service was not changed")
        argv = ["sudo", "-n", *argv]
    try:
        result = subprocess.run(argv, text=True, capture_output=not stream,
                                timeout=None if stream else 90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LumaError(f"Runner service command failed: {operation}: {_diagnostic(str(exc))}") from exc
    if check and result.returncode:
        detail = _diagnostic(result.stderr or "")
        message = f"Runner service command failed ({result.returncode}): {operation}"
        if detail:
            message += "\n" + detail
        raise LumaError(message + "\nInspect 'luma runner status' and 'luma runner logs'.")
    return result


def _diagnostic(text: str) -> str:
    # Hand-written units can contain credentials in commands or environment.
    # Retain parser/permission errors while withholding those definitions.
    lines = ["<service command/environment redacted>" if re.search(
        r"\b(?:ExecStart|ExecStop|Environment|EnvironmentVariables|ProgramArguments)\b", line,
    ) else line for line in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"lbr_[A-Za-z0-9_-]+", "<redacted>", text)
    text = re.sub(r"(?i)(--(?:runner-)?token(?:=|\s+)|Bearer\s+)\S+", r"\1<redacted>", text)
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1<redacted>@", text)
    return text.strip()[:2000]


def load_config(*, required: bool = False) -> dict:
    try:
        data = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        if required:
            raise LumaError("Runner is not configured; run 'luma runner join --url URL --token TOKEN'") from None
        return {}
    except PermissionError:
        raise LumaError("Runner belongs to another user; manage it as that user or with sudo") from None
    except (OSError, ValueError):
        raise LumaError(f"Cannot read runner configuration: {CONFIG}") from None
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise LumaError(f"Invalid runner configuration: {CONFIG}")
    return data


def validate_options(options: dict) -> dict:
    options = dict(options)
    url = str(options.get("server_url") or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LumaError("Runner requires a valid --url (or LUMA_BUILD_URL), without credentials, query or fragment")
    token = str(options.get("runner_token") or "").strip()
    if not token.startswith("lbr_") or any(c.isspace() for c in token):
        raise LumaError("Runner requires an lbr_ token; pass --token or set LUMA_RUNNER_TOKEN")
    for key, minimum, maximum in (("workers", 1, 16), ("cache_keep_gb", 1, None), ("max_parallelism", 0, None)):
        try:
            number = int(options[key])
        except (ValueError, TypeError, KeyError):
            raise LumaError(f"Invalid runner option: {key.replace('_', '-')}") from None
        if number < minimum or (maximum is not None and number > maximum):
            raise LumaError(f"Invalid runner option: {key.replace('_', '-')}")
        options[key] = number
    options.update(server_url=url, runner_token=token,
                   state_dir=str(Path(options["state_dir"]).expanduser().absolute()))
    return options


def _identity() -> dict:
    user = pwd.getpwuid(os.getuid())
    return {"name": user.pw_name, "uid": user.pw_uid, "gid": user.pw_gid, "home": user.pw_dir}


def _executable() -> tuple[list[str], dict[str, str]]:
    try:
        record = runtime_record()
    except (ValueError, OSError) as exc:
        raise LumaError(f"Invalid Luma installation identity: {exc}") from exc
    if record:
        shim = Path(record["binDir"]) / "luma"
        if shim.is_file() and os.access(shim, os.X_OK):
            return [str(shim)], {}
    # A pip install or an explicit development checkout: use this interpreter,
    # never another user's PATH-resolved luma or a resolved system Python link.
    return [sys.executable, "-m", "luma.cli"], {"PYTHONPATH": str(Path(__file__).resolve().parent.parent)}


def _environment(owner: dict) -> dict[str, str]:
    home = owner["home"]
    paths = [str(Path(home) / ".local/bin"), str(Path(home) / ".orbstack/bin"),
             "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    paths = list(dict.fromkeys([*os.environ.get("PATH", "").split(os.pathsep), *paths]))
    env = {key: os.environ[key] for key in ENV_KEYS if os.environ.get(key)}
    env.update(HOME=home, USER=owner["name"], LOGNAME=owner["name"],
               PATH=os.pathsep.join(p for p in paths if p), PYTHONUNBUFFERED="1")
    return env


def _systemd_quote(value: str, *, command: bool = False) -> str:
    # systemd has its own quoting and expansion rules, not shell quoting.
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n').replace('\r', '\\r')
    return '"' + (escaped.replace('$', '$$') if command else escaped) + '"'


def _systemd_directory(value: str) -> str:
    # WorkingDirectory is a path directive, not a shell/argv string. systemd
    # treats surrounding quotes as literal path characters and rejects them.
    value = str(value)
    if not value.startswith("/") or value != value.strip() or any(c in value for c in "\r\n\x00"):
        raise LumaError("Runner home must be an absolute directory without line breaks or surrounding whitespace")
    return value.replace("%", "%%")


def service_definition(config: dict, backend: str) -> bytes:
    owner = config["owner"]
    argv = [*config["command"], "--no-env", "runner", "run", "--service-config", str(CONFIG)]
    startup_env = {key: value for key, value in config["environment"].items()
                   if key in {"HOME", "PATH", "PYTHONPATH", "PYTHONUNBUFFERED"}}
    if backend == "launchd":
        return plistlib.dumps({
            "Label": LABEL, "ProgramArguments": argv, "UserName": owner["name"],
            "WorkingDirectory": owner["home"], "EnvironmentVariables": startup_env,
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 5,
            "ExitTimeOut": 60, "StandardOutPath": str(LOG), "StandardErrorPath": str(LOG),
        })
    lines = ["[Unit]", "Description=Luma Build Runner", "Wants=network-online.target",
             "After=network-online.target docker.service", "StartLimitIntervalSec=0", "", "[Service]",
             "Type=simple", f"User={owner['name']}",
             f"WorkingDirectory={_systemd_directory(owner['home'])}",
             *(f"Environment={_systemd_quote(key + '=' + value)}" for key, value in startup_env.items()),
             "ExecStart=" + " ".join(_systemd_quote(arg, command=True) for arg in argv),
             "Restart=on-failure", "RestartSec=5", "TimeoutStopSec=60", "KillMode=control-group",
             "", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines).encode()


def _install_file(path: Path, content: bytes, *, mode: str, owner: dict | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="luma-runner-install-") as tmp:
        source = Path(tmp) / "payload"
        source.touch(mode=0o600)
        source.write_bytes(content)
        _command(["install", "-d", "-m", "755", str(path.parent)], privileged=True)
        # Rename in the destination directory, so readers always see a complete
        # config. The destination remains private even during token rotation.
        stage = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        args = ["install", "-m", mode]
        if owner:
            args += ["-o", str(owner["uid"]), "-g", str(owner["gid"])]
        _command([*args, str(source), str(stage)], privileged=True)
        _command(["mv", "-f", str(stage), str(path)], privileged=True)


def status() -> dict:
    backend = platform_backend()
    result = {"backend": backend, "service": UNIT if backend == "systemd" else LABEL,
              "installed": (UNIT_PATH if backend == "systemd" else PLIST).is_file(),
              "running": False, "state": "not-installed", "pid": 0}
    if not result["installed"]:
        return result
    if backend == "systemd":
        proc = _command(["systemctl", "show", UNIT, "--property=ActiveState,SubState,MainPID,UnitFileState"], check=False)
        if proc.returncode:
            raise LumaError("Cannot read systemd runner status")
        props = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        result.update(state=props.get("ActiveState", "unknown"),
                      running=props.get("ActiveState") == "active" and props.get("SubState") == "running",
                      pid=int(props.get("MainPID") or 0), enabled=props.get("UnitFileState") == "enabled")
    else:
        proc = _command(["launchctl", "print", f"system/{LABEL}"], check=False)
        state = re.search(r"^\s*state = (\S+)", proc.stdout, re.M)
        pid = re.search(r"^\s*pid = (\d+)", proc.stdout, re.M)
        result.update(state=state.group(1) if state else "stopped", running=bool(pid) and bool(state and state.group(1) == "running"),
                      pid=int(pid.group(1)) if pid else 0)
    return result


def start(options: dict, *, restart: bool = False, configure: bool = False) -> dict:
    backend = platform_backend()
    previous = load_config()
    owner = previous.get("owner") or _identity()
    if os.geteuid() not in (0, owner["uid"]):
        raise LumaError("Runner belongs to another user; manage it as that user or with sudo")
    if previous and (not configure or os.geteuid() != owner["uid"]):
        command, extra_env = previous["command"], {}
    else:
        command, extra_env = _executable()
    environment = previous.get("environment") or _environment(owner)
    if configure and os.geteuid() == owner["uid"]:
        environment = _environment(owner)
    config = {"schemaVersion": 1, "owner": owner, "options": validate_options(options),
              "command": command, "environment": {**environment, **extra_env}}
    changed = config != previous
    definition = service_definition(config, backend)
    path = UNIT_PATH if backend == "systemd" else PLIST
    before = status()
    definition_changed = not path.is_file() or path.read_bytes() != definition
    changed = changed or definition_changed
    if changed or not before["installed"]:
        _install_file(CONFIG, (json.dumps(config, indent=2) + "\n").encode(), mode="600", owner=owner)
        _install_file(path, definition, mode="644")
    if backend == "systemd":
        if changed or not before["installed"]:
            _command(["systemctl", "daemon-reload"], privileged=True)
        _command(["systemctl", "enable", UNIT], privileged=True)
        action = "restart" if before["running"] and (changed or restart) else "start"
        _command(["systemctl", action, UNIT], privileged=True)
    else:
        _command(["install", "-d", "-o", str(owner["uid"]), "-g", str(owner["gid"]),
                  "-m", "700", str(LOG.parent)], privileged=True)
        if not LOG.exists():
            _command(["install", "-m", "600", "-o", str(owner["uid"]), "-g", str(owner["gid"]),
                      "/dev/null", str(LOG)], privileged=True)
        _command(["launchctl", "enable", f"system/{LABEL}"], privileged=True)
        loaded = _command(["launchctl", "print", f"system/{LABEL}"], check=False).returncode == 0
        if loaded and (changed or restart):
            _command(["launchctl", "bootout", f"system/{LABEL}"], privileged=True)
            loaded = False
        if not loaded:
            _command(["launchctl", "bootstrap", "system", str(PLIST)], privileged=True)
        _command(["launchctl", "kickstart", f"system/{LABEL}"], privileged=True)
    for _ in range(20):
        current = status()
        if current["running"]:
            return current
        time.sleep(0.25)
    raise LumaError("Runner service did not start; run 'luma runner logs' for the startup error")


def stop() -> dict:
    before = status()
    if not before["installed"]:
        return before
    if before["backend"] == "systemd":
        _command(["systemctl", "disable", "--now", UNIT], privileged=True)
    else:
        _command(["launchctl", "disable", f"system/{LABEL}"], privileged=True)
        if _command(["launchctl", "print", f"system/{LABEL}"], check=False).returncode == 0:
            _command(["launchctl", "bootout", f"system/{LABEL}"], privileged=True)
    return status()


def logs(*, follow: bool = False, lines: int = 100) -> int:
    backend = platform_backend()
    if not 1 <= lines <= 10000:
        raise LumaError("--lines must be between 1 and 10000")
    if backend == "systemd":
        argv = ["journalctl", "-u", UNIT, "-n", str(lines), "--no-pager"] + (["-f"] if follow else [])
    else:
        if _command(["test", "-f", str(LOG)], privileged=True, check=False).returncode:
            raise LumaError("Runner log has not been created yet; check 'luma runner status' and the service installation error")
        argv = ["tail", "-n", str(lines)] + (["-F"] if follow else []) + [str(LOG)]
    return _command(argv, privileged=True, stream=True).returncode
