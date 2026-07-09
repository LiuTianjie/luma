from __future__ import annotations

import os
import signal
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import LumaError


@dataclass(frozen=True)
class LocalResult:
    code: int
    output: str


class LocalExecutor:
    def run_result(self, command: str, *, timeout: int | None = None) -> LocalResult:
        process = subprocess.Popen(
            ["bash", "-lc", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Bound the post-kill drain: an orphaned grandchild that inherited
            # the stdout pipe can otherwise keep communicate() blocked forever
            # even after the direct child was killed.
            try:
                stdout, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout = ""
            output = _text_output(exc.stdout) + _text_output(stdout)
            message = f"command timed out after {timeout}s"
            return LocalResult(code=124, output=(str(output) + "\n" + message).strip())
        return LocalResult(code=process.returncode or 0, output=stdout or "")

    def run(self, command: str, *, check: bool = True, timeout: int | None = None) -> str:
        result = self.run_result(command, timeout=timeout)
        if check and result.code != 0:
            raise LumaError(f"local command failed:\n{result.output.strip()}")
        return result.output

    def sudo(self, command: str, *, check: bool = True, timeout: int | None = None) -> str:
        result = self.sudo_result(command, timeout=timeout)
        if check and result.code != 0:
            if _sudo_auth_failed(result.output):
                raise LumaError(
                    "local sudo requires a password. Run with sudo, set LUMA_SUDO_PASSWORD, "
                    "or configure passwordless sudo."
                )
            raise LumaError(f"local sudo command failed:\n{result.output.strip()}")
        return result.output

    def sudo_result(self, command: str, *, timeout: int | None = None) -> LocalResult:
        if os.geteuid() == 0:
            return self.run_result(command, timeout=timeout)
        password = os.environ.get("LUMA_SUDO_PASSWORD")
        quoted = shlex.quote(command)
        if password:
            return self.run_result(
                f"printf '%s\\n' {shlex.quote(password)} | sudo -S bash -lc {quoted}",
                timeout=timeout,
            )
        return self.run_result(f"sudo -n bash -lc {quoted}", timeout=timeout)

    def upload(self, local: Path, remote_path: str) -> str:
        source = local.resolve()
        target = Path(remote_path)
        if source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return f"Copied {source} -> {target}"

    def write_secret(self, content: str, remote_path: str, *, mode: str = "600") -> str:
        fh = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        local = Path(fh.name)
        try:
            with fh:
                fh.write(content)
            tmp_path = f"/tmp/luma-secret-{os.getpid()}"
            self.upload(local, tmp_path)
            self.sudo(
                "set -euo pipefail; "
                f"install -D -m {shlex.quote(mode)} {shlex.quote(tmp_path)} {shlex.quote(remote_path)}; "
                f"rm -f {shlex.quote(tmp_path)}"
            )
        finally:
            local.unlink(missing_ok=True)
        return f"Secret written: {remote_path}"


def _sudo_auth_failed(output: str) -> bool:
    lower = output.lower()
    return (
        "a terminal is required" in lower
        or "no tty present" in lower
        or "no password was provided" in lower
        or "sorry, try again" in lower
        or "incorrect password" in lower
        or "password is required" in lower
    )


def _text_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
