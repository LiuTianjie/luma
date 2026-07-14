#!/usr/bin/env python3
"""Run supported LAE task-restart drills with continuous public sentinels."""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_SENTINELS = (
    "https://lae-staging.itool.tech/",
    "https://lae-api-staging.itool.tech/health/ready",
    "https://lae-agent-staging.itool.tech/health/ready",
    "https://lae-artifacts-staging.itool.tech/minio/health/ready",
    "https://gateway.itool.tech/",
)
API_READY_URL = "https://lae-api-staging.itool.tech/health/ready"
ADMIN_RESOURCES = ("users", "tenants", "applications", "operations", "usage")


class DrillFailure(RuntimeError):
    pass


def _request_status(url: str, *, timeout: float) -> int:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "lae-recovery-drill/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1024)
            return int(response.status)
    except urllib.error.HTTPError as error:
        error.read(1024)
        return int(error.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def _admin_snapshot(api_origin: str, token_file: Path, *, timeout: float) -> dict[str, int]:
    if not token_file.is_file() or token_file.is_symlink():
        raise DrillFailure("admin token file is not a private regular file")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise DrillFailure("admin token file is empty")
    result: dict[str, int] = {}
    for resource in ADMIN_RESOURCES:
        request = urllib.request.Request(
            f"{api_origin}/internal/v1/admin/{resource}?limit=1&offset=0",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "lae-recovery-drill/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read(256 * 1024).decode("utf-8"))
        except Exception as error:
            raise DrillFailure("admin snapshot request failed") from error
        page = body.get("page") if isinstance(body, dict) else None
        total = page.get("total") if isinstance(page, dict) else None
        if not isinstance(total, int):
            raise DrillFailure("admin snapshot response is invalid")
        result[resource] = total
    return result


def _validate_counts(
    service: str,
    counts: dict[str, collections.Counter[int]],
    transitions: list[dict[str, Any]],
    *,
    max_api_outage_seconds: float,
) -> None:
    for url, counter in counts.items():
        failures = sum(count for status, count in counter.items() if status != 200)
        if failures == 0:
            continue
        if service != "api" or url != API_READY_URL:
            raise DrillFailure(f"unexpected sentinel failure during {service} restart")
    if service != "api":
        return
    api_transitions = [item for item in transitions if item["url"] == API_READY_URL]
    failed_at: float | None = None
    recovered_at: float | None = None
    for item in api_transitions:
        if item["status"] != 200 and failed_at is None:
            failed_at = float(item["second"])
        if item["status"] == 200 and failed_at is not None:
            recovered_at = float(item["second"])
    if failed_at is not None and recovered_at is None:
        raise DrillFailure("API did not recover after task restart")
    if (
        failed_at is not None
        and recovered_at is not None
        and recovered_at - failed_at > max_api_outage_seconds
    ):
        raise DrillFailure("API recovery exceeded the admitted outage window")


def run(args: argparse.Namespace) -> dict[str, Any]:
    before: dict[str, int] | None = None
    token_file = Path(args.admin_token_file) if args.admin_token_file else None
    if args.service == "postgres":
        if token_file is None:
            raise DrillFailure("PostgreSQL drill requires --admin-token-file")
        before = _admin_snapshot(args.api_origin, token_file, timeout=args.request_timeout)

    process = subprocess.Popen(
        [
            "luma",
            "service",
            "restart",
            args.stack,
            "--service",
            args.service,
            "--mode",
            "task",
            "--timeout",
            str(int(args.restart_timeout)),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    finished_at: float | None = None
    counts = {url: collections.Counter() for url in args.sentinel}
    last: dict[str, int | None] = {url: None for url in args.sentinel}
    transitions: list[dict[str, Any]] = []
    while time.monotonic() - started < args.timeout:
        all_ready = True
        for url in args.sentinel:
            status = _request_status(url, timeout=args.request_timeout)
            counts[url][status] += 1
            if last[url] != status:
                transitions.append(
                    {
                        "second": round(time.monotonic() - started, 2),
                        "url": url,
                        "status": status,
                    }
                )
                last[url] = status
            all_ready = all_ready and status == 200
        if process.poll() is not None:
            if finished_at is None:
                finished_at = time.monotonic()
            elif time.monotonic() - finished_at >= args.stable_seconds and all_ready:
                break
        time.sleep(args.interval)
    else:
        process.terminate()
        raise DrillFailure("recovery drill timed out")

    stdout, stderr = process.communicate(timeout=5)
    if process.returncode != 0:
        raise DrillFailure("Luma task restart failed")
    _validate_counts(
        args.service,
        counts,
        transitions,
        max_api_outage_seconds=args.max_api_outage_seconds,
    )

    after: dict[str, int] | None = None
    if args.service == "postgres" and token_file is not None:
        after = _admin_snapshot(args.api_origin, token_file, timeout=args.request_timeout)
        if before != after:
            raise DrillFailure("admin snapshot changed across PostgreSQL restart")
    return {
        "service": args.service,
        "restartExitCode": process.returncode,
        "elapsedSeconds": round(time.monotonic() - started, 2),
        "statusCounts": {
            url: {str(status): count for status, count in sorted(counter.items())}
            for url, counter in counts.items()
        },
        "transitions": transitions,
        "adminSnapshot": after,
        "restartOutput": stdout[-1000:],
        "restartError": stderr[-1000:],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("service", choices=("worker", "api", "postgres"))
    result.add_argument("--stack", default="lae-platform-staging")
    result.add_argument("--sentinel", action="append", default=list(DEFAULT_SENTINELS))
    result.add_argument("--api-origin", default="https://lae-api-staging.itool.tech")
    result.add_argument("--admin-token-file")
    result.add_argument("--restart-timeout", type=float, default=120)
    result.add_argument("--timeout", type=float, default=180)
    result.add_argument("--stable-seconds", type=float, default=30)
    result.add_argument("--interval", type=float, default=0.5)
    result.add_argument("--request-timeout", type=float, default=5)
    result.add_argument("--max-api-outage-seconds", type=float, default=15)
    return result


def main() -> int:
    args = parser().parse_args()
    for url in args.sentinel:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname is None:
            print(json.dumps({"ok": False, "error": "sentinel must use HTTPS"}))
            return 1
    try:
        result = run(args)
    except (DrillFailure, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
