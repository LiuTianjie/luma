from __future__ import annotations

import os
import re
import subprocess
import urllib.parse
from pathlib import Path

from .errors import LumaError


# Bound every git subprocess so a network/proxy stall (clone, push) or a wedged
# local git can't hang the CLI indefinitely. Network ops get a longer budget
# than local ones.
GIT_NETWORK_TIMEOUT = 300
GIT_LOCAL_TIMEOUT = 60


def _redact(text: str) -> str:
    return re.sub(r"(https://)[^@/\s]+@", r"\1***@", text or "")


def clone(
    url: str,
    dest: Path,
    *,
    ref: str | None = None,
    proxy: str | None = None,
    token: str | None = None,
    username: str | None = None,
) -> str:
    safe_url = str(url or "").strip()
    if not safe_url:
        raise LumaError("git clone requires a repository url")
    if not safe_url.startswith(("https://", "git@", "ssh://")):
        # accept bare github.com/owner/repo shorthand
        safe_url = f"https://{safe_url.lstrip('/')}"
    clone_url = safe_url
    if token and clone_url.startswith("https://"):
        auth_username = urllib.parse.quote(str(username or "x-access-token"), safe="")
        auth_token = urllib.parse.quote(str(token), safe="")
        clone_url = clone_url.replace("https://", f"https://{auth_username}:{auth_token}@", 1)

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy

    command = ["git", "clone", "--depth", "1"]
    if ref:
        command += ["--branch", str(ref)]
    command += [clone_url, str(dest)]
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=GIT_NETWORK_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise LumaError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise LumaError(
            f"git clone timed out after {GIT_NETWORK_TIMEOUT}s "
            f"(network or proxy stall): {_redact(safe_url)}"
        ) from exc
    if result.returncode != 0:
        raise LumaError(f"git clone failed:\n{_redact(result.stdout.strip())}")
    return f"Cloned {_redact(safe_url)}"


def head_commit(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=GIT_LOCAL_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise LumaError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise LumaError(f"git rev-parse timed out after {GIT_LOCAL_TIMEOUT}s") from exc
    if result.returncode != 0:
        raise LumaError(f"git rev-parse failed:\n{result.stdout.strip()}")
    return result.stdout.strip()


def commit(paths: list[Path], message: str) -> str:
    try:
        add = subprocess.run(
            ["git", "add", *[str(path) for path in paths]],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=GIT_LOCAL_TIMEOUT,
        )
        if add.returncode != 0:
            raise LumaError(f"git add failed:\n{add.stdout.strip()}")
        result = subprocess.run(
            ["git", "commit", "-m", message],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=GIT_LOCAL_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise LumaError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise LumaError(f"git commit timed out after {GIT_LOCAL_TIMEOUT}s") from exc
    if result.returncode != 0:
        output = result.stdout.strip()
        if "nothing to commit" in output:
            return "Git commit skipped: nothing to commit"
        raise LumaError(f"git commit failed:\n{output}")
    return result.stdout.strip().splitlines()[-1]


def push() -> str:
    try:
        result = subprocess.run(
            ["git", "push"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=GIT_NETWORK_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise LumaError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise LumaError(
            f"git push timed out after {GIT_NETWORK_TIMEOUT}s (network or proxy stall)"
        ) from exc
    if result.returncode != 0:
        raise LumaError(f"git push failed:\n{result.stdout.strip()}")
    return "Git push complete"
