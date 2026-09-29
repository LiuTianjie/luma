"""Luma CLI: parser commands."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from ..compose import (
    DEFAULT_NFS_MOUNT_OPTIONS,
)
from ..agent import DEFAULT_AGENT_CONFIG
from ..profiles import PROFILES
from .common import OUTPUT_FORMATS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="luma", description="Self-hosted deployment control plane.")
    parser.add_argument("--config", type=Path, default=None, help="Path to luma.yaml")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Path to local env file")
    parser.add_argument("--no-env", action="store_true", help="Do not load .env")
    visible_commands = (
        "init,version,status,preflight,configure,login,context,secret,registry,git-provider,"
        "bootstrap,update,doctor,manager,node,cloudflare,egress,tailscale,"
        "service,validate,render,dns-sync,deploy,import,build,workflow,rollback,history,compose,storage,region"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{" + visible_commands + "}")

    sub.add_parser("init")
    version = sub.add_parser("version")
    version.add_argument("--control-url", help="Control API URL to check instead of the current login context")
    version.add_argument("--insecure", action="store_true", help="Skip TLS verification for the control API check")
    version.add_argument("--resolve-ip", help="Connect to this IP while keeping the control hostname as Host")
    version.add_argument("--local", action="store_true", help="Only print the local CLI version")
    status = sub.add_parser("status")
    _add_control_arguments(status)
    _add_output_arguments(status)
    sub.add_parser("preflight")
    configure = sub.add_parser("configure")
    configure.add_argument("--role", choices=("manager", "worker", "client"), default="manager")
    configure.add_argument("--show", action="store_true", help="Show configured key names without printing secret values")
    login = sub.add_parser("login")
    login.add_argument("endpoint")
    login_token = login.add_mutually_exclusive_group()
    login_token.add_argument("--token", help="Management token (prefer --token-stdin or LUMA_DEPLOY_TOKEN)")
    login_token.add_argument("--token-stdin", action="store_true", help="Read management token from stdin")
    _add_output_arguments(login)
    login.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed control endpoints")
    login.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    context = sub.add_parser("context")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_list = context_sub.add_parser("list")
    _add_output_arguments(context_list)
    context_use = context_sub.add_parser("use")
    context_use.add_argument("cluster")
    _add_output_arguments(context_use)
    secret = sub.add_parser("secret")
    secret_sub = secret.add_subparsers(dest="secret_command", required=True)
    secret_list = secret_sub.add_parser("list")
    _add_control_arguments(secret_list)
    _add_output_arguments(secret_list)
    secret_set = secret_sub.add_parser("set")
    secret_set.add_argument("name")
    secret_set.add_argument("--scope", default="", help="Application/stack scope; omit only for legacy global secrets")
    secret_set.add_argument("--value")
    secret_set.add_argument("--value-stdin", action="store_true", help="Read the secret value from stdin")
    _add_control_arguments(secret_set)
    secret_import = secret_sub.add_parser("import", help="Import deployment secrets from a .env file into an application scope")
    secret_import.add_argument("env_file", type=Path)
    secret_import.add_argument("--scope", required=True, help="Application/stack scope used to isolate common names like DATABASE_URL")
    _add_control_arguments(secret_import)
    registry = sub.add_parser("registry")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    registry_list = registry_sub.add_parser("list")
    _add_control_arguments(registry_list)
    _add_output_arguments(registry_list)
    registry_login = registry_sub.add_parser("login")
    registry_login.add_argument("host")
    registry_login.add_argument("--username", required=True)
    registry_login.add_argument("--password-stdin", action="store_true", help="Read the registry password/token from stdin")
    _add_control_arguments(registry_login)
    registry_remove = registry_sub.add_parser("remove")
    registry_remove.add_argument("host")
    _add_control_arguments(registry_remove)
    registry_serve = registry_sub.add_parser("serve", help="Deploy a managed registry on a Linux Luma node")
    registry_serve.add_argument("--node", required=True, help="Ready Linux node that hosts the registry")
    registry_serve.add_argument("--port", type=int, default=5000, help="Host port the registry listens on (default: 5000)")
    registry_serve.add_argument("--image", default="", help="Registry image (default: registry:2)")
    registry_serve.add_argument("--name", default="", help="Service name (default: luma-registry)")
    registry_serve.add_argument("--storage-class", dest="storage_class", default="", help="Optional storageClass; otherwise use a node-local Docker volume")
    registry_serve.add_argument("--domain", default="", help="TLS hostname for a secure registry; avoids Docker daemon restarts")
    registry_serve.add_argument("--username", default="", help="Basic Auth username for --domain")
    registry_serve.add_argument("--password-stdin", action="store_true", help="Read the secure registry password from stdin")
    registry_serve.add_argument("--no-activate", action="store_true", help="Do not make the new secure registry the Builder push/pull registry")
    _add_control_arguments(registry_serve)
    _add_output_arguments(registry_serve)
    registry_serve.add_argument("--timeout", type=int, default=1800)
    registry_images = registry_sub.add_parser("images", help="List managed Registry manifests and protection state")
    registry_images.add_argument("--refresh", action="store_true")
    _add_control_arguments(registry_images)
    _add_output_arguments(registry_images)
    registry_delete = registry_sub.add_parser("delete", help="Queue a protected manifest deletion")
    registry_delete.add_argument("repository")
    registry_delete.add_argument("digest")
    registry_delete.add_argument("--execute-now", action="store_true", help="Bypass the queue grace period after a fresh protection check")
    _add_control_arguments(registry_delete)
    _add_output_arguments(registry_delete)
    registry_deletion = registry_sub.add_parser("deletion", help="Cancel, execute, or restore a deletion")
    registry_deletion.add_argument("id")
    registry_deletion.add_argument("action", choices=("cancel", "execute", "restore"))
    registry_deletion.add_argument("--force", action="store_true")
    _add_control_arguments(registry_deletion)
    _add_output_arguments(registry_deletion)
    registry_gc = registry_sub.add_parser("gc", help="Preview or execute offline Registry garbage collection")
    registry_gc.add_argument("--execute", action="store_true")
    registry_gc.add_argument("--force", action="store_true")
    _add_control_arguments(registry_gc)
    _add_output_arguments(registry_gc)
    registry_policy = registry_sub.add_parser("policy", help="Show or update Registry retention policy")
    registry_policy.add_argument("--mode", choices=("off", "recommend", "enforce"))
    registry_policy.add_argument("--keep-last", type=int)
    registry_policy.add_argument("--max-age-days", type=int)
    registry_policy.add_argument("--system-keep-last", type=int)
    registry_policy.add_argument("--queue-grace-hours", type=int)
    registry_policy.add_argument("--gc-grace-days", type=int)
    registry_policy.add_argument("--warning-percent", type=int)
    registry_policy.add_argument("--critical-percent", type=int)
    registry_policy.add_argument("--emergency-percent", type=int)
    _add_control_arguments(registry_policy)
    _add_output_arguments(registry_policy)
    git_provider = sub.add_parser("git-provider")
    git_provider_sub = git_provider.add_subparsers(dest="git_provider_command", required=True)
    git_provider_list = git_provider_sub.add_parser("list")
    _add_control_arguments(git_provider_list)
    _add_output_arguments(git_provider_list)
    git_provider_set = git_provider_sub.add_parser("set")
    git_provider_set.add_argument("type", choices=("github", "gitea"))
    git_provider_set.add_argument("account", help="Account label, for example personal or work")
    git_provider_set.add_argument("--base-url", default="", help="API base URL; required for Gitea, defaults to GitHub API for github")
    git_provider_set.add_argument("--clone-base-url", default="", help="Clone URL base; defaults to GitHub/Gitea base URL")
    git_provider_set.add_argument("--username", default="", help="Git username for HTTPS token clone")
    git_provider_set.add_argument("--git-token", dest="provider_token", default="", help="Provider PAT/token; prefer --token-stdin")
    git_provider_set.add_argument("--token-stdin", action="store_true", help="Read provider PAT/token from stdin")
    _add_control_arguments(git_provider_set)
    git_provider_remove = git_provider_sub.add_parser("remove")
    git_provider_remove.add_argument("id", help="Provider credential id, for example github:personal")
    _add_control_arguments(git_provider_remove)
    git_provider_repos = git_provider_sub.add_parser("repos", help="List repositories visible to a saved provider credential")
    git_provider_repos.add_argument("id")
    _add_control_arguments(git_provider_repos)
    _add_output_arguments(git_provider_repos)
    git_provider_refs = git_provider_sub.add_parser("refs", help="List branches and tags for a repository")
    git_provider_refs.add_argument("id")
    git_provider_refs.add_argument("repository", help="Repository full name, for example owner/name")
    _add_control_arguments(git_provider_refs)
    _add_output_arguments(git_provider_refs)
    bootstrap = sub.add_parser("bootstrap")
    bootstrap_sub = bootstrap.add_subparsers(dest="bootstrap_command", required=True)
    manager = bootstrap_sub.add_parser("manager")
    manager.add_argument("--domain", required=True)
    manager.add_argument("--node")
    manager.add_argument("--profile", choices=sorted(PROFILES), default="single-node")
    manager.add_argument("--http-port", type=int, help="Public Traefik HTTP port")
    manager.add_argument("--https-port", type=int, help="Public Traefik HTTPS port")
    manager.add_argument("--skip-egress", action="store_true")
    manager.add_argument("--overwrite-control-state", action="store_true")
    update = sub.add_parser(
        "update",
        description=(
            "Update the local CLI. With no target, Luma hot-refreshes manager control only "
            "when local manager state exists; "
            "clients and workers update CLI only."
        ),
        epilog="Examples: luma update | luma update --install-ref v0.1.366 | luma update manager --domain luma.example.com",
    )
    _add_update_manager_arguments(update)
    _add_control_arguments(update)
    update_sub = update.add_subparsers(dest="update_command", required=False, metavar="[target]")
    update_manager = update_sub.add_parser("manager", help="force a manager control-plane refresh")
    _add_update_manager_arguments(update_manager)
    _add_control_arguments(update_manager)
    update_fleet = update_sub.add_parser("fleet", help="update Luma on registered non-manager nodes with ready agents")
    update_fleet.add_argument("--install-ref", dest="fleet_install_ref", help="Git ref passed to the installer on every node")
    update_fleet.add_argument("--all", action="store_true", help="Include offline nodes in the report as skipped")
    update_fleet.add_argument("--include-manager", action="store_true", help="Also update manager nodes through fleet tasks")
    update_fleet.add_argument("--timeout", type=int, default=900, help="Per-node update timeout in seconds")
    _add_control_arguments(update_fleet)
    _add_output_arguments(update_fleet)
    doctor = sub.add_parser("doctor", help="Check authentication and remote Control/node readiness")
    _add_control_arguments(doctor)
    _add_output_arguments(doctor)
    doctor.add_argument("--local", action="store_true", help="Inspect local installation identity and dependency policy without contacting Control or changing state")
    doctor.add_argument("--deep", action="store_true", help="Run slower live checks")

    manager_ops = sub.add_parser("manager", help="Manager recovery and maintenance operations")
    manager_ops_sub = manager_ops.add_subparsers(dest="manager_command", required=True)
    manager_ip = manager_ops_sub.add_parser(
        "ip-change", help="Recover the manager control plane after its public IPv4 address changes"
    )
    manager_ip.add_argument(
        "--old", dest="old_ip", required=True, help="Previous manager public IPv4 address"
    )
    manager_ip.add_argument(
        "--new", dest="new_ip", required=True, help="New manager public IPv4 address"
    )
    manager_ip.add_argument("--domain", required=True, help="Control-plane hostname, without scheme")
    manager_ip.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and show the recovery plan without changing anything",
    )

    node = sub.add_parser("node")
    node_sub = node.add_subparsers(dest="node_command", required=True)
    node_sub.add_parser("list")
    node_bootstrap = node_sub.add_parser("bootstrap")
    node_bootstrap.add_argument("node")
    node_bootstrap.add_argument("--profile", choices=sorted(PROFILES), required=True)
    node_bootstrap.add_argument("--skip-egress", action="store_true", help="Skip egress setup during bootstrap; run luma egress setup later")
    node_join = node_sub.add_parser("join")
    node_join.add_argument("endpoint")
    node_join.add_argument("--token", required=True)
    node_join.add_argument("--region", help="Built-in region (cn, global, home) or a region created with luma region create")
    node_join.add_argument("--name", default=os.uname().nodename)
    node_join.add_argument("--engine", choices=("nomad",), default="nomad", metavar="{nomad}", help="Orchestrator to join; Nomad is the only supported engine")
    node_join.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed control endpoints")
    node_join.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    node_exit = node_sub.add_parser("exit")
    node_exit.add_argument("--endpoint", help="Control endpoint; when set, unregister this node from Luma Control")
    node_exit.add_argument("--token", help="Management token or node join token used with --endpoint")
    node_exit.add_argument("--name", help="Luma node name to unregister; defaults to this node's registered label or Docker name")
    node_exit.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed control endpoints")
    node_exit.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    node_exit.add_argument("--tailscale", action="store_true", help="Also log out Tailscale on this node")
    node_exit.add_argument("--prune-docker", action="store_true", help="Also prune unused Docker containers, networks, images, and volumes")
    node_remove = node_sub.add_parser("remove")
    node_remove.add_argument("name")
    node_remove.add_argument("--control-url", help="Control API URL to use instead of the current login context")
    node_remove.add_argument("--token", help="Management token to use with --control-url")
    node_remove.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed control endpoints")
    node_remove.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    node_status = node_sub.add_parser("status")
    node_status.add_argument("name", nargs="?", help="Optional Luma node name, display name, hostname, or alias to show")
    _add_control_arguments(node_status)
    _add_output_arguments(node_status)
    node_nomad_join = node_sub.add_parser("nomad-join", help="ask a ready node agent to install and join Nomad on that node")
    node_nomad_join.add_argument("name")
    node_nomad_join.add_argument("--region", help="Override the node's registered region")
    node_nomad_join.add_argument("--server-addr", help="Nomad RPC address to join; defaults to the control-plane join address")
    node_nomad_join.add_argument("--timeout", type=int, default=1200, help="Join timeout in seconds")
    _add_control_arguments(node_nomad_join)
    _add_output_arguments(node_nomad_join)
    region_cmd = sub.add_parser("region", help="Create and list scheduling regions")
    region_sub = region_cmd.add_subparsers(dest="region_command", required=True)
    region_list = region_sub.add_parser("list", help="List built-in and custom regions")
    _add_control_arguments(region_list)
    _add_output_arguments(region_list)
    region_create = region_sub.add_parser("create", help="Create a custom scheduling region")
    region_create.add_argument("name")
    region_create.add_argument("--egress", choices=("proxy", "direct"), default="proxy", help="Join/image-pull egress: proxy uses the manager gateway, direct does not")
    _add_control_arguments(region_create)
    _add_output_arguments(region_create)
    region_remove = region_sub.add_parser("remove", help="Remove an unused custom region")
    region_remove.add_argument("name")
    _add_control_arguments(region_remove)
    _add_output_arguments(region_remove)

    node_agent = sub.add_parser("node-agent", help=argparse.SUPPRESS)
    node_agent_sub = node_agent.add_subparsers(dest="node_agent_command", required=True)
    node_agent_run = node_agent_sub.add_parser("run", help=argparse.SUPPRESS)
    node_agent_run.add_argument("--config", type=Path, default=DEFAULT_AGENT_CONFIG)
    node_agent_run.add_argument("--once", action="store_true")
    node_agent_run.add_argument("--poll-interval", type=int)
    node_agent_terminal = node_agent_sub.add_parser("terminal-supervisor", help=argparse.SUPPRESS)
    node_agent_terminal.add_argument("--config", type=Path, default=DEFAULT_AGENT_CONFIG)
    sub._choices_actions = [action for action in sub._choices_actions if action.dest != "node-agent"]

    cf = sub.add_parser("cloudflare")
    cf_sub = cf.add_subparsers(dest="cloudflare_command", required=True)
    cf_connect = cf_sub.add_parser("connect")
    cf_connect.add_argument("--zone", required=True)

    egress = sub.add_parser("egress")
    egress_sub = egress.add_subparsers(dest="egress_command", required=True)
    for name in ("setup", "refresh"):
        egress_sub.add_parser(name)

    tailscale = sub.add_parser("tailscale")
    tailscale_sub = tailscale.add_subparsers(dest="tailscale_command", required=True)
    tailscale_sub.add_parser("connect")

    service = sub.add_parser("service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_new = service_sub.add_parser("new")
    service_new.add_argument("--output", type=Path)
    service_list = service_sub.add_parser("list", help="List deployed services and replica health")
    service_list.add_argument("--region", help="Filter by scheduling region")
    service_list.add_argument("--stack", help="Filter by application/stack")
    service_inspect = service_sub.add_parser("inspect", help="Inspect an application or exact deployed service")
    service_inspect.add_argument("name", help="Application/stack or full service name")
    service_events = service_sub.add_parser("events", help="Show recent runtime events for the latest task allocation")
    service_events.add_argument("name", help="Deployed service full name")
    service_history = service_sub.add_parser("history", help="Page build and deployment attempts (Nomad versions remain under luma history)")
    service_history.add_argument("name", nargs="?", default="", help="Filter by application name")
    service_history.add_argument("--id", dest="record_id", help="Read a history record and its step log; requires --kind")
    service_history.add_argument("--kind", choices=("build", "deployment"), default="", help="Filter record type, or identify --id type")
    _add_history_arguments(service_history)
    service_logs = service_sub.add_parser("logs", help="Read application logs")
    service_logs.add_argument("name", help="Deployed service full name")
    service_logs.add_argument("--tail", type=int, default=120, help="Recent line budget shared across all selected sources (1-500)")
    service_logs.add_argument("--previous", action="store_true", help="Read stopped allocations instead of running allocations")
    service_logs.add_argument("--allocation", default="", help="Only this allocation ID")
    service_logs.add_argument("--follow", "-f", action="store_true", help="Follow logs until interrupted; use text or ndjson")
    for operation in (service_list, service_inspect, service_events, service_logs, service_history):
        _add_control_arguments(operation)
        _add_output_arguments(operation)
    service_remove = service_sub.add_parser("remove")
    _add_control_arguments(service_remove)
    _add_output_arguments(service_remove)
    service_remove.add_argument("service", help="Deployed service or Compose application name")
    service_remove.add_argument("--skip-dns", action="store_true", help="Keep Cloudflare DNS records")
    service_remove.add_argument("--skip-orchestrator", action="store_true", help="Keep the Nomad job running")
    service_remove.add_argument("--delete-storage", action="store_true", help="Delete removable storage referenced by the recorded deployment")
    service_remove.add_argument("--dry-run", action="store_true", help="Show what would be removed without changing the manager")
    service_remove.add_argument("--timeout", type=int, default=300, help="Seconds to wait for the control-plane remove response")
    service_restart = service_sub.add_parser("restart")
    _add_control_arguments(service_restart)
    _add_output_arguments(service_restart)
    service_restart.add_argument("stack", help="Deployed service or Compose application name")
    service_restart.add_argument("--service", default="", help="Task/service name inside a Compose application")
    service_restart.add_argument("--mode", choices=("recreate", "task"), default="", help="recreate stops the allocation; task restarts in place")
    service_restart.add_argument("--timeout", type=int, default=120, help="Seconds to wait for the control-plane restart response")

    validate = sub.add_parser("validate")
    validate.add_argument("service", type=Path)
    validate.add_argument("--engine", choices=("nomad",), metavar="{nomad}", help="Orchestrator to validate for; Nomad is the only supported engine")
    _add_output_arguments(validate)
    render = sub.add_parser("render")
    render.add_argument("service", type=Path)
    render.add_argument("--engine", choices=("nomad",), metavar="{nomad}", help="Orchestrator to render for; Nomad is the only supported engine")

    dns = sub.add_parser("dns-sync")
    dns.add_argument("service", type=Path)

    deploy = sub.add_parser("deploy")
    _add_workflow_arguments(deploy)
    deploy.add_argument("service", type=Path)
    _add_control_arguments(deploy)
    _add_output_arguments(deploy)
    deploy.add_argument("--dry-run", action="store_true")
    deploy.add_argument("--skip-dns", action="store_true")
    deploy.add_argument("--skip-orchestrator", action="store_true")
    deploy.add_argument("--env", dest="deploy_env_file", type=Path, help="Use this .env file as scoped deployment secrets for this service")
    deploy.add_argument("--secrets-env-file", dest="deploy_env_file", type=Path, help=argparse.SUPPRESS)
    deploy.add_argument("--timeout", type=int, default=3000, help="Seconds to wait for the control-plane deploy response")
    deploy.add_argument("--commit", action="store_true", help="Deprecated for control-plane deploy")
    deploy.add_argument("--push", action="store_true", help="Deprecated for control-plane deploy")

    import_cmd = sub.add_parser(
        "import",
        help="Build and deploy a Git repository; auto-discovers .luma.yml or luma.compose.yml",
        description=(
            "Build and deploy from Git. Luma scans for single-service .luma.yml manifests "
            "and Compose sidecars such as luma.compose.yml. For Compose imports, services "
            "with build: are built on the builder node, pushed to the internal registry, "
            "and rewritten to image: before compose deploy."
        ),
    )
    import_cmd.add_argument(
        "repo",
        nargs="?",
        default="",
        help="Git repository URL or owner/name (owner/name expands to https://github.com/owner/name.git); omit when using --provider-id + --repository",
    )
    _add_workflow_arguments(import_cmd)
    import_cmd.add_argument("--provider-id", default="", help="Saved Git provider credential id to use for clone/list-backed imports")
    import_cmd.add_argument("--repository", default="", help="Repository full name for --provider-id, for example owner/name")
    import_cmd.add_argument("--build-node", dest="build_node", default="", help="Override the declared builder node used to clone and build the image")
    import_cmd.add_argument("--ref", default="", help="Git branch or tag to build (default: repository default branch)")
    import_cmd.add_argument("--region", default="", help="Override region from the repo's service manifest or Compose sidecar")
    import_cmd.add_argument("--exposure", default="", help="Override exposure from the repo's .luma.yml for single-service imports")
    import_cmd.add_argument("--domain", default="", help="Override domain from the repo's .luma.yml for single-service imports")
    import_cmd.add_argument("--port", type=int, default=None, help="Override container port from the repo's .luma.yml for single-service imports")
    import_cmd.add_argument("--manifest", type=Path, help="Use this Luma manifest when the repository does not contain one")
    import_cmd.add_argument(
        "--compose-sidecar",
        default="",
        help="Select one repository-relative Luma Compose sidecar instead of auto-discovery",
    )
    import_cmd.add_argument("--env", dest="deploy_env_file", type=Path, help="Import this .env file as scoped deployment secrets for the imported app/stack")
    import_cmd.add_argument("--secrets-env-file", dest="deploy_env_file", type=Path, help=argparse.SUPPRESS)
    import_cmd.add_argument("--platform", default="", help="Build platform (default: linux/amd64 or the repo's build.platform)")
    import_cmd.add_argument("--context", dest="build_context", default="", help="Docker build context within the repo (default: .)")
    import_cmd.add_argument("--dockerfile", default="", help="Dockerfile path within the repo (default: Dockerfile)")
    import_cmd.add_argument("--registry-host", dest="registry_host", default="", help="Registry host other nodes pull from (default: <build-node>:5000)")
    import_cmd.add_argument(
        "--proxy-mode",
        choices=("auto", "direct"),
        default="auto",
        help="Builder outbound network mode: auto uses the node/region policy; direct explicitly disables the build proxy",
    )
    _add_control_arguments(import_cmd)
    _add_output_arguments(import_cmd)
    import_cmd.add_argument("--timeout", type=int, default=3600, help="Seconds to wait for the build+deploy response")

    build = sub.add_parser("build", help="Inspect and retry repository import build runs")
    build_sub = build.add_subparsers(dest="build_command", required=True)
    build_list = build_sub.add_parser("list", help="List recent repository import build runs")
    _add_control_arguments(build_list)
    _add_output_arguments(build_list)
    _add_history_arguments(build_list, include_app=True)
    build_logs = build_sub.add_parser("logs", help="Show a build run's recorded step log")
    build_logs.add_argument("id")
    _add_pagination_arguments(build_logs)
    _add_control_arguments(build_logs)
    _add_output_arguments(build_logs)
    build_retry = build_sub.add_parser("retry", help="Retry a recorded build run")
    _add_workflow_arguments(build_retry)
    build_retry.add_argument("id")
    _add_control_arguments(build_retry)
    _add_output_arguments(build_retry)
    build_retry.add_argument("--timeout", type=int, default=3600)
    build_retry.add_argument("--env", dest="deploy_env_file", type=Path, help="Use this .env file as scoped deployment secrets for the retried import")
    build_cancel = build_sub.add_parser("cancel", help="Cancel an active repository import build")
    build_cancel.add_argument("id")
    _add_control_arguments(build_cancel)
    _add_output_arguments(build_cancel)
    build_local = build_sub.add_parser(
        "local",
        help="Build a local checkout, push it to the project's Luma registry path, and deploy it",
    )
    build_local.add_argument("path", nargs="?", type=Path, default=Path("."))
    _add_workflow_arguments(build_local)
    build_local.add_argument("--repo-url", default="", help="Project Git URL (default: local origin remote)")
    build_local.add_argument("--compose-sidecar", default="", help="Select one repository-relative Luma Compose sidecar")
    build_local.add_argument("--region", default="", help="Override deployment region")
    build_local.add_argument("--exposure", default="", help="Override single-service exposure")
    build_local.add_argument("--domain", default="", help="Override single-service domain")
    build_local.add_argument("--port", type=int, default=None, help="Override single-service container port")
    build_local.add_argument("--platform", default="", help="Build platform override")
    build_local.add_argument("--builder", default="", help="Existing local Docker Buildx builder")
    build_local.add_argument("--proxy", default="", help="HTTP proxy for local image pulls and Dockerfile RUN steps")
    build_local.add_argument("--context", dest="build_context", default="", help="Docker build context within the local project")
    build_local.add_argument("--dockerfile", default="", help="Dockerfile path within the local project")
    build_local.add_argument("--env", dest="deploy_env_file", type=Path, help="Import this .env file as scoped deployment secrets")
    build_local.add_argument("--timeout", type=int, default=7200, help="Seconds allowed for the local build and deploy")
    _add_control_arguments(build_local)
    _add_output_arguments(build_local)
    build_config = build_sub.add_parser("config", help="Declare builder nodes and internal registry defaults")
    build_config.add_argument("--node", action="append", dest="nodes", default=[], help="Declared builder node; repeat for multiple builders")
    build_config.add_argument("--default-node", default="", help="Default builder node for luma import")
    build_config.add_argument("--registry-host", default="", help="Registry host that target nodes pull from, for example 100.66.177.70:5000")
    build_config.add_argument("--push-host", default="", help="Registry host BuildKit pushes to; use the builder Tailscale endpoint, not localhost:5000")
    build_config.add_argument("--direct-egress-node", action="append", dest="direct_egress_nodes", default=None, help="Builder node with reliable direct internet access; repeat for multiple nodes")
    build_config.add_argument("--clear-direct-egress", action="store_true", help="Clear the direct-egress builder node list")
    _add_control_arguments(build_config)
    _add_output_arguments(build_config)

    workflow = sub.add_parser("workflow", help="Inspect and record server-side build/deploy workflows; deployment commands check them automatically")
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_list = workflow_sub.add_parser("list", help="List recorded application workflows")
    workflow_show = workflow_sub.add_parser("show", help="Show an application's build/deploy command and notes")
    workflow_show.add_argument("name")
    workflow_record = workflow_sub.add_parser("record", help="Explicitly set a workflow without deploying; put the Luma command after --")
    workflow_record.add_argument("name")
    workflow_record.add_argument("--note", dest="workflow_note", default=None)
    workflow_record.add_argument("recipe_command", nargs="+")
    workflow_run = workflow_sub.add_parser("run", help="Run the recorded command from a local checkout using current credentials")
    workflow_run.add_argument("name")
    workflow_run.add_argument("--path", type=Path, default=Path("."), help="Local project checkout (default: current directory)")
    _add_workflow_arguments(workflow_run)
    for workflow_parser in (workflow_list, workflow_show, workflow_record, workflow_run):
        _add_control_arguments(workflow_parser)
        _add_output_arguments(workflow_parser)

    rollback = sub.add_parser("rollback", help="Roll a Nomad-engine service back to a previous version")
    rollback.add_argument("name")
    rollback.add_argument("--to-version", type=int, default=None, help="Target version (default: previous)")
    _add_control_arguments(rollback)
    _add_output_arguments(rollback)

    history = sub.add_parser("history", help="Show a Nomad-engine service's deploy version history")
    history.add_argument("name")
    _add_control_arguments(history)
    _add_output_arguments(history)

    compose = sub.add_parser("compose")
    compose_sub = compose.add_subparsers(dest="compose_command", required=True)
    compose_init = compose_sub.add_parser("init")
    compose_init.add_argument("--compose", type=Path, default=Path("docker-compose.yml"))
    compose_init.add_argument("--output", type=Path, default=Path("luma.compose.yml"))
    compose_validate = compose_sub.add_parser("validate")
    compose_validate.add_argument("sidecar", type=Path)
    compose_validate.add_argument("--engine", choices=("nomad",), metavar="{nomad}", help="Orchestrator to validate for; Nomad is the only supported engine")
    compose_validate.add_argument("--import-mode", action="store_true", help="Allow Compose services with build: for luma import validation")
    _add_control_arguments(compose_validate)
    _add_output_arguments(compose_validate)
    compose_render = compose_sub.add_parser("render")
    compose_render.add_argument("sidecar", type=Path)
    compose_render.add_argument("--engine", choices=("nomad",), metavar="{nomad}", help="Orchestrator to render for; Nomad is the only supported engine")
    _add_control_arguments(compose_render)
    compose_deploy = compose_sub.add_parser("deploy")
    _add_workflow_arguments(compose_deploy)
    compose_deploy.add_argument("sidecar", type=Path)
    compose_deploy.add_argument("--engine", choices=("nomad",), metavar="{nomad}", help="Orchestrator for local dry-run preview; live deploy follows the control-plane config")
    _add_control_arguments(compose_deploy)
    _add_output_arguments(compose_deploy)
    compose_deploy.add_argument("--dry-run", action="store_true")
    compose_deploy.add_argument("--skip-dns", action="store_true")
    compose_deploy.add_argument("--skip-orchestrator", action="store_true")
    compose_deploy.add_argument("--env", dest="deploy_env_file", type=Path, help="Use this .env file as scoped deployment secrets for this Compose application")
    compose_deploy.add_argument("--secrets-env-file", dest="deploy_env_file", type=Path, help=argparse.SUPPRESS)
    compose_deploy.add_argument("--timeout", type=int, default=3000)
    storage = sub.add_parser("storage")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)
    storage_list = storage_sub.add_parser("list")
    _add_control_arguments(storage_list)
    _add_output_arguments(storage_list)
    storage_set = storage_sub.add_parser("set")
    storage_set.add_argument("name")
    storage_set.add_argument("--provider", choices=("nfs",), default="nfs")
    storage_set.add_argument("--external", action="store_true")
    storage_set.add_argument("--node", default="")
    storage_set.add_argument("--path", default="")
    storage_set.add_argument("--endpoint", default="")
    storage_set.add_argument(
        "--mount-options",
        default="",
        help=f"NFS mount options; defaults to {DEFAULT_NFS_MOUNT_OPTIONS}",
    )
    storage_set.add_argument("--region", action="append", dest="regions", default=[])
    storage_set.add_argument("--eligible-node", action="append", dest="nodes", default=[])
    storage_set.add_argument(
        "--timeout",
        type=int,
        default=360,
        help="Wait for managed storage host preparation (default: 360s)",
    )
    _add_control_arguments(storage_set)
    storage_remove = storage_sub.add_parser("remove")
    storage_remove.add_argument("name")
    storage_remove.add_argument(
        "--timeout",
        type=int,
        default=360,
        help="Wait for managed storage host cleanup (default: 360s)",
    )
    _add_control_arguments(storage_remove)
    storage_apply = storage_sub.add_parser("apply")
    storage_apply.add_argument("sidecar", type=Path)
    _add_control_arguments(storage_apply)
    storage_apply.add_argument("--dry-run", action="store_true")
    storage_apply.add_argument("--timeout", type=int, default=300)
    storage_check = storage_sub.add_parser("check")
    storage_check.add_argument("sidecar", type=Path)
    _add_control_arguments(storage_check)
    _add_output_arguments(storage_check)
    storage_migrate = storage_sub.add_parser("migrate")
    storage_migrate.add_argument("sidecar", type=Path)
    storage_migrate.add_argument("--volume", required=True)
    storage_migrate.add_argument("--from-node", required=True)
    storage_migrate.add_argument("--from-volume", required=True)
    _add_control_arguments(storage_migrate)
    _add_output_arguments(storage_migrate)

    return parser


def _add_pagination_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=50, help="Records per page (1-100, default: 50)")
    parser.add_argument("--cursor", default="", help="Opaque nextCursor from the preceding page with the same filters")


def _add_history_arguments(parser: argparse.ArgumentParser, *, include_app: bool = False) -> None:
    _add_pagination_arguments(parser)
    if include_app:
        parser.add_argument("--app", default="", help="Filter by application name")
    parser.add_argument("--status", default="", help="Filter by exact recorded status")
    parser.add_argument("--source", choices=("build", "cli", "dashboard"), default="", help="Filter by history source")
    parser.add_argument("--since", default="", help="Created at or after Unix seconds or RFC3339 timestamp")
    parser.add_argument("--until", default="", help="Created at or before Unix seconds or RFC3339 timestamp")


def _add_workflow_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workflow-app", default="", help="Application workflow to check (required when a repository matches several applications)")
    parser.add_argument("--accept-workflow-change", action="store_true", help="Continue after the user has explicitly approved the displayed workflow differences")
    parser.add_argument("--workflow-note", default=None, help="Record why this build/deploy workflow is used; do not include secrets")


def _add_control_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--control-context", help="Use a saved cluster context without switching the current context")
    parser.add_argument("--control-url", help="Control API URL to use instead of the current login context")
    parser.add_argument("--token", help="Management token to use with --control-url")
    parser.add_argument("--insecure", action="store_true", help="Skip TLS verification for the control API")
    parser.add_argument("--resolve-ip", help="Connect to this IP while keeping the control hostname as Host")


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=OUTPUT_FORMATS, default="text", help="Output format")
    parser.add_argument("--quiet", action="store_true", help="Print only the final result or error")


def _add_update_manager_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--domain", help="Control domain. Defaults to the domain stored in Manager Control state (/opt/luma/control/control.sqlite3 by default).")
    parser.add_argument("--node")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="single-node")
    parser.add_argument("--http-port", type=int, help="Public Traefik HTTP port")
    parser.add_argument("--https-port", type=int, help="Public Traefik HTTPS port")
    parser.add_argument("--skip-egress", action="store_true")
    parser.add_argument("--overwrite-control-state", action="store_true")
    parser.add_argument("--install-ref", help="Git ref passed to the install script as LUMA_INSTALL_REF")
    parser.add_argument(
        "--detach",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Run a manager update in a detached transaction and write progress to a local log",
    )
