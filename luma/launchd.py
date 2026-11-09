"""Reload managed system services after launchd finishes unloading them."""
from __future__ import annotations

import shlex
import time

from .errors import LumaError


def restart_service(remote, *, label: str, plist: str) -> None:
    result = remote.sudo_result(shlex.join(["launchctl", "bootout", "system/" + label]), timeout=30)
    # A first installation has no registered service to unload.
    if result.code not in {0, 3, 113}:
        raise LumaError(f"launchd could not unload {label}:\n{result.output.strip()}")
    command = shlex.join(["launchctl", "bootstrap", "system", plist])
    for attempt in range(5):
        result = remote.sudo_result(command, timeout=10)
        if result.code == 0:
            return
        # bootout may return before launchd releases the service registration.
        # Retry this transient error without unloading again or masking a
        # persistent invalid plist, permissions error or command timeout.
        if attempt == 4 or "Bootstrap failed: 5:" not in result.output:
            raise LumaError(f"launchd could not load {label}:\n{result.output.strip()}")
        time.sleep(1)
