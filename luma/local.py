from __future__ import annotations

import os
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
    def run_result(self, command: str) -> LocalResult:
        result = subprocess.run(
            ["bash", "-lc", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        return LocalResult(code=result.returncode, output=result.stdout)

    def run(self, command: str, *, check: bool = True) -> str:
        result = self.run_result(command)
        if check and result.code != 0:
            raise LumaError(f"local command failed:\n{result.output.strip()}")
        return result.output

    def sudo(self, command: str, *, check: bool = True) -> str:
        result = self.sudo_result(command)
        if check and result.code != 0:
            if _sudo_auth_failed(result.output):
                raise LumaError(
                    "local sudo requires a password. Run with sudo, set LUMA_SUDO_PASSWORD, "
                    "or configure passwordless sudo."
                )
            raise LumaError(f"local sudo command failed:\n{result.output.strip()}")
        return result.output

    def sudo_result(self, command: str) -> LocalResult:
        if os.geteuid() == 0:
            return self.run_result(command)
        password = os.environ.get("LUMA_SUDO_PASSWORD")
        quoted = shlex.quote(command)
        if password:
            return self.run_result(f"printf '%s\\n' {shlex.quote(password)} | sudo -S bash -lc {quoted}")
        return self.run_result(f"sudo -n bash -lc {quoted}")

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
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as fh:
            fh.write(content)
            local = Path(fh.name)
        try:
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
