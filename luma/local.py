from __future__ import annotations

import os
import json
import signal
import shlex
import shutil
import subprocess
import tempfile
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import LumaError


@dataclass(frozen=True)
class LocalResult:
    code: int
    output: str


def _sudo_stdin_command(command: str, *, password: bool) -> str:
    target = "sudo -n bash -lc " + shlex.quote(command)
    if not password:
        return target
    # Validate and execute with the same parent shell (sudo commonly keys its
    # timestamp by parent PID). Read exactly one line ourselves even when sudo
    # is passwordless/cached, keeping the installer's remaining stdin intact.
    return ("IFS= read -r luma_private_sudo_password || exit 1; "
            "printf '%s\\n' \"$luma_private_sudo_password\" | sudo -S -p '' -v || exit 1; "
            "unset luma_private_sudo_password; " + target + "; luma_private_status=$?; exit \"$luma_private_status\"")


def _private_input_error(output: str, content: str, code: int) -> LumaError:
    values = {content, content.strip()}
    def collect(value):
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str) and value:
            values.add(value)
    try:
        collect(json.loads(content))
    except ValueError:
        pass
    detail = output
    for value in sorted((value for value in values if value), key=len, reverse=True):
        detail = detail.replace(value, "<redacted>").replace(json.dumps(value)[1:-1], "<redacted>")
    return LumaError(f"private installer input failed (exit {code}):\n{detail[:4096].strip()}")


class LocalExecutor:
    def run_result(self, command: str, *, timeout: int | None = None, input_data: str | None = None) -> LocalResult:
        process = subprocess.Popen(
            ["bash", "-lc", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if input_data is not None else None,
            start_new_session=True,
        )
        try:
            stdout, _ = process.communicate(input=input_data, timeout=timeout) if input_data is not None else process.communicate(timeout=timeout)
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
        if password:
            return self.run_result(_sudo_stdin_command(command, password=True), timeout=timeout, input_data=password + "\n")
        return self.run_result(_sudo_stdin_command(command, password=False), timeout=timeout)

    def sudo_input(self, command: str, content: str, *, timeout: int = 120) -> str:
        """Send a private installer payload over stdin, never a file or argv."""
        password = ""
        if os.geteuid() != 0:
            password = os.environ.get("LUMA_SUDO_PASSWORD") or ""
            command = _sudo_stdin_command(command, password=bool(password))
        result = self.run_result(command, timeout=timeout, input_data=(password + "\n" if password else "") + content)
        if result.code:
            raise _private_input_error(result.output.replace(password, "<redacted>") if password else result.output, content, result.code)
        return result.output

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
            # The source is already local and private. Do not copy credentials
            # through a predictable /tmp name, and do not depend on GNU
            # install -D (which is unavailable on macOS).
            script = (
                "import os,pathlib,sys,tempfile\n"
                "source=pathlib.Path(sys.argv[1]); target=pathlib.Path(sys.argv[2])\n"
                "mode=int(sys.argv[3],8); target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)\n"
                "if target.is_symlink() or target.parent.is_symlink(): raise OSError('unsafe secret path')\n"
                "fd,name=tempfile.mkstemp(prefix='.luma-secret-',dir=target.parent)\n"
                "try:\n"
                " with os.fdopen(fd,'wb') as handle:\n"
                "  os.fchmod(handle.fileno(),mode); handle.write(source.read_bytes())\n"
                "  handle.flush(); os.fsync(handle.fileno())\n"
                " os.replace(name,target)\n"
                "finally:\n"
                " pathlib.Path(name).unlink(missing_ok=True)\n"
            )
            self.sudo(shlex.join([sys.executable, "-c", script, str(local), remote_path, mode]))
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
