from __future__ import annotations

import os
import re
import subprocess
import tempfile
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


def _redact_secret(text: str, secret: str | None) -> str:
    value = str(secret or "")
    return (text or "").replace(value, "***") if value else (text or "")


def _write_git_askpass(directory: Path, *, username: str, token: str) -> tuple[Path, Path, Path]:
    """Create an ephemeral askpass helper without putting credentials in argv/env.

    Git still needs a non-interactive credential source, but embedding a PAT in
    the clone URL exposes it through process listings.  The helper only receives
    paths to mode-0600 files; the temporary directory is removed immediately
    after the git process exits.
    """

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    username_file = directory / "username"
    password_file = directory / "password"
    askpass = directory / "askpass.sh"
    username_file.write_text(username, encoding="utf-8")
    password_file.write_text(token, encoding="utf-8")
    askpass.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*|*username*) cat \"$LUMA_GIT_USERNAME_FILE\" ;;\n"
        "  *) cat \"$LUMA_GIT_PASSWORD_FILE\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    for path, mode in ((username_file, 0o600), (password_file, 0o600), (askpass, 0o700)):
        try:
            path.chmod(mode)
        except OSError:
            pass
    return askpass, username_file, password_file


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
    parsed = urllib.parse.urlparse(safe_url)
    if parsed.scheme in {"http", "https", "ssh"}:
        if parsed.username or parsed.password:
            raise LumaError("git repository url must not contain inline credentials")
        if parsed.query or parsed.fragment:
            raise LumaError("git repository url must not contain query parameters or fragments")
    env = dict(os.environ)
    for name in (
        "GIT_TRACE",
        "GIT_TRACE2",
        "GIT_TRACE_PACKET",
        "GIT_TRACE_PERFORMANCE",
        "GIT_TRACE_SETUP",
        "GIT_TRACE_SHALLOW",
        "GIT_TRACE_CURL",
        "GIT_TRACE_CURL_NO_DATA",
        "GIT_CURL_VERBOSE",
    ):
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy

    command = ["git", "clone", "--depth", "1"]
    if ref:
        command += ["--branch", str(ref)]
    command += [safe_url, str(dest)]
    try:
        with tempfile.TemporaryDirectory(prefix="luma-git-auth-") as auth_tmp:
            if token and safe_url.startswith("https://"):
                askpass, username_file, password_file = _write_git_askpass(
                    Path(auth_tmp),
                    username=str(username or "x-access-token"),
                    token=str(token),
                )
                env["GIT_ASKPASS"] = str(askpass)
                env["GIT_ASKPASS_REQUIRE"] = "force"
                env["LUMA_GIT_USERNAME_FILE"] = str(username_file)
                env["LUMA_GIT_PASSWORD_FILE"] = str(password_file)
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
        output = _redact_secret(_redact(result.stdout.strip()), token)
        raise LumaError(f"git clone failed:\n{output}")
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


def head_commit_full(repo: Path) -> str:
    """Return the complete Git object id for snapshot binding.

    Keep ``head_commit`` unchanged for the legacy import image tag contract;
    Builder Task v1 must use this full value and never a short SHA.
    """

    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
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
    value = result.stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value):
        raise LumaError("git rev-parse returned an invalid full object id")
    return value.lower()


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
