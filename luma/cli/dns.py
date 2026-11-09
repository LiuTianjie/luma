"""``luma dns``: DNS zones and the providers that host them."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Dict

from ..dns_providers import FIELD_LABELS, PROVIDER_ENV, PROVIDER_FIELDS, PROVIDER_LABELS, normalize_provider
from ..errors import LumaError
from . import common
from .common import _control_context, _format_epoch, _output_format, _print_success, _print_table


def cmd_dns(args: argparse.Namespace) -> int:
    if args.dns_command == "zone":
        command = args.dns_zone_command
        if command == "list":
            return cmd_dns_zone_list(args)
        if command == "add":
            return cmd_dns_zone_add(args)
        if command == "check":
            return cmd_dns_zone_check(args)
        if command == "remove":
            return cmd_dns_zone_remove(args)
        raise LumaError(f"unknown dns zone command: {command}")
    raise LumaError(f"unknown dns command: {args.dns_command}")


def _client(args: argparse.Namespace) -> common.ControlClient:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    return common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)


def cmd_dns_zone_list(args: argparse.Namespace) -> int:
    result = _client(args).list_dns_zones()
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    zones = [item for item in result.get("zones") or [] if isinstance(item, dict)]
    if not zones:
        print("No DNS zones. Add one with: luma dns zone add example.com --provider cloudflare")
        return 0
    rows = []
    for zone in zones:
        status = str(zone.get("status") or "-")
        if zone.get("primary"):
            status = f"{status} (primary)"
        rows.append([
            str(zone.get("name") or ""),
            str(zone.get("providerLabel") or zone.get("provider") or ""),
            status,
            str(len(zone.get("services") or [])),
            _format_epoch(zone.get("checkedAt")) if zone.get("checkedAt") else "-",
        ])
    _print_table(["ZONE", "PROVIDER", "STATUS", "APPS", "CHECKED"], rows)
    return 0


def _prompt_credentials(provider: str, *, interactive: bool) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for field in PROVIDER_FIELDS[provider]:
        env_name = PROVIDER_ENV[provider][field]
        value = os.environ.get(env_name, "").strip()
        if not value and interactive:
            value = getpass.getpass(f"{PROVIDER_LABELS[provider]} {FIELD_LABELS[field]}: ").strip()
        if not value:
            names = ", ".join(PROVIDER_ENV[provider][name] for name in PROVIDER_FIELDS[provider])
            raise LumaError(f"{PROVIDER_LABELS[provider]} {FIELD_LABELS[field]} is required; run interactively or set {names}")
        values[field] = value
    return values


def cmd_dns_zone_add(args: argparse.Namespace) -> int:
    provider = normalize_provider(args.provider)
    same_as = str(getattr(args, "same_as", "") or "").strip()
    credentials: Dict[str, str] = {}
    if not same_as:
        credentials = _prompt_credentials(provider, interactive=sys.stdin.isatty())
    result = _client(args).add_dns_zone(
        name=args.zone,
        provider=provider,
        credentials=credentials,
        same_as=same_as,
        update=bool(getattr(args, "update", False)),
    )
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    zone = result.get("zone") if isinstance(result.get("zone"), dict) else {}
    verb = "updated" if getattr(args, "update", False) else "added"
    print(f"DNS zone {verb}: {zone.get('name') or args.zone} ({zone.get('providerLabel') or PROVIDER_LABELS[provider]})")
    if zone.get("detail"):
        print(f"  {zone['detail']}")
    print("  Services with a domain in this zone now publish their records here on the next deploy.")
    return 0


def cmd_dns_zone_check(args: argparse.Namespace) -> int:
    result = _client(args).check_dns_zone(name=args.zone)
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0 if result.get("status") == "ready" else 1
    print(f"{result.get('name') or args.zone}: {result.get('status')}")
    if result.get("detail"):
        print(f"  {result['detail']}")
    return 0 if result.get("status") == "ready" else 1


def cmd_dns_zone_remove(args: argparse.Namespace) -> int:
    result = _client(args).remove_dns_zone(name=args.zone)
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    print(f"DNS zone removed: {result.get('name') or args.zone}. Existing DNS records at the provider are left as they are.")
    return 0
