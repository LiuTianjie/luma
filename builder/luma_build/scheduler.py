"""Native architecture enforcement, cache-affinity window, and runner pool ranking."""
from __future__ import annotations

from typing import Any, Iterable

from .errors import BuildError

RUNNER_ONLINE_TTL_SECONDS = 90
DEFAULT_AFFINITY_WAIT_SECONDS = 20


def normalize_platform(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    if len(parts) < 2:
        raise BuildError(f"invalid container platform: {value}")
    os_name, arch = parts[0], parts[1]
    if os_name == "darwin":
        # macOS Docker Desktop / OrbStack builds Linux container images natively.
        os_name = "linux"
    if arch in {"x86_64", "amd64"}:
        arch = "amd64"
    elif arch in {"aarch64", "arm64"}:
        arch = "arm64"
    elif arch in {"armv7l", "armhf", "arm"}:
        return f"{os_name}/arm/v7"
    if len(parts) >= 3 and parts[2] not in {"v8"}:
        return f"{os_name}/{arch}/{parts[2]}"
    return f"{os_name}/{arch}"


def parse_platforms(value: str | Iterable[str] | None) -> list[str]:
    if not value:
        return ["linux/amd64"]
    items = [value] if isinstance(value, str) else list(value)
    result: list[str] = []
    for item in items:
        for part in str(item).split(","):
            norm = normalize_platform(part)
            if norm and norm not in result:
                result.append(norm)
    return result or ["linux/amd64"]


def required_native_arm_platform(platforms: Iterable[str]) -> str:
    """Any build targeting ARM requires a verified native ARM engine (no QEMU)."""
    arm_targets = set()
    for p in parse_platforms(list(platforms)):
        if "/arm64" in p or "/arm/" in p:
            arm_targets.add(p)
    if len(arm_targets) > 1:
        raise BuildError(
            f"cannot combine multiple ARM architectures in one build ({', '.join(sorted(arm_targets))}); "
            "ARM emulation is disabled"
        )
    return next(iter(arm_targets), "")


def runner_is_online(runner: dict[str, Any], *, now: int, ttl: int = RUNNER_ONLINE_TTL_SECONDS) -> bool:
    if runner.get("revoked"):
        return False
    last_seen = int(runner.get("lastSeenAt") or 0)
    return last_seen > 0 and (now - last_seen) <= ttl


def runner_matches_build(
    runner: dict[str, Any],
    *,
    required_platforms: list[str],
    pinned_runner: str = "",
    required_labels: dict[str, str] | None = None,
) -> bool:
    if runner.get("revoked"):
        return False
    name = str(runner.get("name") or "")
    if pinned_runner and name != pinned_runner:
        return False

    if required_labels:
        labels = runner.get("labels") if isinstance(runner.get("labels"), dict) else {}
        for k, v in required_labels.items():
            if str(labels.get(k) or "") != str(v):
                return False

    runner_platform = normalize_platform(str(runner.get("nativePlatform") or ""))
    if not runner_platform:
        return False

    arm_needed = required_native_arm_platform(required_platforms)
    if arm_needed:
        return runner_platform == arm_needed

    if len(required_platforms) == 1:
        return runner_platform == required_platforms[0]
    return True


def is_runner_eligible_for_lease(
    *,
    polling_runner: dict[str, Any],
    all_runners: list[dict[str, Any]],
    active_counts: dict[str, int],
    build: dict[str, Any],
    warm_runner_name: str,
    now: int,
    affinity_wait_seconds: int = DEFAULT_AFFINITY_WAIT_SECONDS,
) -> bool:
    """Decide whether ``polling_runner`` may claim ``build`` right now.

    1. Must match architecture, pin, and labels, and have a free concurrency slot.
    2. If a pinned runner is requested, only that runner can claim it.
    3. Cache Affinity (Tier-1 local SSD BuildKit cache): if ``warm_runner_name``
       is online, compatible, and different from ``polling_runner``, hold the job
       for up to ``affinity_wait_seconds`` so the warm runner can claim it—UNLESS
       every slot on the warm runner is currently busy and ``affinity_wait_seconds``
       is 0, or the wait window has elapsed.
    4. Among idle non-warm runners, let strictly higher-weight idle online runners
       claim first within a short 3-second grace window so cloud high-bandwidth
       runners naturally outrank home fallback runners when both are idle.
    """
    name = str(polling_runner.get("name") or "")
    max_slots = max(int(polling_runner.get("maxConcurrency") or 1), 1)
    if active_counts.get(name, 0) >= max_slots:
        return False

    platforms = parse_platforms(build.get("platforms") or ["linux/amd64"])
    pinned = str(build.get("pinnedRunner") or "")
    req_labels = build.get("requiredLabels") if isinstance(build.get("requiredLabels"), dict) else None

    if not runner_matches_build(
        polling_runner,
        required_platforms=platforms,
        pinned_runner=pinned,
        required_labels=req_labels,
    ):
        return False

    if pinned:
        return name == pinned

    queued_age = max(now - int(build.get("createdAt") or now), 0)

    # Warm BuildKit cache affinity check
    if warm_runner_name and name != warm_runner_name and queued_age < max(affinity_wait_seconds, 0):
        warm_record = next((r for r in all_runners if r.get("name") == warm_runner_name), None)
        if warm_record and runner_is_online(warm_record, now=now) and runner_matches_build(
            warm_record, required_platforms=platforms, required_labels=req_labels
        ):
            # If the warm runner is idle (about to poll) or busy within the short
            # affinity window, give it the head start to reuse its hot BuildKit cache.
            return False

    if name == warm_runner_name:
        return True

    # Prefer higher-weight idle online runners during the first 3 seconds
    my_weight = int(polling_runner.get("weight") or 100)
    if queued_age < 3:
        for other in all_runners:
            other_name = str(other.get("name") or "")
            if other_name == name or not runner_is_online(other, now=now):
                continue
            other_max = max(int(other.get("maxConcurrency") or 1), 1)
            if active_counts.get(other_name, 0) >= other_max:
                continue
            if int(other.get("weight") or 100) > my_weight and runner_matches_build(
                other, required_platforms=platforms, required_labels=req_labels
            ):
                return False

    return True
