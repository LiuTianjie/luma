"""Portable deployment recipes shared through Luma Control.

Only CLI options are recorded, never env-file contents or login credentials.
Recipes are interpreted by the Luma parser, never a shell.
"""
from __future__ import annotations

import argparse
import hashlib
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .errors import LumaError


METHODS = {
    "remote-build": "Git checkout and build on Luma Builder, then deploy",
    "local-build": "Build local checkout, push to Luma registry, then deploy",
    "image-deploy": "Deploy an existing image from a service manifest",
    "compose-deploy": "Deploy existing images from a Compose sidecar",
}


def method_for(args: argparse.Namespace) -> str:
    if args.command == "import":
        return "remote-build"
    if args.command == "build" and args.build_command == "local":
        return "local-build"
    if args.command == "build" and args.build_command == "retry":
        return "remote-build"
    if args.command == "deploy":
        return "image-deploy"
    if args.command == "compose" and args.compose_command == "deploy":
        return "compose-deploy"
    raise LumaError("workflow supports only import, build local/retry, deploy, and compose deploy")


def project_root(start: Path) -> Path:
    start = start.expanduser().resolve()
    if start.is_file():
        start = start.parent
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return start


def safe_url(value: str) -> str:
    if "://" not in value:
        return value
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def parse_recipe(argv: Any) -> argparse.Namespace:
    if not isinstance(argv, list) or not argv or len(argv) > 128:
        raise LumaError("workflow argv must be a nonempty array of at most 128 arguments")
    if any(not isinstance(v, str) or len(v) > 4096 or "\x00" in v for v in argv):
        raise LumaError("invalid workflow argument")
    # Do not let argparse exit for help or recursively invoke a recipe.
    if any(v in {"-h", "--help"} for v in argv):
        raise LumaError("help is not a deployment workflow")
    from .cli import build_parser

    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        raise LumaError("workflow must contain a valid Luma deployment command") from exc
    method_for(args)
    if any(getattr(args, key, False) for key in ("dry_run", "skip_orchestrator", "commit", "push", "accept_workflow_change", "workflow_app")):
        raise LumaError("workflow cannot include dry-run, skip-orchestrator, commit/push, or workflow confirmation flags")
    return args


def make_recipe(args: argparse.Namespace) -> dict[str, Any]:
    method = method_for(args)
    if args.command == "build" and args.build_command == "retry":
        return retry_recipe(args)
    if any(getattr(args, key, False) for key in ("dry_run", "skip_orchestrator", "commit", "push")):
        raise LumaError("cannot remember a dry-run, skipped deployment, or deprecated commit/push command")
    cwd = Path.cwd().resolve()
    start = args.path if method == "local-build" else cwd
    if method == "image-deploy":
        start = args.service.resolve().parent
    elif method == "compose-deploy":
        start = args.sidecar.resolve().parent
    root = project_root(start)
    # Record repository-relative paths so another checkout can reuse the recipe.
    def local_path(value: Any) -> str:
        path = Path(value).expanduser().resolve()
        try:
            return path.relative_to(root).as_posix() or "."
        except ValueError:
            # Existing CLI invocations may intentionally use an external config.
            # Keep that explicit dependency rather than silently changing it.
            return str(path)

    argv: list[str] = []
    config = args.config or (cwd / "luma.yaml" if (cwd / "luma.yaml").is_file() else None)
    if config:
        argv += ["--config", local_path(config)]
    if args.no_env:
        argv += ["--no-env"]
    else:
        argv += ["--env-file", local_path(args.env_file)]
    if method == "remote-build":
        argv += ["import"] + ([safe_url(args.repo)] if args.repo else [])
    elif method == "local-build":
        argv += ["build", "local", local_path(args.path)]
    elif method == "image-deploy":
        argv += ["deploy", local_path(args.service)]
    else:
        argv += ["compose", "deploy", local_path(args.sidecar)]
    options = {
        "provider_id": "--provider-id", "repository": "--repository",
        "build_node": "--build-node", "ref": "--ref", "region": "--region",
        "exposure": "--exposure", "domain": "--domain", "port": "--port",
        "manifest": "--manifest", "compose_sidecar": "--compose-sidecar",
        "platform": "--platform", "build_context": "--context", "dockerfile": "--dockerfile",
        "registry_host": "--registry-host", "proxy_mode": "--proxy-mode",
        "repo_url": "--repo-url", "builder": "--builder", "proxy": "--proxy",
        "deploy_env_file": "--env", "timeout": "--timeout", "engine": "--engine",
    }
    for key, flag in options.items():
        value = getattr(args, key, None)
        if value is not None and value != "":
            value = local_path(value) if key in {"manifest", "deploy_env_file"} else safe_url(str(value))
            argv += [flag, value]
    if getattr(args, "skip_dns", False):
        argv.append("--skip-dns")
    # Control URL, token, TLS and resolve-IP are supplied by the current client,
    # not embedded in a server-side recipe that could redirect a future client.
    parse_recipe(argv)
    return {"schemaVersion": 1, "method": method, "argv": argv}


def retry_recipe(args: argparse.Namespace) -> dict[str, Any]:
    run = getattr(args, "_workflow_retry_run", None)
    if not isinstance(run, dict) or not isinstance(run.get("request"), dict):
        raise LumaError("cannot check a build retry without its recorded build request")
    request = run["request"]
    argv = ["--no-env"] if args.no_env else ["--env-file", str(args.env_file)]
    argv += ["build", "retry", args.id, "--timeout", str(args.timeout)]
    parameters: dict[str, Any] = {"method": "remote-build"}
    fields = {
        "repoUrl": "repo", "providerId": "provider_id", "repository": "repository",
        "ref": "ref", "buildNode": "build_node", "region": "region", "exposure": "exposure",
        "domain": "domain", "port": "port", "composeSidecar": "compose_sidecar",
        "platform": "platform", "context": "build_context", "dockerfile": "dockerfile",
        "registryHost": "registry_host", "proxyMode": "proxy_mode",
    }
    for source, target in fields.items():
        value = request.get(source)
        if value not in (None, "", False):
            parameters[target] = safe_url(str(value)) if target != "port" else value
    if request.get("manifest"):
        parameters["manifest"] = "sha256:" + hashlib.sha256(str(request["manifest"]).encode()).hexdigest()
    if args.deploy_env_file:
        argv += ["--env", str(args.deploy_env_file)]
        parameters["deploy_env_file"] = str(args.deploy_env_file)
    return {"schemaVersion": 1, "method": "remote-build", "argv": argv, "retryParameters": parameters}


def comparison_fields(recipe: dict[str, Any]) -> dict[str, Any]:
    args = parse_recipe(recipe["argv"])
    keys = (
        "provider_id", "repository", "repo", "repo_url", "ref", "build_node",
        "region", "exposure", "domain", "port", "manifest", "compose_sidecar",
        "platform", "build_context", "dockerfile", "registry_host", "proxy_mode",
        "builder", "proxy", "service", "sidecar", "path", "skip_dns", "engine",
        "deploy_env_file",
    )
    result: dict[str, Any] = {"method": recipe["method"]}
    for key in keys:
        value = getattr(args, key, None)
        if value not in (None, "", False):
            result[key] = str(value) if isinstance(value, Path) else value
    if args.command == "build" and args.build_command == "retry":
        result = dict(recipe["retryParameters"])
    # Canonicalize spellings with the same semantics.
    for key in ("build_context", "dockerfile", "proxy_mode"):
        default = {"build_context": ".", "dockerfile": "Dockerfile", "proxy_mode": "auto"}[key]
        if result.get(key) == default:
            result.pop(key, None)
    return result


def validate_recipe(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise LumaError("unsupported deployment workflow schema")
    args = parse_recipe(value.get("argv"))
    if value.get("method") != method_for(args):
        raise LumaError("workflow method does not match its command")
    if any(getattr(args, key, None) for key in ("token", "control_url", "insecure", "resolve_ip", "workflow_note")):
        raise LumaError("workflow must not contain credentials, control connection options or nested notes")
    # Sanitize all URL-bearing arguments, including credentials in positional URLs.
    result = {
        "schemaVersion": 1,
        "method": value["method"],
        "argv": [safe_url(v) for v in value["argv"]],
    }
    if args.command == "build" and args.build_command == "retry":
        parameters = value.get("retryParameters")
        if not isinstance(parameters, dict) or parameters.get("method") != "remote-build":
            raise LumaError("retry workflow requires its recorded build parameters")
        if len(parameters) > 32 or any(not isinstance(k, str) or len(k) > 100 or not isinstance(v, (str, int)) or len(str(v)) > 4096 for k, v in parameters.items()):
            raise LumaError("invalid retry workflow parameters")
        allowed = {"method", "repo", "provider_id", "repository", "ref", "build_node", "region", "exposure", "domain", "port", "compose_sidecar", "platform", "build_context", "dockerfile", "registry_host", "proxy_mode", "manifest", "deploy_env_file"}
        if parameters.keys() - allowed:
            raise LumaError("unknown retry workflow parameters")
        result["retryParameters"] = {k: safe_url(str(v)) if isinstance(v, str) else v for k, v in parameters.items()}
    return result


def describe_workflow(record: dict[str, Any]) -> str:
    recipe = record["recipe"]
    lines = [
        f"Application: {record['name']}",
        f"Method: {recipe['method']} — {METHODS[recipe['method']]}",
        "Working directory: project root",
        f"Command: {shlex.join(['luma', *recipe['argv']])}",
        f"Recorded: {record.get('updatedAt', '')} ({record.get('source', 'manual')})",
    ]
    if record.get("lastSuccess"):
        lines.append(f"Last recorded success: {record['lastSuccess']['at']}")
    if record.get("note"):
        lines.append(f"Note: {record['note']}")
    lines.append(f"Reuse with: luma workflow run {shlex.quote(record['name'])}")
    return "\n".join(lines)
