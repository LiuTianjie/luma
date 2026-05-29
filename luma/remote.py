from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import NodeConfig
from .errors import LumaError


@dataclass(frozen=True)
class RemoteResult:
    code: int
    output: str


class RemoteExecutor:
    def __init__(self, node: NodeConfig):
        self.node = node

    def run_result(self, command: str) -> RemoteResult:
        result = subprocess.run(
            ["ssh", self.node.host, f"bash -lc {shlex.quote(command)}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        return RemoteResult(code=result.returncode, output=result.stdout)

    def run(self, command: str, *, check: bool = True) -> str:
        result = self.run_result(command)
        if check and result.code != 0:
            raise LumaError(f"remote command failed on {self.node.name}:\n{result.output.strip()}")
        return result.output

    def sudo(self, command: str, *, check: bool = True) -> str:
        result = self.sudo_result(command)
        if check and result.code != 0:
            raise LumaError(f"remote sudo command failed on {self.node.name}:\n{result.output.strip()}")
        return result.output

    def sudo_result(self, command: str) -> RemoteResult:
        password = os.environ.get("LUMA_SUDO_PASSWORD")
        quoted = shlex.quote(command)
        if password:
            remote_command = (
                f"printf '%s\\n' {shlex.quote(password)} | "
                f"sudo -S bash -lc {quoted}"
            )
        else:
            remote_command = f"sudo bash -lc {quoted}"
        return self.run_result(remote_command)

    def upload(self, local: Path, remote_path: str) -> str:
        local = local.resolve()
        result = subprocess.run(
            ["scp", "-r", str(local), f"{self.node.host}:{remote_path}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode != 0:
            raise LumaError(f"upload failed to {self.node.name}:\n{result.stdout.strip()}")
        return f"Uploaded {local} -> {self.node.name}:{remote_path}"

    def write_secret(self, content: str, remote_path: str, *, mode: str = "600") -> str:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as fh:
            fh.write(content)
            local = Path(fh.name)
        try:
            tmp_remote = f"/tmp/luma-secret-{os.getpid()}"
            self.upload(local, tmp_remote)
            self.sudo(
                "set -euo pipefail; "
                f"install -D -m {shlex.quote(mode)} {shlex.quote(tmp_remote)} {shlex.quote(remote_path)}; "
                f"rm -f {shlex.quote(tmp_remote)}"
            )
        finally:
            local.unlink(missing_ok=True)
        return f"Secret written: {remote_path}"
