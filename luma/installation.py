"""Installation identity and dependency policy, usable before Luma dependencies exist.

This module deliberately uses only the standard library. The shell bootstrap and
an already installed CLI/agent share it; neither needs to infer root's PATH.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

SCHEMA = "luma.installation/v1"
RECORD = "luma-installation.json"


def _absolute(value: str) -> Path:
    if not isinstance(value, str):
        raise ValueError("installation path must be a string")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(c in value for c in "\r\n\0"):
        raise ValueError("installation paths must be absolute and normalized")
    return path


def dependency_policy(env: dict[str, str], previous: dict | None = None, *, validate_paths: bool = True) -> dict:
    previous = previous or {}
    index = env.get("LUMA_PIP_INDEX_URL", previous.get("indexUrl", "https://pypi.org/simple"))
    wheelhouse = env.get("LUMA_PIP_WHEELHOUSE", previous.get("wheelhouse", ""))
    ca = env.get("LUMA_PIP_CA_BUNDLE", previous.get("caBundle", ""))
    parts = urlsplit(index)
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment or any(c.isspace() for c in index):
        raise ValueError("LUMA_PIP_INDEX_URL must be HTTPS without credentials, query or fragment")
    for name, value in (("wheelhouse", wheelhouse), ("caBundle", ca)):
        if value:
            _absolute(value)
            if validate_paths and not (Path(value).is_dir() if name == "wheelhouse" else Path(value).is_file()):
                raise ValueError(f"installation {name} does not exist")
    return {"indexUrl": index, "wheelhouse": wheelhouse, "caBundle": ca}


def pip_environment(env: dict[str, str], policy: dict) -> dict[str, str]:
    # Ignore both environment and global/user/site pip configuration. A dead
    # cloud-image pip.conf must not determine the managed runtime's dependencies.
    result = {k: v for k, v in env.items() if not k.startswith(("PIP_", "PYTHON"))}
    result.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1",
                  PIP_TIMEOUT="25", PIP_RETRIES="2")
    if policy.get("wheelhouse"):
        result.update(PIP_NO_INDEX="1", PIP_FIND_LINKS=policy["wheelhouse"])
    else:
        result["PIP_INDEX_URL"] = policy["indexUrl"]
    if policy.get("caBundle"):
        # Requests' environment override otherwise takes precedence over the
        # explicit pip/session certificate setting.
        result.pop("REQUESTS_CA_BUNDLE", None)
        result.pop("CURL_CA_BUNDLE", None)
        result["PIP_CERT"] = policy["caBundle"]
    return result


def read_record(prefix: Path) -> dict | None:
    prefix = prefix.resolve()
    path = prefix / RECORD
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise ValueError("installation record must be a regular, non-group/world-writable file")
    if prefix.stat().st_mode & 0o022:
        raise ValueError("installation runtime must not be group/world-writable")
    if info.st_uid != prefix.stat().st_uid:
        raise ValueError("installation record owner differs from its runtime")
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or record.get("schemaVersion") != SCHEMA:
        raise ValueError("unsupported installation record schema")
    for key in ("installHome", "binDir", "userHome", "runtime", "source"):
        record[key] = str(_absolute(record.get(key)).resolve())
    if Path(record["runtime"]) != prefix:
        raise ValueError("installation record does not describe the running Python environment")
    root = Path(record["installHome"])
    if root not in prefix.parents and prefix != root:
        raise ValueError("installation runtime is outside its recorded root")
    if Path(record["source"]) != prefix.parent / "src":
        raise ValueError("installation source is not the runtime's paired source directory")
    if not isinstance(record.get("policy"), dict):
        raise ValueError("installation dependency policy must be an object")
    record["policy"] = dependency_policy({}, record["policy"], validate_paths=False)
    if record.get("ownerUid") != prefix.stat().st_uid:
        raise ValueError("installation owner identity differs from its runtime")
    return record


def runtime_record(prefix: Path | None = None) -> dict | None:
    """Inspect the actual venv first, never resolve its Python symlink to /usr/bin."""
    prefix = (prefix or Path(sys.prefix)).resolve()
    registered = read_record(prefix)
    if registered:
        return registered
    if not (prefix / "pyvenv.cfg").is_file():
        return None
    # Editable project environments remain development installations; don't
    # silently take them over via a node operation.
    if prefix.name == ".venv" and (prefix.parent / "pyproject.toml").exists():
        return None
    owner = pwd.getpwuid(prefix.stat().st_uid)
    if prefix.name == "venv" and prefix.parent.name == "luma" and prefix.parent.parent.name == "share" and prefix.parent.parent.parent.name == ".local":
        root = prefix.parent
        home = root.parent.parent.parent
        bindir = home / ".local/bin"
    else:
        # A legacy pip/venv install such as /opt/luma-cli remains its own root.
        root, home, bindir = prefix, Path(owner.pw_dir), prefix / "bin"
    return {
        "schemaVersion": SCHEMA, "installHome": str(root), "binDir": str(bindir),
        "userHome": str(home), "runtime": str(prefix), "source": str(root),
        "ownerUid": owner.pw_uid, "mode": "legacy-venv", "policy": {},
    }


def installer_environment(env: dict[str, str], record: dict | None) -> dict[str, str]:
    result = dict(env)
    if record:
        # A registered/running identity wins over ambient root HOME or stale
        # install overrides. Explicit relocation is not an ordinary update.
        for key, field in (("LUMA_USER_HOME", "userHome"), ("LUMA_INSTALL_HOME", "installHome"), ("LUMA_BIN_DIR", "binDir")):
            if result.get(key) and Path(result[key]).resolve() != Path(record[field]).resolve():
                raise ValueError(f"{key} conflicts with the running installation; relocation requires explicit adoption")
            result[key] = record[field]
        result["LUMA_PREVIOUS_RUNTIME"] = record["runtime"]
    policy = dependency_policy(result, (record or {}).get("policy"))
    result.update(LUMA_PIP_INDEX_URL=policy["indexUrl"], LUMA_PIP_WHEELHOUSE=policy["wheelhouse"], LUMA_PIP_CA_BUNDLE=policy["caBundle"])
    return result


def write_record(prefix: Path, *, root: Path, bindir: Path, home: Path, source: Path,
                 policy: dict, version: str, previous_runtime: str = "") -> dict:
    for path in (prefix, root, bindir, home, source):
        _absolute(str(path))
    # Resolve directories, never the venv's Python executable symlink. macOS
    # /tmp and /var aliases otherwise disagree with Python's canonical prefix.
    prefix, root, bindir, home, source = (p.resolve() for p in (prefix, root, bindir, home, source))
    record = {
        "schemaVersion": SCHEMA, "installationId": hashlib.sha256(str(root).encode()).hexdigest()[:24],
        "installHome": str(root), "binDir": str(bindir), "userHome": str(home),
        "runtime": str(prefix), "source": str(source), "ownerUid": prefix.stat().st_uid,
        "version": version, "mode": "managed", "policy": policy,
        "previousRuntime": previous_runtime,
    }
    fd, temporary = tempfile.mkstemp(prefix=".installation-", dir=prefix)
    try:
        with os.fdopen(fd, "w") as file:
            if os.geteuid() == 0:
                owner = prefix.stat()
                os.fchown(file.fileno(), owner.st_uid, owner.st_gid)
            json.dump(record, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, prefix / RECORD)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return record


def installation_diagnostics(env: dict[str, str] | None = None) -> dict:
    """No network, no secrets and no state writes. Service health is out of scope."""
    env = dict(os.environ) if env is None else env
    checks = []
    record = None
    policy = None
    try:
        record = runtime_record()
        policy_env = installer_environment(env, record)
        policy = dependency_policy(policy_env)
        checks.append({"name": "installation identity and dependency policy", "ok": True})
    except (ValueError, OSError) as exc:
        checks.append({"name": "installation identity and dependency policy", "ok": False,
                       "detail": str(exc)})
    # -I prevents an ambient PYTHONPATH from making a broken venv look healthy.
    try:
        result = subprocess.run([sys.executable, "-I", "-c",
            "import importlib, json, sys\n"
            "missing=[]\n"
            "for name in ('yaml', 'starlette', 'uvicorn', 'websockets', 'python_socks'):\n"
            " try: importlib.import_module(name)\n"
            " except Exception: missing.append(name)\n"
            "print(json.dumps(missing))\n"
            "sys.exit(bool(missing))"],
            env=pip_environment(env, policy or dependency_policy({})),
            capture_output=True, text=True, timeout=15)
        missing = []
        if result.returncode:
            try:
                reported = json.loads(result.stdout)
                missing = [name for name in reported if name in {"yaml", "starlette", "uvicorn", "websockets", "python_socks"}]
            except (ValueError, TypeError):
                pass
        checks.append({"name": "runtime dependency imports", "ok": result.returncode == 0,
                       "detail": "" if result.returncode == 0 else f"Missing or unloadable runtime modules: {', '.join(missing) or 'unknown'}. Prepare a new candidate from an approved package source."})
        result = subprocess.run([sys.executable, "-I", "-m", "pip", "check"],
            env=pip_environment(env, policy or dependency_policy({})),
            capture_output=True, text=True, timeout=15)
        checks.append({"name": "runtime dependency consistency", "ok": result.returncode == 0,
                       "detail": "" if result.returncode == 0 else "pip check failed; prepare a new candidate rather than editing the active runtime."})
    except (OSError, subprocess.TimeoutExpired):
        checks.append({"name": "runtime dependencies", "ok": False, "detail": "Local interpreter check failed or timed out."})
    return {"scope": "local-installation", "healthy": all(c["ok"] for c in checks),
            "interpreter": sys.executable, "runtime": sys.prefix,
            "installation": record, "dependencyPolicy": policy,
            "ignoredHostPipSettings": sorted(k for k in env if k.startswith("PIP_")),
            "checks": checks, "serviceHealthVerified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pip", "record", "shim"))
    args, rest = parser.parse_known_args()
    env = dict(os.environ)
    try:
        policy = dependency_policy(env)
        if args.action == "pip":
            return subprocess.call([sys.executable, "-I", "-m", "pip", *rest], env=pip_environment(env, policy))
        source = _absolute(env["SOURCE_DIR"])
        if args.action == "shim":
            # Quote paths as shell data, not interpolated shell program text.
            print("#!/usr/bin/env sh\nunset PYTHONHOME")
            print("export PYTHONNOUSERSITE=1")
            print("export PYTHONPATH=" + shlex.quote(str(source)))
            print("exec " + shlex.quote(str(Path(sys.prefix) / "bin/python")) + ' -m luma.cli "$@"')
            return 0
        version = next(line.split('"')[1] for line in (source / "luma/__init__.py").read_text().splitlines() if line.startswith('__version__ = "'))
        write_record(Path(sys.prefix), root=Path(env["INSTALL_HOME"]), bindir=Path(env["BIN_DIR"]),
                     home=Path(env["LUMA_USER_HOME"]), source=source, policy=policy,
                     version=version, previous_runtime=env.get("LUMA_PREVIOUS_RUNTIME", ""))
    except (ValueError, KeyError, OSError, StopIteration) as exc:
        print(f"Luma installation preparation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
