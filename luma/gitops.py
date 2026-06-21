from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .errors import LumaError


def _redact(text: str) -> str:
    return re.sub(r"(https://)[^@/\s]+@", r"\1***@", text or "")


def clone(
    url: str,
    dest: Path,
    *,
    ref: str | None = None,
    proxy: str | None = None,
    token: str | None = None,
) -> str:
    safe_url = str(url or "").strip()
    if not safe_url:
        raise LumaError("git clone requires a repository url")
    if not safe_url.startswith(("https://", "git@", "ssh://")):
        # accept bare github.com/owner/repo shorthand
        safe_url = f"https://{safe_url.lstrip('/')}"
    clone_url = safe_url
    if token and clone_url.startswith("https://"):
        clone_url = clone_url.replace("https://", f"https://x-access-token:{token}@", 1)

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
        )
    except FileNotFoundError as exc:
        raise LumaError("git is not installed") from exc
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
        )
    except FileNotFoundError as exc:
        raise LumaError("git is not installed") from exc
    if result.returncode != 0:
        raise LumaError(f"git rev-parse failed:\n{result.stdout.strip()}")
    return result.stdout.strip()


def commit(paths: list[Path], message: str) -> str:
    try:
        subprocess.run(["git", "add", *[str(path) for path in paths]], check=True)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise LumaError("git is not installed") from exc
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
        )
    except FileNotFoundError as exc:
        raise LumaError("git is not installed") from exc
    if result.returncode != 0:
        raise LumaError(f"git push failed:\n{result.stdout.strip()}")
    return "Git push complete"
