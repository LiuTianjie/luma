"""Versioned repository-build requirements shared by CLI and Control."""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .errors import LumaError

REQUIRED_BUILD_CAPABILITY = "required-build-capability-v1"
REPOSITORY_BUILD_INPUTS_CAPABILITY = "repository-build-inputs-v1"
REPOSITORY_BUILD_PLAN_CAPABILITY = "repository-build-plan-v1"
NATIVE_ARM_BUILD_CAPABILITY = "native-arm-build-v1"

VALID_NATIVE_PLATFORMS: frozenset[str] = frozenset({
    "linux/arm64",
    "linux/amd64",
    "darwin/arm64",
    "darwin/amd64",
})


def validate_native_platform(platform: str) -> str:
    """Validate and normalize a native single-architecture build platform.

    Enforces strict native building:
    - Multiple platforms or comma-separated lists are rejected with LumaError.
    - Architecture aliases (arm64/v8, aarch64 -> arm64; x86_64 -> amd64) are normalized.
    - Target must resolve to either linux/arm64, linux/amd64, or valid darwin/arm64 / darwin/amd64.
    """
    if platform is None:
        raise LumaError("native build platform is required")
    raw = str(platform).strip()
    if "," in raw:
        raise LumaError(
            "cross-architecture emulation and multi-platform compilation are disabled; "
            "submit builds separately for each native architecture"
        )
    if not raw:
        raise LumaError("native build platform is required")

    lowered = raw.lower()
    parts = [p.strip() for p in lowered.split("/") if p.strip()]

    if len(parts) == 1:
        os_name = "linux"
        arch = parts[0]
        variant = ""
    elif len(parts) == 2:
        if parts[0] in {"linux", "darwin"}:
            os_name = parts[0]
            arch = parts[1]
            variant = ""
        elif parts[0] in {"arm64", "aarch64"} and parts[1] in {"v8", ""}:
            os_name = "linux"
            arch = parts[0]
            variant = parts[1]
        else:
            os_name = parts[0]
            arch = parts[1]
            variant = ""
    elif len(parts) == 3:
        os_name, arch, variant = parts[0], parts[1], parts[2]
    else:
        raise LumaError(f"invalid platform format: {platform!r}")

    if arch in {"arm64", "aarch64"}:
        if variant not in {"", "v8"}:
            raise LumaError(
                f"unsupported architecture variant {variant!r} for {platform}; "
                "only ARM64 (v8) is supported"
            )
        norm_arch = "arm64"
    elif arch in {"amd64", "x86_64"}:
        if variant:
            raise LumaError(
                f"unsupported architecture variant {variant!r} for {platform}; "
                "AMD64 does not accept variants"
            )
        norm_arch = "amd64"
    else:
        raise LumaError(
            f"unsupported native architecture {arch!r} in {platform!r}; "
            "only arm64 and amd64 architectures are supported"
        )

    norm_platform = f"{os_name}/{norm_arch}"
    if norm_platform not in VALID_NATIVE_PLATFORMS:
        raise LumaError(
            f"unsupported native platform: {platform!r}; "
            "supported platforms are linux/arm64, linux/amd64 (or darwin/arm64, darwin/amd64)"
        )
    return norm_platform


def infer_native_platform(
    *,
    platform: str = "",
    target_node: str = "",
    target_region: str = "",
    node_architectures: Mapping[str, str] | None = None,
) -> str:
    """Infer the native target build platform based on deployment topology.

    Inference priority:
    1. If `platform` is provided and non-empty: validate and normalize via `validate_native_platform`.
    2. If `platform` is empty:
       - If `target_region == "home"` -> return "linux/arm64".
       - If `target_node` in `node_architectures` -> return normalized architecture.
       - If `target_node in {"macbook-pro", "air3"}` or `"arm64"` in `target_node.lower()` or `"aarch64"` in `target_node.lower()` -> return "linux/arm64".
       - Otherwise default to "linux/amd64".
    """
    clean_platform = str(platform or "").strip()
    if clean_platform:
        return validate_native_platform(clean_platform)

    clean_region = str(target_region or "").strip().lower()
    if clean_region == "home":
        return "linux/arm64"

    clean_node = str(target_node or "").strip()
    if clean_node and node_architectures:
        node_arch = node_architectures.get(clean_node)
        if not node_arch and clean_node.lower() in node_architectures:
            node_arch = node_architectures[clean_node.lower()]
        if node_arch:
            norm = validate_native_platform(str(node_arch).strip())
            if norm.startswith("darwin/"):
                return f"linux/{norm.partition('/')[2]}"
            return norm

    lower_node = clean_node.lower()
    if lower_node in {"macbook-pro", "air3"} or "arm64" in lower_node or "aarch64" in lower_node:
        return "linux/arm64"

    return "linux/amd64"


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
