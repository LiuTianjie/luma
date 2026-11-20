"""Integrated ``luma eye`` client commands using Luma Control auto-discovery and token reuse.

Automatically resolves the deployed Luma Eye URL from Control's ``/v1/addons/eye``
endpoint and authenticates with the active ``luma login`` token — no separate
``luma-eye login`` or static admin token copy-pasting required.
"""
from __future__ import annotations

import argparse
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from ..errors import LumaError
from . import common


def add_parser(
    sub: argparse._SubParsersAction,
    _command,
    _commands,
    _add_control_arguments,
    _add_output_arguments,
) -> None:
    eye = _command(
        sub,
        "eye",
        "Query Luma Eye analytics, crashes and logs using your Luma login",
        description=(
            "Operate Luma Eye directly through your active `luma login` identity. "
            "The Eye endpoint is auto-discovered from Control unless `--url` or `LUMA_EYE_URL` is provided."
        ),
        epilog=(
            "Examples:\n"
            "  luma eye status\n"
            "  luma eye projects list\n"
            "  luma eye portfolio --since 24h\n"
            "  luma eye stats itool-tech --since 7d\n"
            "  luma eye issues itool-tech --status open"
        ),
    )
    eye_sub = _commands(eye, "eye_command")

    def _add_eye_flags(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--url", dest="eye_url", default="", help="Override Luma Eye URL (default: auto-discovered from Control)")
        _add_control_arguments(parser)
        _add_output_arguments(parser)

    p_status = _command(eye_sub, "status", "Check Luma Eye connectivity, auth mode and storage usage")
    _add_eye_flags(p_status)

    p_proj = _command(eye_sub, "projects", "List, create or delete monitored projects")
    p_proj_sub = _commands(p_proj, "projects_command")
    p_proj_list = _command(p_proj_sub, "list", "List registered projects")
    _add_eye_flags(p_proj_list)
    p_proj_ensure = _command(p_proj_sub, "ensure", "Create or update a project and return its public key / DSN")
    p_proj_ensure.add_argument("slug", help="Project slug (for example itool-tech)")
    p_proj_ensure.add_argument("--name", default="", help="Display name")
    p_proj_ensure.add_argument("--origins", default="", help="Comma-separated allowed HTTPS origins")
    p_proj_ensure.add_argument("--rps", type=int, default=200, help="Ingest rate limit per second (default: 200)")
    _add_eye_flags(p_proj_ensure)
    p_proj_delete = _command(p_proj_sub, "delete", "Delete a registered project")
    p_proj_delete.add_argument("slug", help="Project slug")
    _add_eye_flags(p_proj_delete)

    p_port = _command(eye_sub, "portfolio", "Show cross-project traffic, crash-free rate and Web Vitals")
    p_port.add_argument("--since", default="24h", help="Lookback window such as 1h, 24h, 7d (default: 24h)")
    _add_eye_flags(p_port)

    p_stats = _command(eye_sub, "stats", "Show overview metrics for a single project")
    p_stats.add_argument("project", help="Project slug")
    p_stats.add_argument("--since", default="24h", help="Lookback window (default: 24h)")
    _add_eye_flags(p_stats)

    p_issues = _command(eye_sub, "issues", "List or resolve production crash issues")
    p_issues.add_argument("project", help="Project slug")
    p_issues.add_argument("--since", default="7d", help="Lookback window (default: 7d)")
    p_issues.add_argument("--status", default="open", choices=("open", "resolved", "ignored", "all"), help="Issue status filter (default: open)")
    p_issues.add_argument("--detail", default="", metavar="FINGERPRINT", help="Inspect symbolicated stack trace and breadcrumbs for one issue")
    p_issues.add_argument("--resolve", default="", metavar="FINGERPRINT", help="Mark an issue resolved")
    _add_eye_flags(p_issues)

    p_logs = _command(eye_sub, "logs", "Search client error and warning logs")
    p_logs.add_argument("project", help="Project slug")
    p_logs.add_argument("--level", default="", help="Filter by log level (error, warn, info)")
    p_logs.add_argument("--search", default="", help="Substring search")
    p_logs.add_argument("--trace", default="", help="Correlated W3C trace_id")
    p_logs.add_argument("--since", default="1h", help="Lookback window (default: 1h)")
    p_logs.add_argument("--limit", type=int, default=100, help="Maximum rows (default: 100)")
    _add_eye_flags(p_logs)


def _resolve_eye_connection(args: argparse.Namespace) -> Tuple[str, str, bool]:
    explicit_url = common._arg_text(args, "eye_url") or common._env_text("LUMA_EYE_URL")
    explicit_token = common._arg_text(args, "token") or common._env_text("LUMA_EYE_ADMIN_TOKEN")
    cli_insecure = bool(getattr(args, "insecure", False)) or common._env_bool("LUMA_INSECURE")

    if explicit_url and explicit_token:
        return explicit_url.rstrip("/"), explicit_token, cli_insecure

    ctl_url, ctl_token, ctl_insecure, resolve_ip = common._control_context(args, require_token=True)
    token = explicit_token or ctl_token
    if explicit_url:
        return explicit_url.rstrip("/"), token, bool(cli_insecure or ctl_insecure)

    client = common.ControlClient(ctl_url, ctl_token, insecure=ctl_insecure, resolve_ip=resolve_ip)
    info = client.request("GET", "/v1/addons/eye")
    addon = info.get("addon") if isinstance(info.get("addon"), dict) else {}
    discovered = str(addon.get("url") or "").strip().rstrip("/")
    if not discovered:
        raise LumaError("Luma Eye URL is not configured on Control; run `luma addon install eye` or pass --url")
    return discovered, token, bool(cli_insecure or ctl_insecure)


def _eye_request(
    base_url: str,
    token: str,
    method: str,
    path: str,
    *,
    body: Optional[Dict[str, Any]] = None,
    insecure: bool = False,
) -> Dict[str, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method, headers=headers)
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
            return parsed if isinstance(parsed, dict) else {"data": parsed}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        try:
            err_json = json.loads(detail)
            if isinstance(err_json, dict) and err_json.get("error"):
                detail = str(err_json["error"])
        except ValueError:
            pass
        raise LumaError(f"Luma Eye API {method} {path} failed ({exc.code}): {detail or exc.reason}") from None
    except OSError as exc:
        raise LumaError(f"cannot reach Luma Eye at {base_url}: {exc}") from None


def cmd_eye(args: argparse.Namespace) -> int:
    base_url, token, insecure = _resolve_eye_connection(args)
    output_format = common._output_format(args)
    cmd = args.eye_command

    if cmd == "status":
        status = _eye_request(base_url, token, "GET", "/api/v1/auth/status", insecure=insecure)
        result: Dict[str, Any] = {"url": base_url, **status}
        if status.get("authenticated"):
            projs = _eye_request(base_url, token, "GET", "/api/v1/projects", insecure=insecure)
            result["project_count"] = len(projs.get("projects") or [])
        if output_format != "text":
            common._print_success(args, result)
            return 0
        print(f"Luma Eye ({base_url})")
        print(f"  Authenticated: {result.get('authenticated')} (mode={result.get('auth_mode') or 'local'}, method={result.get('auth_method') or '-'})")
        if "project_count" in result:
            print(f"  Projects:      {result['project_count']}")
        return 0

    if cmd == "projects":
        sub = args.projects_command
        if sub == "list":
            data = _eye_request(base_url, token, "GET", "/api/v1/projects", insecure=insecure)
            if output_format != "text":
                common._print_success(args, data)
                return 0
            rows = [
                [str(p.get("slug") or ""), str(p.get("name") or ""), str(p.get("public_key") or ""), ",".join(p.get("allowed_origins") or [])]
                for p in (data.get("projects") or [])
            ]
            common._print_table(["SLUG", "NAME", "PUBLIC KEY", "ALLOWED ORIGINS"], rows)
            return 0
        if sub == "ensure":
            origins = [o.strip() for o in str(args.origins or "").split(",") if o.strip()]
            data = _eye_request(base_url, token, "POST", "/api/v1/projects", body={
                "slug": args.slug,
                "name": args.name or args.slug,
                "allowed_origins": origins,
                "rate_limit_rps": args.rps,
            }, insecure=insecure)
            common._print_success(args, data) if output_format != "text" else print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if sub == "delete":
            data = _eye_request(base_url, token, "DELETE", f"/api/v1/projects/{urllib.parse.quote(args.slug, safe='')}", insecure=insecure)
            common._print_success(args, data) if output_format != "text" else print(f"[ok] Deleted project {args.slug}")
            return 0

    if cmd == "portfolio":
        qs = urllib.parse.urlencode({"since": args.since})
        data = _eye_request(base_url, token, "GET", f"/api/v1/query/portfolio?{qs}", insecure=insecure)
        if output_format != "text":
            common._print_success(args, data)
            return 0
        rows = []
        for item in data.get("projects") or []:
            proj = item.get("project") or {}
            rows.append([
                str(proj.get("slug") or ""),
                str(item.get("unique_visitors") or 0),
                str(item.get("pageviews") or 0),
                str(item.get("error_count") or 0),
                f"{float(item.get('crash_free_rate') or 100):.1f}%",
            ])
        common._print_table(["PROJECT", "UV", "PV", "ERRORS", "CRASH-FREE"], rows)
        return 0

    if cmd == "stats":
        qs = urllib.parse.urlencode({"project": args.project, "since": args.since})
        data = _eye_request(base_url, token, "GET", f"/api/v1/query/overview?{qs}", insecure=insecure)
        common._print_success(args, data) if output_format != "text" else print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if cmd == "issues":
        if args.resolve:
            fp = urllib.parse.quote(args.resolve, safe="")
            data = _eye_request(base_url, token, "POST", f"/api/v1/issues/{fp}/status", body={"project": args.project, "status": "resolved"}, insecure=insecure)
        elif args.detail:
            fp = urllib.parse.quote(args.detail, safe="")
            qs = urllib.parse.urlencode({"project": args.project})
            data = _eye_request(base_url, token, "GET", f"/api/v1/issues/{fp}?{qs}", insecure=insecure)
        else:
            qs = urllib.parse.urlencode({"project": args.project, "since": args.since, "status": args.status})
            data = _eye_request(base_url, token, "GET", f"/api/v1/issues?{qs}", insecure=insecure)
        common._print_success(args, data) if output_format != "text" else print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if cmd == "logs":
        params: Dict[str, Any] = {"project": args.project, "since": args.since, "limit": args.limit}
        for k in ("level", "search", "trace"):
            val = getattr(args, k, "")
            if val:
                params["trace_id" if k == "trace" else k] = val
        qs = urllib.parse.urlencode(params)
        data = _eye_request(base_url, token, "GET", f"/api/v1/logs?{qs}", insecure=insecure)
        common._print_success(args, data) if output_format != "text" else print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    raise LumaError(f"unknown eye command: {cmd}")
