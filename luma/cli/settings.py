"""Luma CLI: settings commands."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any, Dict
from ..envfile import parse_env_file
from ..errors import LumaError
from . import common
from .common import _configured_label, _control_context, _output_format, _print_deploy_step, _print_json, _print_success, _print_table, _quiet


def cmd_secret(args: argparse.Namespace) -> int:
    if args.secret_command in {"list", "set", "import"}:
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    if args.secret_command == "list":
        result = client.list_secrets()
        keys = result.get("secrets") if isinstance(result.get("secrets"), list) else []
        if _output_format(args) != "text":
            _print_success(args, {"secrets": keys})
            return 0
        if not keys:
            print("No deployment secrets configured")
            return 0
        for key in keys:
            print(str(key))
        return 0
    if args.secret_command == "set":
        if args.value is not None and args.value_stdin:
            raise LumaError("--value and --value-stdin cannot be used together")
        if args.value_stdin:
            value = sys.stdin.read()
            if value.endswith("\n"):
                value = value[:-1]
        else:
            value = args.value
        if value is None:
            value = getpass.getpass(f"{args.name}: ")
        if args.scope:
            result = client.set_secret(name=args.name, value=value, scope=str(args.scope))
        else:
            result = client.set_secret(name=args.name, value=value)
        scope = result.get("scope")
        label = f"{scope}/{result.get('name', args.name)}" if scope else result.get("name", args.name)
        print(f"Secret saved: {label}")
        return 0
    if args.secret_command == "import":
        values = parse_env_file(args.env_file)
        if not values:
            print(f"No secrets found in {args.env_file}")
            return 0
        for key, value in sorted(values.items()):
            client.set_secret(name=key, value=value, scope=str(args.scope))
        print(f"Secrets imported: {len(values)} into scope {args.scope}")
        return 0
    raise LumaError(f"unknown secret command: {args.secret_command}")


def cmd_registry(args: argparse.Namespace) -> int:
    if args.registry_command in {"list", "login", "remove", "serve", "images", "delete", "deletion", "gc", "policy"}:
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    if args.registry_command == "list":
        result = client.list_registries()
        items = result.get("registries") if isinstance(result.get("registries"), list) else []
        if _output_format(args) != "text":
            _print_success(args, {"registries": items})
            return 0
        if not items:
            print("No registry credentials configured")
            return 0
        for item in items:
            host = str(item.get("host") or item.get("serverAddress") or "")
            username = str(item.get("username") or "")
            print(f"{host}\t{username}")
        return 0
    if args.registry_command == "login":
        if args.password_stdin:
            password = sys.stdin.read().strip()
        else:
            password = getpass.getpass(f"{args.host} password/token: ")
        result = client.set_registry(host=args.host, username=args.username, password=password)
        print(f"Registry credential saved: {result.get('host', args.host)}")
        return 0
    if args.registry_command == "remove":
        result = client.remove_registry(host=args.host)
        status = "removed" if result.get("removed") else "not configured"
        print(f"Registry credential {status}: {result.get('host', args.host)}")
        return 0
    if args.registry_command == "serve":
        output_format = _output_format(args)
        quiet = _quiet(args) or output_format != "text"
        registry_password = ""
        if args.domain:
            if not args.username:
                raise LumaError("--username is required with --domain")
            registry_password = (
                sys.stdin.read().strip()
                if args.password_stdin
                else getpass.getpass(f"{args.domain} password: ")
            )
        serve_kwargs: Dict[str, Any] = dict(
            node=args.node,
            port=args.port,
            image=args.image,
            name=args.name,
            storage_class=args.storage_class,
            domain=args.domain,
            username=args.username,
            password=registry_password,
            activate=not args.no_activate,
            timeout=args.timeout,
        )
        if not quiet:
            print(f"[start] Deploy in-cluster registry on {args.node}:{args.port}", flush=True)
        result: Dict[str, Any] | None = None
        streamed = False
        try:
            for event in client.registry_serve_events(**serve_kwargs):
                status = str(event.get("status") or "")
                if output_format == "ndjson":
                    _print_json({"type": "event", **event})
                if status in {"start", "ok", "fail"}:
                    if not quiet:
                        _print_deploy_step(event)
                    if status == "fail":
                        raise LumaError(str(event.get("message") or "registry serve failed"))
                elif status == "done":
                    payload = event.get("result")
                    if isinstance(payload, dict):
                        result = payload
                streamed = True
        except LumaError as exc:
            if "control API error 404" not in str(exc):
                raise
        if result is None:
            if streamed:
                raise LumaError("control API stream ended without a registry serve result")
            result = client.registry_serve(**serve_kwargs)
        if output_format != "text":
            _print_success(args, result)
            return 0
        print(f"[ok] Registry ready: {result.get('registryHost')}")
        print(f"Push from build node: {result.get('pushHost')}")
        if result.get("configuredNodes"):
            print(f"insecure-registries configured on: {', '.join(result['configuredNodes'])}")
        if result.get("skippedNodes"):
            print(f"Skipped nodes: {', '.join(str(n) for n in result['skippedNodes'])}")
        return 0
    if args.registry_command == "images":
        result = client.registry_inventory(refresh=bool(args.refresh))
        if _output_format(args) != "text":
            _print_success(args, result)
            return 0
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        print(
            f"Registry {result.get('registry', {}).get('host', '-')}: "
            f"{summary.get('repositoryCount', 0)} repositories, {summary.get('tagCount', 0)} tags, "
            f"{summary.get('manifestCount', 0)} manifests"
        )
        print(
            f"Protected {summary.get('protectedCount', 0)}, retained {summary.get('retainedCount', 0)}, "
            f"candidates {summary.get('candidateCount', 0)}, unknown {summary.get('unknownCount', 0)}"
        )
        if usage.get("volumeBytes") is not None:
            print(
                f"Storage {usage.get('volumeBytes')} bytes; filesystem "
                f"{usage.get('filesystemUsePercent', '-')}% used"
            )
        for item in result.get("entries") or []:
            if not isinstance(item, dict):
                continue
            print(
                f"{item.get('repository')}@{item.get('digest')}\t"
                f"{','.join(str(tag) for tag in item.get('tags') or [])}\t"
                f"{item.get('protectionStatus')}\t{item.get('logicalBytes', 0)}"
            )
        return 0
    if args.registry_command == "delete":
        manifests = [{"repository": args.repository, "digest": args.digest}]
        preview = client.preview_registry_deletion(manifests, manual_override=True)
        if not preview.get("allowed"):
            raise LumaError(f"Registry deletion blocked: {preview.get('blocked')}")
        result = client.create_registry_deletion(manifests, manual_override=True)
        deletion = result.get("deletion") if isinstance(result.get("deletion"), dict) else {}
        if args.execute_now:
            result = client.registry_deletion_action(str(deletion.get("id") or ""), "execute", force=True)
        if _output_format(args) != "text":
            _print_success(args, result)
        else:
            current = result.get("deletion") if isinstance(result.get("deletion"), dict) else deletion
            print(f"Registry deletion {current.get('id')}: {current.get('status')}")
        return 0
    if args.registry_command == "deletion":
        result = client.registry_deletion_action(args.id, args.action, force=bool(args.force))
        if _output_format(args) != "text":
            _print_success(args, result)
        else:
            deletion = result.get("deletion") if isinstance(result.get("deletion"), dict) else {}
            print(f"Registry deletion {deletion.get('id', args.id)}: {deletion.get('status', args.action)}")
        return 0
    if args.registry_command == "gc":
        result = client.registry_gc(preview=not bool(args.execute), force=bool(args.force))
        if _output_format(args) != "text":
            _print_success(args, result)
        else:
            payload = result.get("preview") if isinstance(result.get("preview"), dict) else result.get("result") if isinstance(result.get("result"), dict) else result
            print(
                f"Registry GC {'complete' if args.execute else 'preview complete'}: "
                f"eligible={payload.get('eligibleBlobs', 0)}, reclaimed={payload.get('reclaimedBytes', 0)} bytes"
            )
        return 0
    if args.registry_command == "policy":
        changes = {
            key: value
            for key, value in {
                "mode": args.mode,
                "keepLast": args.keep_last,
                "maxAgeDays": args.max_age_days,
                "systemKeepLast": args.system_keep_last,
                "queueGraceHours": args.queue_grace_hours,
                "gcGraceDays": args.gc_grace_days,
                "warningPercent": args.warning_percent,
                "criticalPercent": args.critical_percent,
                "emergencyPercent": args.emergency_percent,
            }.items()
            if value is not None
        }
        if changes:
            current = client.registry_policy().get("policy") or {}
            result = client.set_registry_policy({**current, **changes})
        else:
            result = client.registry_policy()
        if _output_format(args) != "text":
            _print_success(args, result)
        else:
            print(json.dumps(result.get("policy") or {}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    raise LumaError(f"unknown registry command: {args.registry_command}")


def cmd_git_provider(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    output_format = _output_format(args)
    if args.git_provider_command == "list":
        result = client.list_git_providers()
        items = result.get("providers") if isinstance(result.get("providers"), list) else []
        if output_format != "text":
            _print_success(args, {"providers": items})
            return 0
        if not items:
            print("No Git provider credentials configured")
            return 0
        rows = [
            [
                str(item.get("id") or ""),
                str(item.get("type") or ""),
                str(item.get("account") or ""),
                str(item.get("username") or ""),
                _configured_label(bool(item.get("configured"))),
            ]
            for item in items
            if isinstance(item, dict)
        ]
        _print_table(["id", "type", "account", "username", "configured"], rows)
        return 0
    if args.git_provider_command == "set":
        if args.provider_token and args.token_stdin:
            raise LumaError("--git-token and --token-stdin cannot be used together")
        provider_token = sys.stdin.read().strip() if args.token_stdin else args.provider_token
        if not provider_token:
            provider_token = getpass.getpass(f"{args.type}:{args.account} token: ")
        result = client.set_git_provider(
            provider_type=args.type,
            account=args.account,
            token=provider_token,
            base_url=args.base_url,
            clone_base_url=args.clone_base_url,
            username=args.username,
        )
        print(f"Git provider saved: {result.get('id')}")
        return 0
    if args.git_provider_command == "remove":
        result = client.remove_git_provider(provider_id=args.id)
        status = "removed" if result.get("removed") else "not configured"
        print(f"Git provider {status}: {result.get('id', args.id)}")
        return 0
    if args.git_provider_command == "repos":
        result = client.list_git_provider_repositories(provider_id=args.id)
        items = result.get("repositories") if isinstance(result.get("repositories"), list) else []
        if output_format != "text":
            _print_success(args, {"repositories": items})
            return 0
        for item in items:
            if not isinstance(item, dict):
                continue
            private = "private" if item.get("private") else "public"
            print(f"{item.get('fullName')}\t{item.get('defaultBranch') or ''}\t{private}")
        return 0
    if args.git_provider_command == "refs":
        result = client.list_git_provider_refs(provider_id=args.id, repository=args.repository)
        items = result.get("refs") if isinstance(result.get("refs"), list) else []
        if output_format != "text":
            _print_success(args, {"refs": items})
            return 0
        for item in items:
            if isinstance(item, dict):
                print(f"{item.get('name')}\t{item.get('type')}")
        return 0
    raise LumaError(f"unknown git-provider command: {args.git_provider_command}")
