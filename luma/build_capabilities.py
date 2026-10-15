"""Versioned repository-build requirements shared by CLI and Control."""
from __future__ import annotations

import re
from typing import Any, Iterable

from .errors import LumaError

REQUIRED_BUILD_CAPABILITY = "required-build-capability-v1"
REPOSITORY_BUILD_INPUTS_CAPABILITY = "repository-build-inputs-v1"
REPOSITORY_BUILD_PLAN_CAPABILITY = "repository-build-plan-v1"
NATIVE_ARM_BUILD_CAPABILITY = "native-arm-build-v1"


def native_arm_build_platform(platforms: Iterable[str]) -> str:
    """Return the ARM engine a build requires, never an emulated substitute."""
    required = set()
    for value in platforms:
        for platform in str(value).lower().split(","):
            parts = platform.strip().split("/")
            if len(parts) < 2:
                continue
            os_name, arch = parts[:2]
            variant = parts[2] if len(parts) > 2 else ""
            if arch in {"arm64", "aarch64"}:
                if variant not in {"", "v8"}:
                    raise LumaError(f"cannot verify a native builder for {platform}; ARM emulation is disabled")
                required.add(f"{os_name}/arm64")
            elif arch.startswith("arm"):
                variant = variant or {"armv6l": "v6", "armv7l": "v7", "armhf": "v7", "arm": "v7"}.get(arch)
                if not variant:
                    raise LumaError(f"cannot verify a native builder for {platform}; ARM emulation is disabled")
                required.add(f"{os_name}/arm/{variant}")
    if len(required) > 1:
        raise LumaError("builds targeting different ARM architectures must be submitted separately; ARM emulation is disabled")
    return next(iter(required), "")


def normalize_build_capabilities(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32 or any(
        not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,95}", item)
        for item in value
    ):
        raise LumaError("requireBuildCapabilities must be an array of at most 32 capability names")
    return sorted(set(value))


def repository_build_requirements(value: Any, *, fallback_manifest: bool = False) -> list[str]:
    required = normalize_build_capabilities(value)
    if fallback_manifest:
        required = sorted(set(required + [REPOSITORY_BUILD_INPUTS_CAPABILITY]))
    return required


def require_build_result_capabilities(result: dict[str, Any], required: list[str], *, owner: str) -> None:
    if not required:
        return
    advertised = result.get("buildCapabilities")
    honored = {item for item in advertised if isinstance(item, str)} if isinstance(advertised, list) else set()
    missing = sorted(set(required) - honored)
    if missing:
        raise LumaError(f"{owner} did not confirm required build capabilities: {', '.join(missing)}; refusing to continue")
