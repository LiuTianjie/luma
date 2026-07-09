from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import NodeConfig
from .errors import LumaError


# ssh exits 255 specifically for transport/connection failures (unreachable
# host, auth rejected, host-key mismatch) as opposed to the remote command's
# own non-zero exit — surfacing that distinction makes failures diagnosable.
SSH_TRANSPORT_EXIT = 255

# Guard rails so a stalled connection or a hung remote command can never block
# the CLI indefinitely. ConnectTimeout bounds the TCP/handshake phase;
# DEFAULT_REMOTE_TIMEOUT bounds the whole command.
SSH_CONNECT_TIMEOUT = 10
DEFAULT_REMOTE_TIMEOUT = 900


def _ssh_base_args() -> list[str]:
    # BatchMode=yes: never block on an interactive password/passphrase prompt.
    # -n: redirect stdin from /dev/null so a prompt can't hang on the local tty
    #     (the sudo -S password path pipes the password inside the remote shell,
    #     not over ssh stdin, so this is safe).
    return [
        "ssh",
        "-n",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
    ]


@dataclass(frozen=True)
class RemoteResult:
    code: int
    output: str


class RemoteExecutor:
    def __init__(self, node: NodeConfig):
        self.node = node

    def run_result(self, command: str, *, timeout: int | None = DEFAULT_REMOTE_TIMEOUT) -> RemoteResult:
        try:
            result = subprocess.run(
                [*_ssh_base_args(), self.node.host, f"bash -lc {shlex.quote(command)}"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.output if isinstance(exc.output, str) else ""
            raise LumaError(
                f"remote command on {self.node.name} timed out after {timeout}s"
                + (f":\n{output.strip()}" if output.strip() else "")
            ) from exc
        return RemoteResult(code=result.returncode, output=result.stdout)

    def run(self, command: str, *, check: bool = True, timeout: int | None = DEFAULT_REMOTE_TIMEOUT) -> str:
        result = self.run_result(command, timeout=timeout)
        if check and result.code != 0:
            if result.code == SSH_TRANSPORT_EXIT:
                raise LumaError(
                    f"ssh connection to {self.node.name} ({self.node.host}) failed "
                    f"(unreachable, auth rejected, or host-key issue):\n{result.output.strip()}"
                )
            raise LumaError(f"remote command failed on {self.node.name}:\n{result.output.strip()}")
        return result.output

    def sudo(self, command: str, *, check: bool = True) -> str:
        result = self.sudo_result(command)
        if check and result.code != 0:
            if _sudo_auth_failed(result.output):
                raise LumaError(
                    f"remote sudo requires a password on {self.node.name}. "
                    "Set LUMA_SUDO_PASSWORD in .env or configure passwordless sudo."
                )
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
            remote_command = f"sudo -n bash -lc {quoted}"
        return self.run_result(remote_command)

    def upload(self, local: Path, remote_path: str, *, timeout: int | None = DEFAULT_REMOTE_TIMEOUT) -> str:
        local = local.resolve()
        try:
            result = subprocess.run(
                [
                    "scp",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
                    "-r",
                    str(local),
                    f"{self.node.host}:{remote_path}",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.output if isinstance(exc.output, str) else ""
            raise LumaError(
                f"upload to {self.node.name} timed out after {timeout}s"
                + (f":\n{output.strip()}" if output.strip() else "")
            ) from exc
        if result.returncode != 0:
            raise LumaError(f"upload failed to {self.node.name}:\n{result.stdout.strip()}")
        return f"Uploaded {local} -> {self.node.name}:{remote_path}"

    def write_secret(self, content: str, remote_path: str, *, mode: str = "600") -> str:
        fh = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        local = Path(fh.name)
        try:
            with fh:
                fh.write(content)
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
