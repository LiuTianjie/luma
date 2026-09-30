"""Command tree for the ``luma`` CLI.

Top-level commands are listed in ``COMMAND_GROUPS``; ``luma --help`` renders
that grouping, and every command and subcommand carries a one-line summary.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import textwrap
from pathlib import Path

from .. import __version__
from ..agent import DEFAULT_AGENT_CONFIG
from ..compose import DEFAULT_NFS_MOUNT_OPTIONS
from ..profiles import PROFILES
from ..service import VALID_EXPOSURES
from .common import OUTPUT_FORMATS

COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Get started", (
        ("init", "Create a service manifest"),
        ("login", "Save a Control endpoint and management token"),
        ("context", "List or switch saved Control logins"),
        ("doctor", "Check this machine, Control and nodes"),
        ("version", "Show CLI and Control versions"),
    )),
    ("Deploy", (
        ("validate", "Check a service manifest without contacting Control"),
        ("deploy", "Deploy a service manifest"),
        ("compose", "Deploy a Docker Compose application with a Luma sidecar"),
        ("import", "Build a Git repository on a builder node and deploy it"),
        ("build", "Build a local checkout, or inspect and retry build runs"),
        ("workflow", "Show or record how an application is built and deployed"),
    )),
    ("Operate", (
        ("status", "Show cluster, node and application health"),
        ("app", "Inspect, restart, roll back or remove deployed applications"),
        ("secret", "Manage application secrets"),
    )),
    ("Cluster administration", (
        ("bootstrap", "Install the Luma manager on this machine"),
        ("update", "Update the CLI, the manager or every node"),
        ("node", "Join, list and remove nodes"),
        ("region", "Manage scheduling regions"),
        ("storage", "Manage NFS storage classes"),
        ("registry", "Manage registry credentials and the managed registry"),
        ("git-provider", "Manage saved GitHub and Gitea credentials"),
        ("manager", "Repair manager networking, DNS and IP changes"),
    )),
)
SUMMARIES = {name: summary for _group, commands in COMMAND_GROUPS for name, summary in commands}


class LumaHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Wrap prose paragraphs; keep indented blocks such as examples as written."""

    def __init__(self, prog: str, width: int | None = None) -> None:
        super().__init__(prog, max_help_position=30, width=width)

    def add_argument(self, action: argparse.Action) -> None:
        super().add_argument(action)
        # Older argparse releases measure subcommand names two columns shallower
        # than they print them; newer ones do not. Take the printed width either way.
        if isinstance(action, argparse._SubParsersAction):
            widest = max((len(self._format_action_invocation(sub)) for sub in action._get_subactions()), default=0)
            self._action_max_length = max(self._action_max_length, self._current_indent + 2 + widest)

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        paragraphs = []
        for block in text.split("\n\n"):
            if "\n" in block or block.startswith(" "):
                paragraphs.append(super()._fill_text(block, width, indent))
            else:
                paragraphs.append(textwrap.fill(block, width, initial_indent=indent, subsequent_indent=indent))
        return "\n\n".join(paragraphs)


class LumaArgumentParser(argparse.ArgumentParser):
    """ArgumentParser with "did you mean" hints for mistyped commands."""

    def error(self, message: str) -> None:  # type: ignore[override]
        match = re.search(r"invalid choice: '([^']*)' \(choose from (.*)\)", message)
        if match:
            choices = re.findall(r"'([^']*)'", match.group(2))
            if self.prog == "luma":
                choices = list(SUMMARIES)
            close = difflib.get_close_matches(match.group(1), choices, n=1)
            message = f"unknown command '{match.group(1)}'"
            if close:
                message += f"; did you mean '{close[0]}'?"
            message += f"\nRun '{self.prog} --help' to see the available commands."
            self.exit(2, f"{self.prog}: error: {message}\n")
        super().error(message)


def _top_level_description() -> str:
    width = max(len(name) for name in SUMMARIES) + 2
    lines = ["Deploy containers to your own servers through Luma Control.", ""]
    for group, commands in COMMAND_GROUPS:
        lines.append(f"{group}:")
        lines.extend(f"  {name:<{width}}{summary}" for name, summary in commands)
        lines.append("")
    return "\n".join(lines).rstrip()


def _command(sub: argparse._SubParsersAction, name: str, summary: str = "", **kwargs) -> argparse.ArgumentParser:
    summary = summary or SUMMARIES[name]
    kwargs.setdefault("description", summary)
    return sub.add_parser(name, help=summary, formatter_class=LumaHelpFormatter, **kwargs)


def _commands(parser: argparse.ArgumentParser, dest: str) -> argparse._SubParsersAction:
    return parser.add_subparsers(dest=dest, required=True, metavar="<command>", title="commands")


def build_parser() -> argparse.ArgumentParser:
    parser = LumaArgumentParser(
        prog="luma",
        usage="luma <command> [options]",
        description=_top_level_description(),
        epilog="Run 'luma <command> --help' for the options of a command.\nDocumentation: https://liutianjie.github.io/luma/",
        formatter_class=LumaHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"luma {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="Cluster config file, used on the manager (default: ./luma.yaml if present)")
    parser.add_argument("--env-file", type=Path, default=None, help="Load environment variables from this file (default: Luma settings from ./.env)")
    parser.add_argument("--no-env", action="store_true", help="Do not load ./.env or saved local settings")
    sub = parser.add_subparsers(dest="command", metavar="<command>", help=argparse.SUPPRESS, prog="luma")

    _add_get_started_commands(sub)
    _add_deploy_commands(sub)
    _add_operate_commands(sub)
    _add_cluster_commands(sub)

    # Internal entry points used by installed node agents; never listed.
    node_agent = sub.add_parser("node-agent")
    node_agent_sub = node_agent.add_subparsers(dest="node_agent_command", required=True)
    node_agent_run = node_agent_sub.add_parser("run")
    node_agent_run.add_argument("--config", type=Path, default=DEFAULT_AGENT_CONFIG)
    node_agent_run.add_argument("--once", action="store_true")
    node_agent_run.add_argument("--poll-interval", type=int)
    node_agent_terminal = node_agent_sub.add_parser("terminal-supervisor")
    node_agent_terminal.add_argument("--config", type=Path, default=DEFAULT_AGENT_CONFIG)
    return parser


def _add_get_started_commands(sub: argparse._SubParsersAction) -> None:
    init = _command(
        sub, "init",
        description="Create a service manifest. Prompts for missing values in a terminal; "
        "without a terminal every value comes from flags or defaults.",
        epilog="Example: luma init --name web --image nginx:1.27 --region cn --exposure cn-edge --domain web.example.com --port 80",
    )
    init.add_argument("--name", help="Application name (default: app)")
    init.add_argument("--image", help="Container image (default: ghcr.io/your-org/<name>:latest)")
    init.add_argument("--region", help="Where the service runs: cn, global, home or a created region (default: cn)")
    init.add_argument("--exposure", choices=sorted(VALID_EXPOSURES), help="How traffic reaches the service (default: cn-edge)")
    init.add_argument("--domain", help="Public hostname for edge and tunnel exposures")
    init.add_argument("--port", type=int, help="Container port (default: 3000)")
    init.add_argument("--replicas", type=int, help="Number of instances (default: 1)")
    init.add_argument("--output", type=Path, help="Manifest path (default: <name>.yaml)")
    init.add_argument("--force", action="store_true", help="Overwrite an existing manifest")

    login = _command(sub, "login", epilog="Example: luma login https://luma.example.com --token-stdin < token.txt")
    login.add_argument("endpoint", help="Control URL, for example https://luma.example.com")
    login_token = login.add_mutually_exclusive_group()
    login_token.add_argument("--token", help="Management token (prefer --token-stdin or LUMA_DEPLOY_TOKEN)")
    login_token.add_argument("--token-stdin", action="store_true", help="Read the management token from stdin")
    login.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed endpoints")
    login.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    _add_output_arguments(login)

    context = _command(sub, "context")
    context_sub = _commands(context, "context_command")
    context_list = _command(context_sub, "list", "List saved logins")
    _add_output_arguments(context_list)
    context_use = _command(context_sub, "use", "Switch the current login")
    context_use.add_argument("cluster", help="Cluster ID or name shown by 'luma context list'")
    _add_output_arguments(context_use)

    doctor = _command(sub, "doctor")
    doctor.add_argument("--local", action="store_true", help="Only inspect this machine's installation; do not contact Control")
    doctor.add_argument("--deep", action="store_true", help="Also run slower live checks on every node")
    _add_control_arguments(doctor)
    _add_output_arguments(doctor)

    version = _command(sub, "version")
    version.add_argument("--local", action="store_true", help="Only print the local CLI version")
    version.add_argument("--control-url", help="Control URL to check instead of the current login")
    version.add_argument("--insecure", action="store_true", help="Skip TLS verification for the Control check")
    version.add_argument("--resolve-ip", help="Connect to this IP while keeping the Control hostname as Host")


def _add_deploy_commands(sub: argparse._SubParsersAction) -> None:
    validate = _command(sub, "validate", epilog="Use 'luma deploy FILE --dry-run' to see the rendered Nomad job.")
    validate.add_argument("service", type=Path, metavar="FILE", help="Service manifest")
    _add_output_arguments(validate)

    deploy = _command(
        sub, "deploy",
        description="Deploy a service manifest through Luma Control. The image must already exist; "
        "use 'luma build local' or 'luma import' to build one.",
        epilog="Examples:\n  luma deploy app.yaml --dry-run\n  luma deploy app.yaml --env .env",
    )
    deploy.add_argument("service", type=Path, metavar="FILE", help="Service manifest")
    deploy.add_argument("--dry-run", action="store_true", help="Render the Nomad job locally and print it; nothing is deployed")
    deploy.add_argument("--env", dest="deploy_env_file", type=Path, help="Store the variables the manifest references from this .env file as application secrets")
    deploy.add_argument("--timeout", type=int, default=3000, help="Seconds to wait for Control (default: 3000)")
    deploy.add_argument("--skip-dns", action="store_true", help="Do not create or update Cloudflare DNS records")
    deploy.add_argument("--skip-orchestrator", action="store_true", help="Update routes and DNS without submitting the Nomad job")
    _add_legacy_engine_argument(deploy)
    _add_workflow_arguments(deploy)
    _add_control_arguments(deploy)
    _add_output_arguments(deploy)

    compose = _command(sub, "compose")
    compose_sub = _commands(compose, "compose_command")
    compose_init = _command(compose_sub, "init", "Create a Luma sidecar for a docker-compose.yml")
    compose_init.add_argument("--compose", type=Path, default=Path("docker-compose.yml"), help="Compose file (default: docker-compose.yml)")
    compose_init.add_argument("--output", type=Path, default=Path("luma.compose.yml"), help="Sidecar path (default: luma.compose.yml)")
    compose_validate = _command(compose_sub, "validate", "Check a Compose sidecar and the Compose file it references")
    compose_validate.add_argument("sidecar", type=Path, metavar="SIDECAR", help="Luma Compose sidecar, for example luma.compose.yml")
    compose_validate.add_argument("--import-mode", action="store_true", help="Allow services with build: as 'luma import' does")
    _add_control_arguments(compose_validate)
    _add_output_arguments(compose_validate)
    compose_deploy = _command(compose_sub, "deploy", "Deploy a Compose application")
    compose_deploy.add_argument("sidecar", type=Path, metavar="SIDECAR", help="Luma Compose sidecar, for example luma.compose.yml")
    compose_deploy.add_argument("--dry-run", action="store_true", help="Render the Nomad job and print it; nothing is deployed")
    compose_deploy.add_argument("--env", dest="deploy_env_file", type=Path, help="Store referenced variables from this .env file as application secrets")
    compose_deploy.add_argument("--timeout", type=int, default=3000, help="Seconds to wait for Control (default: 3000)")
    compose_deploy.add_argument("--skip-dns", action="store_true", help="Do not create or update Cloudflare DNS records")
    compose_deploy.add_argument("--skip-orchestrator", action="store_true", help="Update routes and DNS without submitting the Nomad job")
    _add_legacy_engine_argument(compose_deploy)
    _add_workflow_arguments(compose_deploy)
    _add_control_arguments(compose_deploy)
    _add_output_arguments(compose_deploy)

    import_cmd = _command(
        sub, "import",
        description=(
            "Build and deploy a Git repository on a builder node. Luma looks for a .luma.yml "
            "service manifest or a luma.compose.yml sidecar; Compose services with build: are "
            "built, pushed to the internal registry and deployed."
        ),
        epilog="Examples:\n  luma import acme/web\n  luma import https://gitea.example.com/acme/web.git --ref v1.2.0",
    )
    import_cmd.add_argument("repo", nargs="?", default="", help="Repository URL or owner/name (GitHub); omit with --provider-id and --repository")
    import_cmd.add_argument("--ref", default="", help="Branch or tag to build (default: the repository's default branch)")
    import_cmd.add_argument("--provider-id", default="", help="Saved Git provider credential, for example github:personal")
    import_cmd.add_argument("--repository", default="", help="Repository full name for --provider-id, for example owner/name")
    import_cmd.add_argument("--manifest", type=Path, help="Local manifest to use when the repository has none")
    import_cmd.add_argument("--compose-sidecar", default="", help="Repository-relative Compose sidecar to use instead of auto-discovery")
    import_cmd.add_argument("--region", default="", help="Override the manifest's region")
    import_cmd.add_argument("--exposure", default="", help="Override the manifest's exposure (single service)")
    import_cmd.add_argument("--domain", default="", help="Override the manifest's domain (single service)")
    import_cmd.add_argument("--port", type=int, default=None, help="Override the manifest's container port (single service)")
    import_cmd.add_argument("--env", dest="deploy_env_file", type=Path, help="Store variables from this .env file as application secrets")
    import_cmd.add_argument("--build-node", dest="build_node", default="", help="Builder node to use instead of the default")
    import_cmd.add_argument("--platform", default="", help="Build platform (default: linux/amd64 or the manifest's build.platform)")
    import_cmd.add_argument("--context", dest="build_context", default="", help="Docker build context inside the repository (default: .)")
    import_cmd.add_argument("--dockerfile", default="", help="Dockerfile inside the repository (default: Dockerfile)")
    import_cmd.add_argument("--registry-host", dest="registry_host", default="", help="Registry host that nodes pull from (default: <build-node>:5000)")
    import_cmd.add_argument("--proxy-mode", choices=("auto", "direct"), default="auto", help="Builder network: auto follows the node's region policy, direct disables the proxy")
    import_cmd.add_argument("--timeout", type=int, default=3600, help="Seconds to wait for build and deploy (default: 3600)")
    _add_workflow_arguments(import_cmd)
    _add_control_arguments(import_cmd)
    _add_output_arguments(import_cmd)

    build = _command(sub, "build")
    build_sub = _commands(build, "build_command")
    build_local = _command(
        build_sub, "local", "Build a local checkout with Docker Buildx, push it and deploy it",
        epilog="Example: luma build local . --platform linux/amd64",
    )
    build_local.add_argument("path", nargs="?", type=Path, default=Path("."), help="Project directory (default: .)")
    build_local.add_argument("--compose-sidecar", default="", help="Repository-relative Compose sidecar to use")
    build_local.add_argument("--region", default="", help="Override the manifest's region")
    build_local.add_argument("--exposure", default="", help="Override the manifest's exposure (single service)")
    build_local.add_argument("--domain", default="", help="Override the manifest's domain (single service)")
    build_local.add_argument("--port", type=int, default=None, help="Override the manifest's container port (single service)")
    build_local.add_argument("--platform", default="", help="Build platform, for example linux/amd64")
    build_local.add_argument("--builder", default="", help="Existing local Docker Buildx builder")
    build_local.add_argument("--proxy", default="", help="HTTP proxy for base image pulls and RUN steps")
    build_local.add_argument("--context", dest="build_context", default="", help="Docker build context inside the project")
    build_local.add_argument("--dockerfile", default="", help="Dockerfile inside the project")
    build_local.add_argument("--repo-url", default="", help="Project Git URL used to name the image (default: the origin remote)")
    build_local.add_argument("--env", dest="deploy_env_file", type=Path, help="Store variables from this .env file as application secrets")
    build_local.add_argument("--timeout", type=int, default=7200, help="Seconds allowed for build and deploy (default: 7200)")
    _add_workflow_arguments(build_local)
    _add_control_arguments(build_local)
    _add_output_arguments(build_local)
    build_list = _command(build_sub, "list", "List recent build runs")
    _add_history_arguments(build_list, include_app=True)
    _add_control_arguments(build_list)
    _add_output_arguments(build_list)
    build_logs = _command(build_sub, "logs", "Show the step log of a build run")
    build_logs.add_argument("id", help="Build run ID from 'luma build list'")
    _add_pagination_arguments(build_logs)
    _add_control_arguments(build_logs)
    _add_output_arguments(build_logs)
    build_retry = _command(build_sub, "retry", "Run a recorded build again")
    build_retry.add_argument("id", help="Build run ID from 'luma build list'")
    build_retry.add_argument("--env", dest="deploy_env_file", type=Path, help="Store variables from this .env file as application secrets")
    build_retry.add_argument("--timeout", type=int, default=3600, help="Seconds to wait for build and deploy (default: 3600)")
    _add_workflow_arguments(build_retry)
    _add_control_arguments(build_retry)
    _add_output_arguments(build_retry)
    build_cancel = _command(build_sub, "cancel", "Cancel a running build")
    build_cancel.add_argument("id", help="Build run ID from 'luma build list'")
    _add_control_arguments(build_cancel)
    _add_output_arguments(build_cancel)
    build_config = _command(build_sub, "config", "Show or set builder nodes and the internal registry")
    build_config.add_argument("--node", action="append", dest="nodes", default=[], help="Builder node; repeat for several")
    build_config.add_argument("--default-node", default="", help="Builder used by 'luma import' by default")
    build_config.add_argument("--registry-host", default="", help="Registry host that nodes pull from, for example 10.0.0.5:5000")
    build_config.add_argument("--push-host", default="", help="Registry host that builds push to (the builder's mesh address, not localhost)")
    build_config.add_argument("--direct-egress-node", action="append", dest="direct_egress_nodes", default=None, help="Builder with direct internet access; repeat for several")
    build_config.add_argument("--clear-direct-egress", action="store_true", help="Clear the direct-egress builder list")
    _add_control_arguments(build_config)
    _add_output_arguments(build_config)

    workflow = _command(
        sub, "workflow",
        description="Show or record how an application is built and deployed. Deploy, import and "
        "build commands compare themselves with the recorded workflow and stop on changes.",
    )
    workflow_sub = _commands(workflow, "workflow_command")
    workflow_list = _command(workflow_sub, "list", "List recorded workflows")
    workflow_show = _command(workflow_sub, "show", "Show an application's recorded command and notes")
    workflow_show.add_argument("name", help="Application name")
    workflow_record = _command(
        workflow_sub, "record", "Record a workflow without deploying",
        epilog="Example: luma workflow record web -- luma build local . --platform linux/amd64",
    )
    workflow_record.add_argument("name", help="Application name")
    workflow_record.add_argument("--note", dest="workflow_note", default=None, help="Why this workflow is used; never include secrets")
    workflow_record.add_argument("recipe_command", nargs="+", metavar="COMMAND", help="The luma command, after --")
    workflow_run = _command(workflow_sub, "run", "Run the recorded command from a local checkout")
    workflow_run.add_argument("name", help="Application name")
    workflow_run.add_argument("--path", type=Path, default=Path("."), help="Local project checkout (default: .)")
    _add_workflow_arguments(workflow_run)
    for workflow_parser in (workflow_list, workflow_show, workflow_record, workflow_run):
        _add_control_arguments(workflow_parser)
        _add_output_arguments(workflow_parser)


def _add_operate_commands(sub: argparse._SubParsersAction) -> None:
    status = _command(sub, "status")
    _add_control_arguments(status)
    _add_output_arguments(status)

    app = _command(sub, "app", epilog="Examples:\n  luma app list\n  luma app logs web --follow\n  luma app rollback web")
    app_sub = _commands(app, "app_command")
    app_list = _command(app_sub, "list", "List deployed services and their replica health")
    app_list.add_argument("--region", help="Only this region")
    app_list.add_argument("--app", dest="stack", help="Only this application")
    app_show = _command(app_sub, "show", "Show an application or one of its services")
    app_show.add_argument("name", help="Application or full service name")
    app_logs = _command(app_sub, "logs", "Read application logs")
    app_logs.add_argument("name", help="Full service name from 'luma app list'")
    app_logs.add_argument("--follow", "-f", action="store_true", help="Keep streaming new lines until interrupted")
    app_logs.add_argument("--tail", type=int, default=120, help="Recent lines to read across all sources, 1-500 (default: 120)")
    app_logs.add_argument("--previous", action="store_true", help="Read stopped allocations instead of running ones")
    app_logs.add_argument("--allocation", default="", help="Only this allocation ID")
    app_events = _command(app_sub, "events", "Show recent runtime events of the latest allocation")
    app_events.add_argument("name", help="Full service name from 'luma app list'")
    app_history = _command(app_sub, "history", "Page through build and deployment attempts")
    app_history.add_argument("name", nargs="?", default="", help="Only this application")
    app_history.add_argument("--kind", choices=("build", "deployment"), default="", help="Only this record type, or the type of --id")
    app_history.add_argument("--id", dest="record_id", help="Show one record and its step log; requires --kind")
    _add_history_arguments(app_history)
    app_versions = _command(app_sub, "versions", "List the Nomad job versions that 'luma app rollback' can restore")
    app_versions.add_argument("name", help="Application name")
    for operation in (app_list, app_show, app_logs, app_events, app_history, app_versions):
        _add_control_arguments(operation)
        _add_output_arguments(operation)
    app_rollback = _command(
        app_sub, "rollback", "Restore a previous Nomad job version",
        description="Restore a previous Nomad job version. This does not restore application data.",
    )
    app_rollback.add_argument("name", help="Application name")
    app_rollback.add_argument("--to-version", type=int, default=None, help="Version from 'luma app versions' (default: the previous one)")
    _add_control_arguments(app_rollback)
    _add_output_arguments(app_rollback)
    app_restart = _command(app_sub, "restart", "Restart an application")
    app_restart.add_argument("stack", metavar="name", help="Application name")
    app_restart.add_argument("--service", default="", help="Only this service of a Compose application")
    app_restart.add_argument("--mode", choices=("recreate", "task"), default="", help="recreate replaces the allocation; task restarts in place")
    app_restart.add_argument("--timeout", type=int, default=120, help="Seconds to wait for Control (default: 120)")
    _add_control_arguments(app_restart)
    _add_output_arguments(app_restart)
    app_remove = _command(app_sub, "remove", "Remove an application, its routes and DNS records")
    app_remove.add_argument("service", metavar="name", help="Application name")
    app_remove.add_argument("--dry-run", action="store_true", help="Show what would be removed")
    app_remove.add_argument("--delete-storage", action="store_true", help="Also delete removable storage recorded for the deployment")
    app_remove.add_argument("--skip-dns", action="store_true", help="Keep Cloudflare DNS records")
    app_remove.add_argument("--skip-orchestrator", action="store_true", help="Keep the Nomad job running")
    app_remove.add_argument("--timeout", type=int, default=300, help="Seconds to wait for Control (default: 300)")
    _add_control_arguments(app_remove)
    _add_output_arguments(app_remove)

    secret = _command(
        sub, "secret",
        description="Manage application secrets. Manifests reference them as ${NAME}; the scope is the application name.",
    )
    secret_sub = _commands(secret, "secret_command")
    secret_list = _command(secret_sub, "list", "List secret names")
    _add_control_arguments(secret_list)
    _add_output_arguments(secret_list)
    secret_set = _command(secret_sub, "set", "Set a secret; prompts for the value when neither --value nor --value-stdin is given")
    secret_set.add_argument("name", help="Variable name, for example DATABASE_URL")
    secret_set.add_argument("--scope", default="", help="Application name the secret belongs to")
    secret_set.add_argument("--value", help="Secret value (visible in shell history; prefer the prompt or --value-stdin)")
    secret_set.add_argument("--value-stdin", action="store_true", help="Read the value from stdin")
    _add_control_arguments(secret_set)
    secret_import = _command(secret_sub, "import", "Import every variable of a .env file into an application scope")
    secret_import.add_argument("secrets_file", type=Path, metavar="ENV_FILE", help=".env file to import")
    secret_import.add_argument("--scope", required=True, help="Application name the secrets belong to")
    _add_control_arguments(secret_import)
    secret_remove = _command(secret_sub, "remove", "Remove a secret")
    secret_remove.add_argument("name", help="Variable name")
    secret_remove.add_argument("--scope", default="", help="Application name the secret belongs to")
    _add_control_arguments(secret_remove)


def _add_cluster_commands(sub: argparse._SubParsersAction) -> None:
    bootstrap = _command(
        sub, "bootstrap",
        description=(
            "Install the Luma manager on this Linux server: Docker, Nomad, Traefik, Luma Control and, "
            "when EGRESS_SUBSCRIPTION_URL is set, the egress proxy. Prompts for missing settings. "
            "Safe to rerun to repair a layer."
        ),
        epilog="Example: luma bootstrap --domain luma.example.com",
    )
    bootstrap.add_argument("--domain", required=True, help="Hostname for the Control API and dashboard")
    bootstrap.add_argument("--node", help="Manager node name from the cluster config (default: this host)")
    bootstrap.add_argument("--profile", choices=sorted(PROFILES), default="single-node", help="Roles installed on this host (default: single-node)")
    bootstrap.add_argument("--http-port", type=int, help="Public HTTP port for Traefik (default: 80)")
    bootstrap.add_argument("--https-port", type=int, help="Public HTTPS port for Traefik (default: 443)")
    bootstrap.add_argument("--skip-egress", action="store_true", help="Do not install the egress proxy")
    bootstrap.add_argument("--overwrite-control-state", action="store_true", help="Create new Control state and tokens instead of reusing the existing ones")

    update = _command(
        sub, "update",
        description=(
            "Update the local CLI. On a manager this also refreshes Luma Control, and on a joined "
            "node it refreshes the node agent."
        ),
        epilog="Examples:\n  luma update\n  luma update --install-ref v0.2.0\n  luma update fleet",
    )
    _add_update_arguments(update)
    _add_control_arguments(update)
    update_sub = update.add_subparsers(dest="update_command", required=False, metavar="[target]", title="targets")
    update_manager = _command(update_sub, "manager", "Force a manager control-plane refresh")
    _add_update_arguments(update_manager)
    _add_control_arguments(update_manager)
    update_fleet = _command(update_sub, "fleet", "Update Luma on every registered node with a ready agent")
    update_fleet.add_argument("--install-ref", dest="fleet_install_ref", help="Git ref installed on every node")
    update_fleet.add_argument("--all", action="store_true", help="Also list offline nodes as skipped")
    update_fleet.add_argument("--include-manager", action="store_true", help="Also update manager nodes")
    update_fleet.add_argument("--timeout", type=int, default=900, help="Per-node timeout in seconds (default: 900)")
    _add_control_arguments(update_fleet)
    _add_output_arguments(update_fleet)

    node = _command(sub, "node")
    node_sub = _commands(node, "node_command")
    node_join = _command(
        node_sub, "join", "Join this machine to the cluster",
        epilog="Example: luma node join https://luma.example.com --token <node-join-token> --region global --name sg-1",
    )
    node_join.add_argument("endpoint", help="Control URL")
    node_join.add_argument("--token", required=True, help="Node join token printed by 'luma bootstrap'")
    node_join.add_argument("--region", help="cn, global, home or a region from 'luma region create'")
    node_join.add_argument("--name", default=os.uname().nodename, help="Node name used by manifests' node field (default: hostname)")
    node_join.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed endpoints")
    node_join.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    node_list = _command(node_sub, "list", "List registered nodes")
    _add_control_arguments(node_list)
    _add_output_arguments(node_list)
    node_status = _command(node_sub, "status", "Show node readiness, agent and resource details")
    node_status.add_argument("name", nargs="?", help="Only this node (name, hostname or alias)")
    _add_control_arguments(node_status)
    _add_output_arguments(node_status)
    node_remove = _command(node_sub, "remove", "Unregister a node from Control")
    node_remove.add_argument("name", help="Node name")
    _add_control_arguments(node_remove)
    node_exit = _command(node_sub, "exit", "Stop Nomad on this machine and remove its Luma state")
    node_exit.add_argument("--endpoint", help="Control URL; also unregister this node")
    node_exit.add_argument("--token", help="Management or node join token used with --endpoint")
    node_exit.add_argument("--name", help="Node name to unregister (default: this node's registered name)")
    node_exit.add_argument("--tailscale", action="store_true", help="Also log out of Tailscale")
    node_exit.add_argument("--prune-docker", action="store_true", help="Also prune unused Docker containers, networks, images and volumes")
    node_exit.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed endpoints")
    node_exit.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    _command(node_sub, "tailscale", "Install Tailscale on this machine and join the tailnet (needs TAILSCALE_AUTHKEY)")
    node_nomad_join = _command(node_sub, "nomad-join", "Ask a ready node's agent to reinstall Nomad and rejoin")
    node_nomad_join.add_argument("name", help="Node name")
    node_nomad_join.add_argument("--region", help="Override the node's registered region")
    node_nomad_join.add_argument("--server-addr", help="Nomad RPC address (default: the manager's join address)")
    node_nomad_join.add_argument("--timeout", type=int, default=1200, help="Seconds to wait (default: 1200)")
    _add_control_arguments(node_nomad_join)
    _add_output_arguments(node_nomad_join)

    region = _command(sub, "region")
    region_sub = _commands(region, "region_command")
    region_list = _command(region_sub, "list", "List built-in and custom regions")
    _add_control_arguments(region_list)
    _add_output_arguments(region_list)
    region_create = _command(region_sub, "create", "Create a custom region")
    region_create.add_argument("name", help="Region name")
    region_create.add_argument("--egress", choices=("proxy", "direct"), default="proxy", help="Joins and image pulls use the manager proxy or go direct (default: proxy)")
    _add_control_arguments(region_create)
    _add_output_arguments(region_create)
    region_remove = _command(region_sub, "remove", "Remove an unused custom region")
    region_remove.add_argument("name", help="Region name")
    _add_control_arguments(region_remove)
    _add_output_arguments(region_remove)

    storage = _command(sub, "storage")
    storage_sub = _commands(storage, "storage_command")
    storage_list = _command(storage_sub, "list", "List storage classes")
    _add_control_arguments(storage_list)
    _add_output_arguments(storage_list)
    storage_set = _command(storage_sub, "set", "Create or update an NFS storage class")
    storage_set.add_argument("name", help="Storage class name")
    storage_set.add_argument("--provider", choices=("nfs",), default="nfs", help="Storage provider (default: nfs)")
    storage_set.add_argument("--node", default="", help="Node that serves a managed NFS export")
    storage_set.add_argument("--path", default="", help="Export path on the NFS server")
    storage_set.add_argument("--external", action="store_true", help="Use an existing NFS server instead of preparing one")
    storage_set.add_argument("--endpoint", default="", help="Address of an external NFS server")
    storage_set.add_argument("--mount-options", default="", help=f"NFS mount options (default: {DEFAULT_NFS_MOUNT_OPTIONS})")
    storage_set.add_argument("--region", action="append", dest="regions", default=[], help="Region allowed to mount it; repeat for several")
    storage_set.add_argument("--eligible-node", action="append", dest="nodes", default=[], help="Node allowed to mount it; repeat for several")
    storage_set.add_argument("--timeout", type=int, default=360, help="Seconds to wait for host preparation (default: 360)")
    _add_control_arguments(storage_set)
    storage_remove = _command(storage_sub, "remove", "Remove a storage class")
    storage_remove.add_argument("name", help="Storage class name")
    storage_remove.add_argument("--timeout", type=int, default=360, help="Seconds to wait for host cleanup (default: 360)")
    _add_control_arguments(storage_remove)
    storage_apply = _command(storage_sub, "apply", "Prepare the volumes a Compose sidecar needs")
    storage_apply.add_argument("sidecar", type=Path, metavar="SIDECAR", help="Luma Compose sidecar")
    storage_apply.add_argument("--dry-run", action="store_true", help="Show the plan without preparing anything")
    storage_apply.add_argument("--timeout", type=int, default=300, help="Seconds to wait (default: 300)")
    _add_control_arguments(storage_apply)
    storage_check = _command(storage_sub, "check", "Check that a sidecar's volumes can be mounted where it runs")
    storage_check.add_argument("sidecar", type=Path, metavar="SIDECAR", help="Luma Compose sidecar")
    _add_control_arguments(storage_check)
    _add_output_arguments(storage_check)
    storage_migrate = _command(storage_sub, "migrate", "Print a manual plan for moving a volume's data")
    storage_migrate.add_argument("sidecar", type=Path, metavar="SIDECAR", help="Luma Compose sidecar")
    storage_migrate.add_argument("--volume", required=True, help="Volume in the sidecar")
    storage_migrate.add_argument("--from-node", required=True, help="Node that holds the current data")
    storage_migrate.add_argument("--from-volume", required=True, help="Current Docker volume or path")
    _add_control_arguments(storage_migrate)
    _add_output_arguments(storage_migrate)

    registry = _command(sub, "registry")
    registry_sub = _commands(registry, "registry_command")
    registry_list = _command(registry_sub, "list", "List saved registry credentials")
    _add_control_arguments(registry_list)
    _add_output_arguments(registry_list)
    registry_login = _command(registry_sub, "login", "Save credentials for pulling private images")
    registry_login.add_argument("host", help="Registry host, for example ghcr.io")
    registry_login.add_argument("--username", required=True, help="Registry user name")
    registry_login.add_argument("--password-stdin", action="store_true", help="Read the password or token from stdin (otherwise prompt)")
    _add_control_arguments(registry_login)
    registry_remove = _command(registry_sub, "remove", "Remove saved registry credentials")
    registry_remove.add_argument("host", help="Registry host")
    _add_control_arguments(registry_remove)
    registry_serve = _command(registry_sub, "serve", "Deploy the managed registry on a Linux node")
    registry_serve.add_argument("--node", required=True, help="Ready Linux node that hosts the registry")
    registry_serve.add_argument("--port", type=int, default=5000, help="Host port (default: 5000)")
    registry_serve.add_argument("--domain", default="", help="TLS hostname; avoids reconfiguring Docker on every node")
    registry_serve.add_argument("--username", default="", help="Basic Auth user for --domain")
    registry_serve.add_argument("--password-stdin", action="store_true", help="Read the --domain password from stdin")
    registry_serve.add_argument("--storage-class", dest="storage_class", default="", help="Storage class for images (default: a node-local volume)")
    registry_serve.add_argument("--image", default="", help="Registry image (default: registry:2)")
    registry_serve.add_argument("--name", default="", help="Service name (default: luma-registry)")
    registry_serve.add_argument("--no-activate", action="store_true", help="Do not make it the builders' push and pull registry")
    registry_serve.add_argument("--timeout", type=int, default=1800, help="Seconds to wait (default: 1800)")
    _add_control_arguments(registry_serve)
    _add_output_arguments(registry_serve)
    registry_images = _command(registry_sub, "images", "List images in the managed registry and their protection state")
    registry_images.add_argument("--refresh", action="store_true", help="Rescan the registry instead of using the cache")
    _add_control_arguments(registry_images)
    _add_output_arguments(registry_images)
    registry_delete = _command(registry_sub, "delete", "Queue a manifest for deletion after a protection check")
    registry_delete.add_argument("repository", help="Repository, for example acme/web")
    registry_delete.add_argument("digest", help="Manifest digest (sha256:...)")
    registry_delete.add_argument("--execute-now", action="store_true", help="Skip the grace period after a fresh protection check")
    _add_control_arguments(registry_delete)
    _add_output_arguments(registry_delete)
    registry_deletion = _command(registry_sub, "deletion", "Cancel, execute or restore a queued deletion")
    registry_deletion.add_argument("id", help="Deletion ID")
    registry_deletion.add_argument("action", choices=("cancel", "execute", "restore"), help="What to do")
    registry_deletion.add_argument("--force", action="store_true", help="Proceed despite warnings")
    _add_control_arguments(registry_deletion)
    _add_output_arguments(registry_deletion)
    registry_gc = _command(registry_sub, "gc", "Preview or run registry garbage collection (irreversible)")
    registry_gc.add_argument("--execute", action="store_true", help="Run it instead of previewing")
    registry_gc.add_argument("--force", action="store_true", help="Proceed despite warnings")
    _add_control_arguments(registry_gc)
    _add_output_arguments(registry_gc)
    registry_policy = _command(registry_sub, "policy", "Show or change the retention policy")
    registry_policy.add_argument("--mode", choices=("off", "recommend", "enforce"), help="off, recommend deletions, or enforce them")
    registry_policy.add_argument("--keep-last", type=int, help="Tags to keep per repository")
    registry_policy.add_argument("--max-age-days", type=int, help="Delete older unprotected images")
    registry_policy.add_argument("--system-keep-last", type=int, help="Tags to keep for Luma system images")
    registry_policy.add_argument("--queue-grace-hours", type=int, help="Hours before queued deletions run")
    registry_policy.add_argument("--gc-grace-days", type=int, help="Days before garbage collection runs")
    registry_policy.add_argument("--warning-percent", type=int, help="Disk usage that raises a warning")
    registry_policy.add_argument("--critical-percent", type=int, help="Disk usage that raises a critical alert")
    registry_policy.add_argument("--emergency-percent", type=int, help="Disk usage that triggers emergency cleanup")
    _add_control_arguments(registry_policy)
    _add_output_arguments(registry_policy)

    git_provider = _command(sub, "git-provider")
    git_provider_sub = _commands(git_provider, "git_provider_command")
    git_provider_list = _command(git_provider_sub, "list", "List saved provider credentials")
    _add_control_arguments(git_provider_list)
    _add_output_arguments(git_provider_list)
    git_provider_set = _command(git_provider_sub, "set", "Save a GitHub or Gitea access token")
    git_provider_set.add_argument("type", choices=("github", "gitea"), help="Provider type")
    git_provider_set.add_argument("account", help="Label, for example personal or work")
    git_provider_set.add_argument("--token-stdin", action="store_true", help="Read the token from stdin")
    git_provider_set.add_argument("--git-token", dest="provider_token", default="", help="Token (visible in shell history; prefer --token-stdin)")
    git_provider_set.add_argument("--username", default="", help="Git user name for HTTPS clones")
    git_provider_set.add_argument("--base-url", default="", help="API base URL; required for Gitea")
    git_provider_set.add_argument("--clone-base-url", default="", help="Clone URL base (default: derived from the base URL)")
    _add_control_arguments(git_provider_set)
    git_provider_remove = _command(git_provider_sub, "remove", "Remove a saved credential")
    git_provider_remove.add_argument("id", help="Credential ID, for example github:personal")
    _add_control_arguments(git_provider_remove)
    git_provider_repos = _command(git_provider_sub, "repos", "List repositories a credential can read")
    git_provider_repos.add_argument("id", help="Credential ID")
    _add_control_arguments(git_provider_repos)
    _add_output_arguments(git_provider_repos)
    git_provider_refs = _command(git_provider_sub, "refs", "List branches and tags of a repository")
    git_provider_refs.add_argument("id", help="Credential ID")
    git_provider_refs.add_argument("repository", help="Repository full name, for example owner/name")
    _add_control_arguments(git_provider_refs)
    _add_output_arguments(git_provider_refs)

    manager = _command(sub, "manager", description="Repair the manager. Run these on the manager itself.")
    manager_sub = _commands(manager, "manager_command")
    _command(manager_sub, "egress", "Install or refresh the egress proxy (needs EGRESS_SUBSCRIPTION_URL)")
    manager_cloudflare = _command(manager_sub, "cloudflare", "Point the cluster config at a Cloudflare zone")
    manager_cloudflare.add_argument("--zone", required=True, help="Zone name, for example example.com")
    manager_ip = _command(manager_sub, "ip-change", "Recover Control after the manager's public IPv4 address changes")
    manager_ip.add_argument("--old", dest="old_ip", required=True, help="Previous public IPv4 address")
    manager_ip.add_argument("--new", dest="new_ip", required=True, help="New public IPv4 address")
    manager_ip.add_argument("--domain", required=True, help="Control hostname, without scheme")
    manager_ip.add_argument("--dry-run", action="store_true", help="Show the recovery plan without changing anything")


def _add_legacy_engine_argument(parser: argparse.ArgumentParser) -> None:
    # Hidden: workflow recipes recorded by older CLIs may carry --engine nomad.
    parser.add_argument("--engine", choices=("nomad",), help=argparse.SUPPRESS)


def _add_pagination_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=50, help="Records per page, 1-100 (default: 50)")
    parser.add_argument("--cursor", default="", help="nextCursor from the previous page with the same filters")


def _add_history_arguments(parser: argparse.ArgumentParser, *, include_app: bool = False) -> None:
    _add_pagination_arguments(parser)
    if include_app:
        parser.add_argument("--app", default="", help="Only this application")
    parser.add_argument("--status", default="", help="Only this recorded status")
    parser.add_argument("--source", choices=("build", "cli", "dashboard"), default="", help="Only records from this source")
    parser.add_argument("--since", default="", help="Created at or after (Unix seconds or RFC 3339)")
    parser.add_argument("--until", default="", help="Created at or before (Unix seconds or RFC 3339)")


def _add_workflow_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("workflow options")
    group.add_argument("--workflow-app", default="", help="Application whose workflow to check, when a repository has several")
    group.add_argument("--accept-workflow-change", action="store_true", help="Proceed after reviewing the reported workflow differences")
    group.add_argument("--workflow-note", default=None, help="Record why this workflow is used; never include secrets")


def _add_control_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("connection options")
    group.add_argument("--control-context", help="Use this saved login without switching to it")
    group.add_argument("--control-url", help="Control URL to use instead of the current login")
    group.add_argument("--token", help="Management token for --control-url (or set LUMA_DEPLOY_TOKEN)")
    group.add_argument("--insecure", action="store_true", help="Skip TLS verification for Control")
    group.add_argument("--resolve-ip", help="Connect to this IP while keeping the Control hostname as Host")


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=OUTPUT_FORMATS, default="text", help="Output format (default: text)")
    parser.add_argument("--quiet", action="store_true", help="Only print the final result or error")


def _add_update_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--install-ref", help="Git tag, branch or commit to install (default: the latest release)")
    parser.add_argument("--domain", help="Control domain, when it changed (default: the domain in Control state)")
    parser.add_argument(
        "--detach",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Run a manager update in the background and log progress locally",
    )
    # The manager refresh shares code with bootstrap; these are fixed for updates.
    parser.set_defaults(node=None, profile="single-node", http_port=None, https_port=None,
                        skip_egress=False, overwrite_control_state=False)
