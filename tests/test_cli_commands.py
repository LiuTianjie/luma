"""CLI command behaviour against a mocked Control and host."""
import gzip
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import yaml

from luma import __version__
from luma.agent import (
    update_luma_install,
)
from luma.config import LumaConfig
from luma.bootstrap import (
    _last_command_value,
    install_nomad_node,
    setup_tailscale,
)
from luma.control.client import ControlClient
from luma.control.context import load_current_context, save_context
from luma.errors import LumaError
from luma.cli.manager import _node_join_examples
from luma.cli.common import _run_with_wait_heartbeat
from luma.cli import build_parser, main
from luma.cli.nodes import exit_local_node
from luma.userconfig import configured_keys, ensure_interactive_config, load_user_config
from tests.support import _restore_env, _set_env


class CliTests(unittest.TestCase):
    def test_last_command_value_preserves_digest_colon_after_sudo_prompt(self):
        output = '[sudo] password for tao: ["ghcr.io/liutianjie/luma-control@sha256:abc123"]\n'

        self.assertEqual(_last_command_value(output), '["ghcr.io/liutianjie/luma-control@sha256:abc123"]')


    def test_parser_exposes_node_tailscale(self):
        args = build_parser().parse_args(["node", "tailscale"])
        self.assertEqual(args.command, "node")
        self.assertEqual(args.node_command, "tailscale")

    def test_repair_commands_reject_remote_node_argument(self):
        for argv in (["node", "tailscale", "manager-1"], ["portainer", "setup", "manager-1"], ["manager", "egress", "manager-1"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                build_parser().parse_args(argv)
            self.assertEqual(raised.exception.code, 2)




    def test_bootstrap_manager_supports_public_port_overrides(self):
        args = build_parser().parse_args(
            ["bootstrap", "--domain", "luma.example.com", "--http-port", "10080", "--https-port", "10443"]
        )
        self.assertEqual(args.command, "bootstrap")
        self.assertEqual(args.http_port, 10080)
        self.assertEqual(args.https_port, 10443)

    def test_deploy_defaults_to_control_plane(self):
        args = build_parser().parse_args(["deploy", "app.yaml"])
        self.assertEqual(args.command, "deploy")
        self.assertEqual(args.timeout, 3000)

    def test_service_remove_parser_defaults_to_full_cleanup(self):
        args = build_parser().parse_args(["app", "remove", "app.yaml"])
        self.assertEqual(args.command, "app")
        self.assertEqual(args.app_command, "remove")
        self.assertFalse(args.skip_dns)
        self.assertFalse(args.skip_orchestrator)
        self.assertFalse(args.delete_storage)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.timeout, 300)
        args = build_parser().parse_args(["app", "remove", "app", "--delete-storage"])
        self.assertTrue(args.delete_storage)

    def test_compose_and_storage_parsers_accept_planned_commands(self):
        args = build_parser().parse_args(["compose", "deploy", "luma.compose.yml", "--dry-run"])
        self.assertEqual(args.command, "compose")
        self.assertEqual(args.compose_command, "deploy")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.timeout, 3000)
        args = build_parser().parse_args(["compose", "validate", "luma.compose.yml", "--import-mode"])
        self.assertEqual(args.compose_command, "validate")
        self.assertTrue(args.import_mode)
        args = build_parser().parse_args(
            [
                "secret",
                "set",
                "DATABASE_URL",
                "--value",
                "postgres://secret",
                "--control-url",
                "https://luma.example.com",
                "--token",
                "deploy-token",
            ]
        )
        self.assertEqual(args.secret_command, "set")
        self.assertEqual(args.control_url, "https://luma.example.com")

        args = build_parser().parse_args(
            [
                "registry",
                "login",
                "ghcr.io",
                "--username",
                "bot",
                "--password-stdin",
                "--control-url",
                "https://luma.example.com",
                "--token",
                "deploy-token",
            ]
        )
        self.assertEqual(args.registry_command, "login")
        self.assertEqual(args.control_url, "https://luma.example.com")
        args = build_parser().parse_args(["compose", "validate", "luma.compose.yml", "--control-url", "https://luma.example.com", "--token", "deploy-token"])
        self.assertEqual(args.compose_command, "validate")
        self.assertEqual(args.control_url, "https://luma.example.com")
        args = build_parser().parse_args(
            [
                "storage",
                "migrate",
                "luma.compose.yml",
                "--volume",
                "pg-data",
                "--from-node",
                "home",
                "--from-volume",
                "pg-data",
                "--control-url",
                "https://luma.example.com",
                "--token",
                "deploy-token",
            ]
        )
        self.assertEqual(args.command, "storage")
        self.assertEqual(args.storage_command, "migrate")
        self.assertEqual(args.volume, "pg-data")
        self.assertEqual(args.control_url, "https://luma.example.com")
        args = build_parser().parse_args(
            [
                "storage",
                "set",
                "home-nfs",
                "--provider",
                "nfs",
                "--node",
                "home-nas",
                "--path",
                "/srv/luma",
                "--control-url",
                "https://luma.example.com",
                "--token",
                "deploy-token",
            ]
        )
        self.assertEqual(args.storage_command, "set")
        self.assertEqual(args.name, "home-nfs")
        self.assertEqual(args.node, "home-nas")
        self.assertEqual(args.path, "/srv/luma")
        self.assertEqual(args.timeout, 360)
        self.assertFalse(args.external)
        self.assertEqual(args.control_url, "https://luma.example.com")
        args = build_parser().parse_args(
            [
                "storage",
                "set",
                "company-nfs",
                "--external",
                "--endpoint",
                "nfs.example.com:/srv/luma",
                "--region",
                "cn",
            ]
        )
        self.assertTrue(args.external)
        self.assertEqual(args.endpoint, "nfs.example.com:/srv/luma")
        self.assertEqual(args.regions, ["cn"])
        for argv in (
            ["storage", "set", "home-nfs", "--mode", "managed"],
            ["storage", "set", "home-nfs", "--export-root", "/srv/luma"],
            ["storage", "set", "home-nfs", "--provider", "external"],
        ):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(argv)

    def test_repository_import_timeouts_cover_build_and_cold_rollout(self):
        imported = build_parser().parse_args(["import", "owner/repository"])
        retried = build_parser().parse_args(["build", "retry", "build-123"])

        self.assertEqual(imported.timeout, 3600)
        self.assertEqual(retried.timeout, 3600)

    def test_storage_set_command_validates_new_shape_before_control_call(self):
        cases = (
            ["storage", "set", "home-nfs", "--node", "home-nas"],
            ["storage", "set", "home-nfs", "--node", "home-nas", "--path", "/srv/luma", "--endpoint", "home-nas:/srv/luma"],
            ["storage", "set", "company-nfs", "--external", "--endpoint", "nfs.example.com:/srv/luma"],
        )
        for argv in cases:
            with self.subTest(argv=argv), patch("luma.cli.common.ControlClient") as client_cls, patch("builtins.print"):
                code = main(argv)
            self.assertEqual(code, 1)
            client_cls.assert_not_called()

    def test_service_remove_rejects_manifest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text("name: api\nimage: nginx:alpine\nregion: cn\nexposure: none\n", encoding="utf-8")
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                with patch("luma.cli.common.ControlClient") as client_cls, patch("builtins.print"):
                    code = main(["app", "remove", str(service_path), "--timeout", "12", "--dry-run"])
                self.assertEqual(code, 1)
                client_cls.assert_not_called()
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_service_remove_submits_name_to_control_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.remove_service.return_value = {
                    "service": "api",
                    "dryRun": True,
                    "portainer": "Nomad job would be removed: api",
                    "generatedFiles": "Generated files would be removed: /opt/luma/stacks/cn/api",
                    "steps": [
                        {"name": "Remove Nomad job", "status": "ok", "message": "Nomad job would be removed: api"},
                    ],
                }
                with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["app", "remove", "api", "--dry-run"])
                self.assertEqual(code, 0)
                client.remove_service.assert_called_once()
                kwargs = client.remove_service.call_args.kwargs
                self.assertEqual(kwargs["name"], "api")
                self.assertTrue(kwargs["dry_run"])
                self.assertFalse(kwargs["delete_storage"])
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("[start] Submit remove: api", printed_text)
                self.assertIn("[ok] Remove dry run finished: api", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_deploy_prints_progress_and_passes_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.check_workflow.return_value = {"status": "unrecorded", "differences": []}
                client.record_workflow.return_value = {"workflow": {"name": "api"}}
                client.deploy_events.side_effect = LumaError("control API error 404: not found")
                client.deploy.return_value = {
                    "service": "api",
                    "image": {"selected": "nginx:alpine"},
                    "portainer": "Nomad job deployed for api: api",
                    "steps": [
                        {"name": "Sync DNS", "status": "ok", "message": "DNS skipped: service is not public"},
                        {"name": "Deploy Nomad job", "status": "ok", "message": "Nomad job deployed for api: api"},
                    ],
                }
                with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["deploy", str(service_path), "--timeout", "42"])
                self.assertEqual(code, 0)
                client.deploy.assert_called_once()
                self.assertEqual(client.deploy.call_args.kwargs["timeout"], 42)
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("[start] Load deploy context", printed_text)
                self.assertIn("[start] Waiting for control plane response (timeout 42s)", printed_text)
                self.assertIn("[ok] Sync DNS: DNS skipped: service is not public", printed_text)
                self.assertIn("[ok] Deploy Nomad job: Nomad job deployed for api: api", printed_text)
                self.assertIn("[ok] Deploy finished: api", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_deploy_streams_current_control_plane_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.check_workflow.return_value = {"status": "unrecorded", "differences": []}
                client.record_workflow.return_value = {"workflow": {"name": "api"}}
                client.deploy_events.return_value = iter(
                    [
                        {"name": "Resolve image", "status": "start", "message": "started"},
                        {"name": "Resolve image", "status": "ok", "message": "nginx:alpine"},
                        {"status": "done", "result": {"service": "api", "image": {"selected": "nginx:alpine"}}},
                    ]
                )
                with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["deploy", str(service_path)])
                self.assertEqual(code, 0)
                client.deploy_events.assert_called_once()
                client.deploy.assert_not_called()
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("[start] Resolve image: started", printed_text)
                self.assertIn("[ok] Resolve image: nginx:alpine", printed_text)
                self.assertIn("[ok] Deploy finished: api", printed_text)
                self.assertNotIn("Waiting for control plane response", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_compose_deploy_submits_sidecar_and_compose_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            compose_path = root / "docker-compose.yml"
            sidecar_path = root / "luma.compose.yml"
            compose_path.write_text(
                yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}}),
                encoding="utf-8",
            )
            sidecar_path.write_text(
                yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"}),
                encoding="utf-8",
            )
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.check_workflow.return_value = {"status": "unrecorded", "differences": []}
                client.record_workflow.return_value = {"workflow": {"name": "api"}}
                client.deploy_compose_events.side_effect = LumaError("control API error 404: not found")
                client.deploy_compose.return_value = {"deployment": "app-stack", "steps": []}
                with patch("luma.cli.common.ControlClient", return_value=client):
                    code = main(["compose", "deploy", str(sidecar_path), "--timeout", "12"])
                self.assertEqual(code, 0)
                client.deploy_compose.assert_called_once()
                kwargs = client.deploy_compose.call_args.kwargs
                self.assertIn("app-stack", kwargs["manifest"])
                self.assertIn("nginx:alpine", kwargs["compose_content"])
                self.assertEqual(kwargs["timeout"], 12)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_compose_deploy_stream_interrupted_does_not_redeploy(self):
        # Regression: if the event stream emits steps but ends WITHOUT a `done`
        # result (connection dropped mid-deploy), the deploy already ran on the
        # manager. Re-issuing via the non-streaming endpoint would silently
        # deploy a second time. Must raise instead, like the native path.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            compose_path = root / "docker-compose.yml"
            sidecar_path = root / "luma.compose.yml"
            compose_path.write_text(
                yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}}),
                encoding="utf-8",
            )
            sidecar_path.write_text(
                yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"}),
                encoding="utf-8",
            )
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.check_workflow.return_value = {"status": "unrecorded", "differences": []}
                client.record_workflow.return_value = {"workflow": {"name": "api"}}
                # stream yields progress but no {"status": "done", ...} result
                client.deploy_compose_events.return_value = iter(
                    [
                        {"name": "Render compose Nomad job", "status": "start", "message": "started"},
                        {"name": "Render compose Nomad job", "status": "ok", "message": "rendered"},
                    ]
                )
                with patch("luma.cli.common.ControlClient", return_value=client):
                    code = main(["compose", "deploy", str(sidecar_path), "--timeout", "12"])
                # non-zero exit (LumaError surfaced) and NO silent second deploy
                self.assertNotEqual(code, 0)
                client.deploy_compose.assert_not_called()
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_deploy_streams_ndjson_with_env_context_without_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            old_url = _set_env("LUMA_CONTROL_URL", "https://luma.example.com")
            old_token = _set_env("LUMA_DEPLOY_TOKEN", "deploy-token")
            old_insecure = _set_env("LUMA_INSECURE", "")
            old_resolve = _set_env("LUMA_RESOLVE_IP", "")
            try:
                client = Mock()
                client.check_workflow.return_value = {"status": "unrecorded", "differences": []}
                client.record_workflow.return_value = {"workflow": {"name": "api"}}
                client.deploy_events.return_value = iter(
                    [
                        {"name": "Resolve image", "status": "start", "message": "started"},
                        {"name": "Resolve image", "status": "ok", "message": "nginx:alpine"},
                        {"status": "done", "result": {"service": "api", "image": {"selected": "nginx:alpine"}}},
                    ]
                )
                with patch("luma.cli.common.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
                    code = main(["--no-env", "deploy", str(service_path), "--format", "ndjson"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)
                _restore_env("LUMA_CONTROL_URL", old_url)
                _restore_env("LUMA_DEPLOY_TOKEN", old_token)
                _restore_env("LUMA_INSECURE", old_insecure)
                _restore_env("LUMA_RESOLVE_IP", old_resolve)

            self.assertEqual(code, 0)
            client_cls.assert_called_once_with("https://luma.example.com", "deploy-token", insecure=False, resolve_ip=None)
            client.deploy_events.assert_called_once()
            client.deploy.assert_not_called()
            lines = [json.loads(call.args[0]) for call in printed.call_args_list]
            self.assertTrue(all(isinstance(line, dict) for line in lines))
            self.assertEqual(lines[0]["type"], "event")
            self.assertEqual(lines[-1]["type"], "result")
            self.assertTrue(lines[-1]["ok"])
            self.assertEqual(lines[-1]["result"]["service"], "api")

    def test_import_cli_supports_provider_repository_manifest_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            manifest_path = root / "service.luma.yml"
            env_path = root / "service.env"
            manifest_path.write_text(
                "name: api\nregion: cn\nexposure: none\nenv:\n  DATABASE_URL: ${DATABASE_URL}\n",
                encoding="utf-8",
            )
            env_path.write_text("DATABASE_URL=postgres://secret\nUNUSED=value\n", encoding="utf-8")
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.check_workflow.return_value = {"status": "unrecorded", "differences": []}
                client.record_workflow.return_value = {"workflow": {"name": "api"}}
                client.build_deploy_events.side_effect = LumaError("control API error 404: not found")
                client.build_deploy.return_value = {"service": "api", "image": "100.64.0.70:5000/acme/app:abc123", "steps": []}
                with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print"):
                    code = main(
                        [
                            "import",
                            "--provider-id",
                            "gitea:lin",
                            "--repository",
                            "acme/app",
                            "--build-node",
                            "builder",
                            "--proxy-mode",
                            "direct",
                            "--manifest",
                            str(manifest_path),
                            "--env",
                            str(env_path),
                        ]
                    )

                self.assertEqual(code, 0)
                kwargs = client.build_deploy.call_args.kwargs
                self.assertEqual(kwargs["provider_id"], "gitea:lin")
                self.assertEqual(kwargs["repository"], "acme/app")
                self.assertEqual(kwargs["repo_url"], "")
                self.assertEqual(kwargs["proxy_mode"], "direct")
                self.assertIn("DATABASE_URL", kwargs["manifest"])
                self.assertEqual(kwargs["env_secrets"], {"DATABASE_URL": "postgres://secret", "UNUSED": "value"})
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_compose_validate_import_mode_accepts_build_only_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            compose_path = root / "docker-compose.yml"
            sidecar_path = root / "luma.compose.yml"
            compose_path.write_text(
                "services:\n  web:\n    build:\n      context: .\n      dockerfile: Dockerfile\n",
                encoding="utf-8",
            )
            sidecar_path.write_text(
                "name: app-stack\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n",
                encoding="utf-8",
            )
            (root / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                with patch("builtins.print"):
                    code = main(["compose", "validate", str(sidecar_path), "--import-mode"])
                self.assertEqual(code, 0)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_git_provider_cli_set_reads_token_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.set_git_provider.return_value = {"id": "gitea:lin", "saved": True}
                with patch("luma.cli.common.ControlClient", return_value=client), patch("sys.stdin", io.StringIO("gitea-secret\n")), patch("builtins.print"):
                    code = main(
                        [
                            "git-provider",
                            "set",
                            "gitea",
                            "lin",
                            "--base-url",
                            "https://gcode.example.com",
                            "--username",
                            "lin",
                            "--token-stdin",
                        ]
                    )

                self.assertEqual(code, 0)
                client.set_git_provider.assert_called_once_with(
                    provider_type="gitea",
                    account="lin",
                    token="gitea-secret",
                    base_url="https://gcode.example.com",
                    clone_base_url="",
                    username="lin",
                )
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_git_provider_cli_lists_repositories(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.list_git_provider_repositories.return_value = {
                    "repositories": [{"fullName": "acme/app", "defaultBranch": "main", "private": True}]
                }
                with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["git-provider", "repos", "gitea:lin"])

                self.assertEqual(code, 0)
                client.list_git_provider_repositories.assert_called_once_with(provider_id="gitea:lin")
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("acme/app", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_deploy_dry_run_json_does_not_create_control_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            with patch("luma.cli.common.ControlClient") as client_cls, patch("builtins.print") as printed:
                code = main(["deploy", str(service_path), "--dry-run", "--format", "json"])

        self.assertEqual(code, 0)
        client_cls.assert_not_called()
        payload = json.loads(printed.call_args_list[-1].args[0])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["result"]["dryRun"])
        self.assertEqual(payload["result"]["service"]["name"], "api")
        self.assertEqual(payload["result"]["artifacts"][0]["kind"], "job")

    def test_compose_validate_json_reports_degraded_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_config_home = _set_env("LUMA_CONFIG_HOME", str(root / "config-home"))
            config_path = root / "luma.yaml"
            compose_path = root / "docker-compose.yml"
            sidecar_path = root / "luma.compose.yml"
            try:
                config_path.write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose_path.write_text(yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}}), encoding="utf-8")
                sidecar_path.write_text(
                    yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"}),
                    encoding="utf-8",
                )

                with patch("builtins.print") as printed:
                    code = main(
                        [
                            "--no-env",
                            "--config",
                            str(config_path),
                            "compose",
                            "validate",
                            str(sidecar_path),
                            "--format",
                            "json",
                        ]
                    )
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_config_home)

        self.assertEqual(code, 0)
        payload = json.loads(printed.call_args_list[-1].args[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["validationMode"], "degraded")
        self.assertTrue(any("Storage classes" in warning for warning in payload["result"]["warnings"]))
        self.assertTrue(any("Node records" in warning for warning in payload["result"]["warnings"]))

    def test_wait_heartbeat_prints_during_slow_deploy_request(self):
        def slow_action():
            time.sleep(0.03)
            return {"ok": True}

        with patch("builtins.print") as printed:
            result = _run_with_wait_heartbeat(slow_action, timeout=42, interval=0.01)
        self.assertEqual(result, {"ok": True})
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("[wait] Control plane still working", printed_text)
        self.assertIn("timeout 42s", printed_text)

    def test_login_writes_context_without_printing_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            client = Mock()
            client.verify_login.return_value = {"clusterId": "luma-test"}
            try:
                with patch("luma.cli.common.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
                    code = main(
                        [
                            "login",
                            "https://luma.example.com",
                            "--token",
                            "secret-token",
                            "--insecure",
                            "--resolve-ip",
                            "203.0.113.10",
                        ]
                    )
                self.assertEqual(code, 0)
                client_cls.assert_called_once_with(
                    "https://luma.example.com",
                    "secret-token",
                    insecure=True,
                    resolve_ip="203.0.113.10",
                )
                context = load_current_context()
                self.assertEqual(context["clusterId"], "luma-test")
                self.assertEqual(context["endpoint"], "https://luma.example.com")
                self.assertEqual(context["token"], "secret-token")
                self.assertTrue(context["insecure"])
                self.assertEqual(context["resolveIp"], "203.0.113.10")
                printed_text = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
                self.assertNotIn("secret-token", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)


    def test_bootstrap_prompts_for_missing_manager_config_during_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            project_config = Path(tmp) / "luma.yaml"
            project_config.write_text("{}\n", encoding="utf-8")
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_cf = _set_env("CLOUDFLARE_API_TOKEN", "")
            old_target = _set_env("LUMA_DNS_EDGE_TARGET", "")
            old_email = _set_env("TRAEFIK_ACME_EMAIL", "")
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_egress = _set_env("EGRESS_SUBSCRIPTION_URL", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                secret_values = iter(["cf-token", "ts-key", "sub-url", "sudo-pass"])
                def bootstrap_side_effect(_config, _node, _profile, _domain, state, **_kwargs):
                    state["portainerApiUrl"] = "https://203.0.113.10:9443/api"
                    state["portainerAdminUsername"] = "admin"
                    state["portainerAdminPassword"] = "portainer-secret"
                    return []

                input_values = iter(["203.0.113.10", "ops@example.com"])
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)
                ), patch("builtins.input", side_effect=lambda _prompt: next(input_values)), patch(
                    "luma.cli.manager.bootstrap_manager_local", side_effect=bootstrap_side_effect
                ), patch(
                    "luma.cli.manager.find_zone", return_value={"id": "zone-example"}
                ), patch("builtins.print") as printed:
                    code = main(
                        [
                            "--config",
                            str(project_config),
                            "bootstrap",
                            "--domain",
                            "luma.example.com",
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertTrue(config_path.exists())
                self.assertIn("LUMA_DNS_EDGE_TARGET", configured_keys(config_path))
                self.assertIn("EGRESS_SUBSCRIPTION_URL", configured_keys(config_path))
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("CLOUDFLARE_API_TOKEN [required]", printed_text)
                self.assertIn("Zone Read and DNS Edit", printed_text)
                self.assertIn("LUMA_DNS_EDGE_TARGET [optional", printed_text)
                self.assertIn("EGRESS_SUBSCRIPTION_URL [optional", printed_text)
                self.assertNotIn("portainer-secret", printed_text)
                self.assertIn(
                    "cn worker: luma node join https://luma.example.com --token",
                    printed_text,
                )
                self.assertIn("--region global --name global-sg-1", printed_text)
                self.assertIn("--region home --name home-mac-mini", printed_text)
                self.assertNotIn("--egress", printed_text)
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_cf)
                _restore_env("LUMA_DNS_EDGE_TARGET", old_target)
                _restore_env("TRAEFIK_ACME_EMAIL", old_email)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("EGRESS_SUBSCRIPTION_URL", old_egress)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_bootstrap_infers_cloudflare_dns_from_interactive_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "providers": {"portainer": {}},
                        "nodes": {
                            "manager": {
                                "host": "localhost",
                                "publicIp": "203.0.113.10",
                                "region": "cn",
                                "roles": ["swarm-manager", "edge"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            old_cf = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            old_target = _set_env("LUMA_DNS_EDGE_TARGET", "")
            old_email = _set_env("TRAEFIK_ACME_EMAIL", "ops@example.com")
            old_ts = _set_env("TAILSCALE_AUTHKEY", "ts-key")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "sudo-pass")
            try:
                captured = {}

                def bootstrap_side_effect(config, _node, _profile, _domain, state, **_kwargs):
                    captured["config"] = config.raw
                    state["portainerApiUrl"] = "https://203.0.113.10:9443/api"
                    state["portainerAdminUsername"] = "admin"
                    state["portainerAdminPassword"] = "portainer-secret"
                    return []

                def find_zone_side_effect(_config, zone_name):
                    if zone_name == "example.net":
                        return {"id": "zone-itool"}
                    raise LumaError("not found")

                with patch("luma.cli.manager.find_zone", side_effect=find_zone_side_effect), patch(
                    "luma.cli.manager.bootstrap_manager_local", side_effect=bootstrap_side_effect
                ), patch("builtins.print"):
                    code = main(
                        [
                            "--config",
                            str(config_path),
                            "bootstrap",
                            "--domain",
                            "luma.example.net",
                            "--node",
                            "manager",
                            "--skip-egress",
                        ]
                    )
                self.assertEqual(code, 0)
                dns = captured["config"]["providers"]["dns"]
                self.assertEqual(dns["type"], "cloudflare")
                self.assertEqual(dns["zone"], "example.net")
                self.assertEqual(dns["zoneId"], "zone-itool")
                self.assertEqual(dns["edgeTarget"], "203.0.113.10")
                saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["providers"]["dns"]["zoneId"], "zone-itool")
                self.assertEqual(saved["providers"]["dns"]["edgeTarget"], "203.0.113.10")
            finally:
                _restore_env("CLOUDFLARE_API_TOKEN", old_cf)
                _restore_env("LUMA_DNS_EDGE_TARGET", old_target)
                _restore_env("TRAEFIK_ACME_EMAIL", old_email)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_node_join_examples_are_region_first(self):
        examples = _node_join_examples("https://luma.example.com", "join-token")
        commands = "\n".join(command for _label, command in examples)
        self.assertIn("--region cn --name cn-worker-1", commands)
        self.assertIn("--region global --name global-sg-1", commands)
        self.assertIn("--region home --name home-mac-mini", commands)
        self.assertNotIn("--profile", commands)
        self.assertNotIn("--egress", commands)

    def test_update_manager_installs_cli_then_refreshes_control_only(self):
        state = {"clusterId": "luma-test", "domain": "luma.example.com", "deployToken": "deploy", "joinToken": "join"}
        with patch("luma.cli.manager._run_luma_installer") as installer, patch("luma.cli.manager._reexec_after_luma_update") as reexec, patch(
            "luma.cli.manager._existing_control_state", return_value=state
        ), patch(
            "luma.cli.manager.refresh_manager_control_local", return_value=["Control refreshed"]
        ) as refresh:
            code = main(
                [
                    "update",
                    "manager",
                    "--domain",
                    "luma.example.com",
                    "--install-ref",
                    "main",
                ]
            )

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref="main", skip_node_agent_refresh=True)
        reexec.assert_called_once()
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[2], "luma.example.com")
        self.assertIs(refresh.call_args.args[3], state)

    def test_installer_bootstrap_uses_the_same_tag_ref_as_source_archive(self):
        from luma.installer import luma_installer_command

        command, exact_ref = luma_installer_command("staging/a4b02a3", environ={})

        self.assertEqual(exact_ref, "staging/a4b02a3")
        self.assertIn(
            "https://raw.githubusercontent.com/LiuTianjie/luma/staging/a4b02a3/scripts/install-luma.sh",
            command,
        )
        self.assertNotIn("/main/scripts/install-luma.sh", command)
        self.assertNotIn("| sh", command)
        self.assertIn("curl -fsSL", command)
        self.assertIn('-o "$installer"', command)

    def test_installer_bootstrap_propagates_download_failure(self):
        from luma.installer import luma_installer_command

        with patch("luma.installer.LUMA_INSTALLER_RAW_BASE", "http://127.0.0.1:1"):
            command, _exact_ref = luma_installer_command("missing-ref", environ={})

        completed = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(completed.returncode, 0)

    def test_local_update_pins_bootstrap_installer_and_archive_to_install_ref(self):
        with patch("luma.cli.manager.subprocess.run") as run:
            from luma.cli.manager import _run_luma_installer

            _run_luma_installer(install_ref="v0.1.168")

        self.assertIn("/v0.1.168/scripts/install-luma.sh", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"]["LUMA_INSTALL_REF"], "v0.1.168")

    def test_manager_installer_preserves_running_operator_layout_and_defers_agent_restart(self):
        with patch("luma.cli.manager._current_install_layout", return_value=(
            Path("/home/tao"),
            Path("/home/tao/.local/share/luma"),
            Path("/home/tao/.local/bin"),
        )), patch("luma.cli.manager.subprocess.run") as run:
            from luma.cli.manager import _run_luma_installer

            _run_luma_installer(install_ref="v0.1.173", skip_node_agent_refresh=True)

        env = run.call_args.kwargs["env"]
        self.assertEqual(env["LUMA_USER_HOME"], "/home/tao")
        self.assertEqual(env["LUMA_INSTALL_HOME"], "/home/tao/.local/share/luma")
        self.assertEqual(env["LUMA_BIN_DIR"], "/home/tao/.local/bin")
        self.assertEqual(env["LUMA_SKIP_NODE_AGENT_SERVICE_REFRESH"], "1")

    def test_node_agent_update_pins_bootstrap_installer_and_archive_to_install_ref(self):
        executor = Mock()
        completed = Mock(returncode=0, stdout="installer ok\nLuma version: 0.1.222\n")
        with patch("luma.agent.subprocess.run", return_value=completed) as run, patch(
            "luma.agent.LocalExecutor", return_value=executor
        ), patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent._installed_luma_executable", return_value="/root/.local/bin/luma"
        ):
            update_luma_install(install_ref="staging/a4b02a3")

        self.assertIn("/staging/a4b02a3/scripts/install-luma.sh", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"]["LUMA_INSTALL_REF"], "staging/a4b02a3")

    def test_detached_manager_update_starts_before_installer_or_control_refresh(self):
        with patch("luma.cli.manager._start_detached_manager_update", return_value=0) as detached, patch(
            "luma.cli.manager._run_luma_installer"
        ) as installer, patch("luma.cli.manager._refresh_manager_control") as refresh:
            code = main(["update", "manager", "--detach", "--install-ref", "v0.1.168"])

        self.assertEqual(code, 0)
        detached.assert_called_once()
        installer.assert_not_called()
        refresh.assert_not_called()

    def test_detach_before_manager_subcommand_is_not_overwritten_by_subparser_defaults(self):
        with patch("luma.cli.manager._start_detached_manager_update", return_value=0) as detached:
            code = main(["update", "--detach", "manager"])

        self.assertEqual(code, 0)
        detached.assert_called_once()

    def test_detached_manager_child_does_not_spawn_recursively(self):
        old_detached = _set_env("LUMA_UPDATE_DETACHED", "1")
        old_reexec = _set_env("LUMA_UPDATE_REEXECED", "1")
        try:
            with patch("luma.cli.manager._start_detached_manager_update") as detached, patch(
                "luma.cli.manager._refresh_manager_control"
            ) as refresh, patch("luma.cli.manager._try_refresh_manager_agent"):
                code = main(["update", "manager", "--detach"])
        finally:
            _restore_env("LUMA_UPDATE_DETACHED", old_detached)
            _restore_env("LUMA_UPDATE_REEXECED", old_reexec)

        self.assertEqual(code, 0)
        detached.assert_not_called()
        refresh.assert_called_once()

    def test_detached_manager_update_uses_new_session_private_log_and_status_file(self):
        from luma.cli.manager import _start_detached_manager_update

        process = Mock(pid=4321)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_STATE_HOME": tmp}, clear=False
        ), patch("luma.cli.manager._current_luma_command", return_value=["/opt/luma/bin/luma"]), patch(
            "luma.cli.manager._manager_update_needs_transient_unit", return_value=False
        ), patch(
            "luma.cli.manager.subprocess.Popen", return_value=process
        ) as popen, patch("builtins.print"):
            code = _start_detached_manager_update(
                Mock(_raw_argv=["update", "manager", "--detach", "--install-ref", "deadbeef"])
            )
            logs = list((Path(tmp) / "luma" / "updates").glob("*.log"))
            log_mode = logs[0].stat().st_mode & 0o777

        self.assertEqual(code, 0)
        self.assertEqual(len(logs), 1)
        self.assertEqual(log_mode, 0o600)
        invocation = popen.call_args.args[0]
        self.assertEqual(invocation[-5:], ["update", "manager", "--detach", "--install-ref", "deadbeef"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        detached_env = popen.call_args.kwargs["env"]
        self.assertEqual(detached_env["LUMA_UPDATE_DETACHED"], "1")
        self.assertTrue(detached_env["LUMA_UPDATE_STATUS_PATH"].endswith(".status"))

    def test_detached_manager_update_escapes_node_agent_systemd_cgroup(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "XDG_STATE_HOME": tmp,
                "INVOCATION_ID": "agent-service-invocation",
                "LUMA_CONTROL_IMAGE": "ghcr.io/example/luma-control:v9",
                "CLOUDFLARE_API_TOKEN": "secret",
            },
            clear=False,
        ), patch("luma.cli.manager._current_luma_command", return_value=["/home/tao/.local/bin/luma"]), patch(
            "luma.cli.manager._manager_update_needs_transient_unit", return_value=True
        ), patch("luma.cli.manager.subprocess.run") as run, patch("builtins.print"):
            from luma.cli.manager import _start_detached_manager_update

            code = _start_detached_manager_update(
                Mock(_raw_argv=["update", "manager", "--detach", "--install-ref", "deadbeef"])
            )

        self.assertEqual(code, 0)
        invocation = run.call_args.args[0]
        self.assertEqual(invocation[0], "systemd-run")
        self.assertIn("--collect", invocation)
        self.assertIn("--no-block", invocation)
        self.assertIn("--setenv=LUMA_UPDATE_DETACHED=1", invocation)
        self.assertIn("--setenv=LUMA_CONTROL_IMAGE=ghcr.io/example/luma-control:v9", invocation)
        self.assertNotIn("--setenv=CLOUDFLARE_API_TOKEN=secret", invocation)
        self.assertIn("/home/tao/.local/bin/luma", invocation)

    def test_detach_is_rejected_for_fleet_update(self):
        with patch("luma.cli.manager._run_luma_installer") as installer:
            code = main(["update", "--detach", "fleet"])

        self.assertEqual(code, 1)
        installer.assert_not_called()

    def test_update_manager_infers_cloudflare_dns_before_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "nodes": {
                            "manager": {
                                "host": "localhost",
                                "publicIp": "203.0.113.10",
                                "region": "cn",
                                "roles": ["swarm-manager", "edge"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            state = {"clusterId": "luma-test", "domain": "luma.example.net", "deployToken": "deploy", "joinToken": "join"}
            old_cf = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            old_target = _set_env("LUMA_DNS_EDGE_TARGET", "")
            try:
                captured = {}

                def refresh_side_effect(config, _node, _domain, current_state, **_kwargs):
                    captured["config"] = config.raw
                    captured["state"] = dict(current_state)
                    return ["Control refreshed"]

                def find_zone_side_effect(_config, zone_name):
                    if zone_name == "example.net":
                        return {"id": "zone-itool"}
                    raise LumaError("not found")

                with patch("luma.cli.manager._run_luma_installer"), patch("luma.cli.manager._reexec_after_luma_update"), patch(
                    "luma.cli.manager._existing_control_state", return_value=state
                ), patch("luma.cli.manager.find_zone", side_effect=find_zone_side_effect), patch(
                    "luma.cli.manager.refresh_manager_control_local", side_effect=refresh_side_effect
                ), patch("builtins.print"):
                    code = main(
                        [
                            "--config",
                            str(config_path),
                            "update",
                            "manager",
                            "--domain",
                            "luma.example.net",
                        ]
                    )

                self.assertEqual(code, 0)
                dns = captured["config"]["providers"]["dns"]
                self.assertEqual(dns["type"], "cloudflare")
                self.assertEqual(dns["zone"], "example.net")
                self.assertEqual(dns["zoneId"], "zone-itool")
                self.assertEqual(dns["edgeTarget"], "203.0.113.10")
                self.assertEqual(captured["state"]["secrets"]["CLOUDFLARE_API_TOKEN"], "cf-token")
                saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["providers"]["dns"]["zoneId"], "zone-itool")
                self.assertEqual(saved["providers"]["dns"]["edgeTarget"], "203.0.113.10")
            finally:
                _restore_env("CLOUDFLARE_API_TOKEN", old_cf)
                _restore_env("LUMA_DNS_EDGE_TARGET", old_target)

    def test_update_infers_domain_and_refreshes_when_manager_state_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "control.json").write_text(
                json.dumps({"clusterId": "luma-test", "domain": "luma.example.com"}) + "\n",
                encoding="utf-8",
            )
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(state_dir))
            try:
                with patch("luma.cli.manager._run_luma_installer") as installer, patch("luma.cli.manager._reexec_after_luma_update"), patch(
                    "luma.cli.manager.refresh_manager_control_local", return_value=["Control refreshed"]
                ) as refresh:
                    code = main(["update"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None, skip_node_agent_refresh=True)
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[2], "luma.example.com")

    def test_linux_managed_nfs_prepare_reuses_identical_export_path(self):
        from luma.agent import _linux_prepare_nfs_command

        command = _linux_prepare_nfs_command("staging-runtime-nfs", "/srv/luma")
        self.assertIn("glob.glob('/etc/exports.d/luma-*.exports')", command)
        self.assertIn("if export_line in lines", command)
        self.assertIn("target.unlink(missing_ok=True)", command)
        self.assertIn("already exists with different options", command)

    def test_managed_volume_path_is_writable_by_arbitrary_container_uid(self):
        from luma.agent import _volume_path_command

        command = _volume_path_command("/srv/luma/tenant/app/volume")

        self.assertIn("install -d -m 0777", command)
        self.assertIn("chmod 0777", command)

    def test_update_refreshes_manager_even_when_control_version_matches_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "control.json").write_text(
                json.dumps({"clusterId": "luma-test", "domain": "luma.example.com"}) + "\n",
                encoding="utf-8",
            )
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(state_dir))
            try:
                with patch("luma.cli.manager._run_luma_installer") as installer, patch("luma.cli.manager._reexec_after_luma_update"), patch(
                    "luma.cli.manager.refresh_manager_control_local", return_value=["Control refreshed"]
                ) as refresh, patch("builtins.print") as printed:
                    code = main(["update"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None, skip_node_agent_refresh=True)
        refresh.assert_called_once()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Manager control-plane refresh required", printed_text)
        self.assertIn("local manager control state found", printed_text)

    def test_update_joined_node_skips_agent_refresh_when_control_is_too_old(self):
        with patch("luma.cli.manager._run_luma_installer") as installer, patch("luma.cli.manager._reexec_after_luma_update"), patch(
            "luma.cli.manager._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch("luma.cli.manager._local_agent_config", return_value=None), patch("luma.cli.manager._safe_local_nomad_node_id", return_value="node-1"), patch(
            "luma.cli.manager._control_context",
            return_value=("https://luma.example.com", "management-token", False, None),
        ), patch(
            "luma.cli.manager._refresh_local_node_agent",
            side_effect=LumaError("control API does not support node-agent credentials yet. Update the manager control plane first."),
        ), patch("builtins.print") as printed:
            code = main(["update"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None, skip_node_agent_refresh=False)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("[info] Role: joined node", printed_text)
        self.assertIn("[skip] Luma node agent skipped", printed_text)
        self.assertIn("[ok] Joined node update complete", printed_text)

    def test_update_joined_node_skips_agent_refresh_when_node_is_unregistered(self):
        with patch("luma.cli.manager._run_luma_installer") as installer, patch("luma.cli.manager._reexec_after_luma_update"), patch(
            "luma.cli.manager._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch("luma.cli.manager._local_agent_config", return_value=None), patch("luma.cli.manager._safe_local_nomad_node_id", return_value="stale-node-id"), patch(
            "luma.cli.manager._control_context",
            return_value=("https://luma.example.com", "management-token", False, None),
        ), patch(
            "luma.cli.manager._refresh_local_node_agent",
            side_effect=LumaError("control API error 400: {\"error\": \"nodeName or nodeId must match a registered node\"}"),
        ), patch("builtins.print") as printed:
            code = main(["update"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None, skip_node_agent_refresh=False)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("[info] Role: joined node", printed_text)
        self.assertIn("[skip] Luma node agent skipped", printed_text)
        self.assertIn("nodeName or nodeId must match a registered node", printed_text)
        self.assertIn("[ok] Joined node update complete", printed_text)

    def test_update_joined_node_prefers_explicit_control_token_over_local_agent_config(self):
        with patch("luma.cli.manager._run_luma_installer"), patch("luma.cli.manager._reexec_after_luma_update"), patch(
            "luma.cli.manager._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch(
            "luma.cli.manager._local_agent_config",
            return_value={
                "endpoint": "https://luma.example.com",
                "token": "stale-agent-token",
                "nodeName": "home-mac-mini",
                "nodeId": "old-node-id",
            },
        ), patch("luma.cli.manager._safe_local_nomad_node_id", return_value="new-node-id"), patch(
            "luma.cli.manager._control_context",
            return_value=("https://luma.example.com", "join-token", False, None),
        ) as context, patch(
            "luma.cli.manager._refresh_local_node_agent"
        ) as refresh:
            code = main(["update", "--control-url", "https://luma.example.com", "--token", "join-token"])

        self.assertEqual(code, 0)
        context.assert_called_once()
        refresh.assert_called_once_with(
            endpoint="https://luma.example.com",
            token="join-token",
            insecure=False,
            resolve_ip=None,
            allow_skip=False,
        )

    def test_update_after_reexec_skips_installer_and_refreshes_manager(self):
        old_reexec = _set_env("LUMA_UPDATE_REEXECED", "1")
        try:
            with patch("luma.cli.manager._run_luma_installer") as installer, patch(
                "luma.cli.manager._manager_refresh_decision", return_value=(True, "local manager control state found")
            ), patch("luma.cli.manager._refresh_manager_control") as refresh, patch("luma.cli.manager._try_refresh_manager_agent"), patch("builtins.print") as printed:
                code = main(["update"])
        finally:
            _restore_env("LUMA_UPDATE_REEXECED", old_reexec)

        self.assertEqual(code, 0)
        installer.assert_not_called()
        refresh.assert_called_once()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("[skip] Luma CLI already updated in this run", printed_text)
        self.assertIn("[ok] Manager update complete", printed_text)

    def test_update_without_manager_state_updates_cli_only(self):
        with patch("luma.cli.manager._existing_control_state", return_value=None), patch(
            "luma.cli.manager._run_luma_installer"
        ) as installer, patch("luma.cli.manager._reexec_after_luma_update"), patch("luma.cli.manager.refresh_manager_control_local") as refresh, patch("builtins.print") as printed:
            code = main(["update"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None, skip_node_agent_refresh=False)
        refresh.assert_not_called()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("CLI updated", printed_text)
        self.assertIn("Manager control-plane refresh skipped", printed_text)

    def test_update_reports_unreadable_manager_state_instead_of_using_client_token(self):
        from luma.cli.manager import _manager_refresh_decision

        args = Mock(
            domain=None,
            node=None,
            http_port=None,
            https_port=None,
            skip_egress=False,
            overwrite_control_state=False,
            profile="single-node",
        )
        with patch("luma.cli.manager._existing_control_state", return_value=None), patch(
            "luma.cli.manager._manager_state_requires_privilege", return_value=True
        ):
            with self.assertRaisesRegex(LumaError, "not readable by this user"):
                _manager_refresh_decision(args)

    def test_update_fleet_updates_local_cli_and_remote_nodes_with_install_ref(self):
        client = Mock()
        client.update_fleet.return_value = {
            "succeeded": 1,
            "failed": 0,
            "skipped": 0,
            "results": [{"nodeName": "home-mac-mini", "region": "home", "os": "darwin", "status": "succeeded", "message": "Luma installer finished"}],
        }
        with patch("luma.cli.manager._run_luma_installer") as installer, patch("luma.cli.manager._reexec_after_luma_update"), patch(
            "luma.cli.manager._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch(
            "luma.cli.manager._control_context", return_value=("https://luma.example.com", "management-token", False, None)
        ), patch(
            "luma.cli.common.ControlClient", return_value=client
        ), patch("builtins.print"):
            code = main(["update", "fleet", "--install-ref", "main", "--timeout", "120"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref="main", skip_node_agent_refresh=False)
        client.update_fleet.assert_called_once_with(install_ref="main", include_all=False, include_manager=False, timeout=120)

    def test_update_fleet_include_manager_flag_is_explicit(self):
        client = Mock()
        client.update_fleet.return_value = {"succeeded": 0, "failed": 0, "skipped": 0, "results": []}
        with patch("luma.cli.manager._run_luma_installer"), patch("luma.cli.manager._reexec_after_luma_update"), patch(
            "luma.cli.manager._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch(
            "luma.cli.manager._control_context", return_value=("https://luma.example.com", "management-token", False, None)
        ), patch(
            "luma.cli.common.ControlClient", return_value=client
        ), patch("builtins.print"):
            code = main(["update", "fleet", "--include-manager"])

        self.assertEqual(code, 0)
        client.update_fleet.assert_called_once_with(install_ref="", include_all=False, include_manager=True, timeout=900)


    def test_update_manager_does_not_call_full_bootstrap_paths(self):
        state = {"clusterId": "luma-test", "domain": "luma.example.com", "deployToken": "deploy", "joinToken": "join"}
        with patch("luma.cli.manager._run_luma_installer"), patch("luma.cli.manager._reexec_after_luma_update"), patch("luma.cli.manager._existing_control_state", return_value=state), patch(
            "luma.cli.manager.refresh_manager_control_local", return_value=["Control refreshed"]
        ), patch("luma.cli.manager.bootstrap_manager_local") as bootstrap_manager, patch(
            "luma.cli.nodes.install_docker"
        ) as docker, patch("luma.cli.manager.setup_egress") as egress:
            code = main(["update", "manager", "--domain", "luma.example.com"])

        self.assertEqual(code, 0)
        bootstrap_manager.assert_not_called()
        docker.assert_not_called()
        egress.assert_not_called()

    def test_service_restart_exposes_recreate_and_task_modes(self):
        client = Mock()
        client.restart_application.return_value = {
            "stack": "ledger",
            "service": "mysql",
            "mode": "task",
            "restarted": [{"allocId": "alloc-1", "task": "mysql", "mode": "task"}],
        }
        with patch(
            "luma.cli.apps._control_context",
            return_value=("https://luma.example.com", "deploy-token", False, None),
        ), patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
            code = main(["app", "restart", "ledger", "--service", "mysql", "--mode", "task", "--timeout", "45"])

        self.assertEqual(code, 0)
        client.restart_application.assert_called_once_with(stack="ledger", service="mysql", mode="task", timeout=45)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Restart finished: ledger/mysql (task)", printed_text)

    def test_version_local_skips_control_check(self):
        with patch("luma.cli.common.ControlClient") as client_cls, patch("builtins.print") as printed:
            code = main(["version", "--local"])

        self.assertEqual(code, 0)
        client_cls.assert_not_called()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn(f"Luma CLI: {__version__}", printed_text)
        self.assertNotIn("Luma Control", printed_text)

    def test_version_prints_cli_without_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                with patch("builtins.print") as printed:
                    code = main(["version"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

        self.assertEqual(code, 0)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn(f"Luma CLI: {__version__}", printed_text)
        self.assertIn("Luma Control: not checked", printed_text)

    def test_version_prints_control_health_from_explicit_url(self):
        client = Mock()
        client.health.return_value = {
            "version": "0.1.0",
            "nodeJoinModel": "region-first",
            "capabilities": ["node-region", "service-proxy"],
        }
        with patch("luma.cli.common.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
            code = main(["version", "--control-url", "https://luma.example.com"])

        self.assertEqual(code, 0)
        client_cls.assert_called_once_with("https://luma.example.com", "health", insecure=False, resolve_ip=None)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Luma Control: 0.1.0", printed_text)
        self.assertIn("Node join model: region-first", printed_text)
        self.assertIn("Capabilities: node-region, service-proxy", printed_text)

    def test_status_prints_control_plane_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.status.return_value = {
                    "clusterId": "luma-test",
                    "version": "0.1.2",
                    "configPath": "/opt/luma/luma.yaml",
                    "dns": {
                        "provider": "cloudflare",
                        "zone": "example.com",
                        "zoneIdConfigured": True,
                        "tokenEnv": "CLOUDFLARE_API_TOKEN",
                        "tokenConfigured": True,
                        "target": "203.0.113.10",
                        "ready": True,
                    },
                    "storage": {
                        "storageClasses": [
                            {
                                "name": "cn-nfs",
                                "provider": "nfs",
                                "mode": "managed",
                                "node": "manager",
                                "path": "/srv/luma",
                                "regions": ["cn"],
                            }
                        ]
                    },
                    "nodes": {
                        "registered": 2,
                        "items": [
                            {"name": "manager", "region": "cn", "status": "labeled", "displayName": "manager"},
                            {"name": "docker-home", "region": "home", "status": "labeled", "displayName": "mini-mini"},
                        ],
                    },
                    "nomad": {
                        "available": True,
                        "leader": "100.64.0.1:4647",
                        "nodes": [
                            {
                                "hostname": "manager",
                                "lumaNode": "manager",
                                "role": "client",
                                "state": "ready",
                                "availability": "eligible",
                                "region": "cn",
                                "leader": True,
                            },
                            {
                                "hostname": "docker-home",
                                "lumaNode": "docker-home",
                                "role": "client",
                                "state": "ready",
                                "availability": "eligible",
                                "region": "home",
                                "leader": False,
                            },
                        ],
                    },
                }
                with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["status"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

        self.assertEqual(code, 0)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Luma status", printed_text)
        self.assertIn("Control", printed_text)
        self.assertIn("Cluster  luma-test", printed_text)
        self.assertIn("Version  0.1.2", printed_text)
        self.assertIn("DNS", printed_text)
        self.assertIn("Ready     yes", printed_text)
        self.assertIn("Provider  cloudflare", printed_text)
        self.assertIn("Orchestrator (Nomad)", printed_text)
        self.assertIn("Storage", printed_text)
        self.assertIn("Summary: storageClasses=1", printed_text)
        self.assertIn("cn-nfs", printed_text)
        self.assertIn("/srv/luma", printed_text)
        self.assertIn("Nodes", printed_text)
        self.assertIn("Summary: registered=2, nomad=2", printed_text)
        self.assertIn("NAME         REGION", printed_text)
        self.assertIn("docker-home  home    labeled     ready  client", printed_text)
        self.assertIn("manager      cn      labeled     ready  client", printed_text)

    def test_status_prints_dns_missing_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.status.return_value = {
                    "clusterId": "luma-test",
                    "version": "0.1.2",
                    "configPath": "/opt/luma/luma.yaml",
                    "dns": {
                        "provider": "not configured",
                        "zone": "",
                        "zoneIdConfigured": False,
                        "tokenEnv": "CLOUDFLARE_API_TOKEN",
                        "tokenConfigured": True,
                        "target": "",
                        "ready": False,
                        "missing": ["provider", "zoneId", "target"],
                    },
                    "portainer": {
                        "apiUrl": "https://100.64.0.1:9443/api",
                        "endpointIdConfigured": True,
                        "swarmIdConfigured": True,
                        "ready": True,
                    },
                    "storage": {"storageClasses": []},
                    "nodes": {"registered": 0, "items": []},
                    "swarm": {"available": True, "nodes": []},
                }
                with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["status"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

        self.assertEqual(code, 0)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Ready     no", printed_text)
        self.assertIn("Provider  not configured", printed_text)
        self.assertIn("Missing   provider, zoneId, target", printed_text)

    def test_status_uses_env_context_without_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            old_url = _set_env("LUMA_CONTROL_URL", "https://luma.example.com")
            old_token = _set_env("LUMA_DEPLOY_TOKEN", "deploy-token")
            old_insecure = _set_env("LUMA_INSECURE", "false")
            old_resolve = _set_env("LUMA_RESOLVE_IP", "")
            try:
                client = Mock()
                client.status.return_value = {"clusterId": "luma-test", "version": "0.1.2"}
                with patch("luma.cli.common.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
                    code = main(["--no-env", "status", "--format", "json"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)
                _restore_env("LUMA_CONTROL_URL", old_url)
                _restore_env("LUMA_DEPLOY_TOKEN", old_token)
                _restore_env("LUMA_INSECURE", old_insecure)
                _restore_env("LUMA_RESOLVE_IP", old_resolve)

        self.assertEqual(code, 0)
        client_cls.assert_called_once_with("https://luma.example.com", "deploy-token", insecure=False, resolve_ip=None)
        payload = json.loads(printed.call_args_list[-1].args[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "status")
        self.assertEqual(payload["result"]["clusterId"], "luma-test")

    def test_status_cli_context_overrides_env_and_login_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            old_url = _set_env("LUMA_CONTROL_URL", "https://env.example.com")
            old_token = _set_env("LUMA_DEPLOY_TOKEN", "env-token")
            try:
                save_context(endpoint="https://context.example.com", cluster_id="luma-test", token="context-token")
                client = Mock()
                client.status.return_value = {"clusterId": "luma-test"}
                with patch("luma.cli.common.ControlClient", return_value=client) as client_cls, patch("builtins.print"):
                    code = main(
                        [
                            "--no-env",
                            "status",
                            "--control-url",
                            "https://cli.example.com",
                            "--token",
                            "cli-token",
                            "--format",
                            "json",
                        ]
                    )
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)
                _restore_env("LUMA_CONTROL_URL", old_url)
                _restore_env("LUMA_DEPLOY_TOKEN", old_token)

        self.assertEqual(code, 0)
        client_cls.assert_called_once_with("https://cli.example.com", "cli-token", insecure=False, resolve_ip=None)

    def test_status_json_reports_invalid_env_bool(self):
        old_url = _set_env("LUMA_CONTROL_URL", "https://luma.example.com")
        old_token = _set_env("LUMA_DEPLOY_TOKEN", "deploy-token")
        old_insecure = _set_env("LUMA_INSECURE", "sometimes")
        try:
            with patch("luma.cli.common.ControlClient") as client_cls, patch("builtins.print") as printed:
                code = main(["--no-env", "status", "--format", "json"])
        finally:
            _restore_env("LUMA_CONTROL_URL", old_url)
            _restore_env("LUMA_DEPLOY_TOKEN", old_token)
            _restore_env("LUMA_INSECURE", old_insecure)

        self.assertEqual(code, 1)
        client_cls.assert_not_called()
        payload = json.loads(printed.call_args_list[-1].args[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "luma_error")
        self.assertIn("LUMA_INSECURE", payload["error"]["message"])

    def test_secret_and_registry_list_use_env_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            old_url = _set_env("LUMA_CONTROL_URL", "https://luma.example.com")
            old_token = _set_env("LUMA_DEPLOY_TOKEN", "deploy-token")
            try:
                secret_client = Mock()
                secret_client.list_secrets.return_value = {"secrets": ["DATABASE_URL"]}
                registry_client = Mock()
                registry_client.list_registries.return_value = {"registries": [{"host": "ghcr.io", "username": "bot"}]}
                with patch("luma.cli.common.ControlClient", side_effect=[secret_client, registry_client]) as client_cls, patch(
                    "builtins.print"
                ) as printed:
                    secret_code = main(["--no-env", "secret", "list", "--format", "json"])
                    registry_code = main(["--no-env", "registry", "list", "--format", "json"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)
                _restore_env("LUMA_CONTROL_URL", old_url)
                _restore_env("LUMA_DEPLOY_TOKEN", old_token)

        self.assertEqual(secret_code, 0)
        self.assertEqual(registry_code, 0)
        self.assertEqual(client_cls.call_count, 2)
        for client_call in client_cls.call_args_list:
            self.assertEqual(client_call.args[:2], ("https://luma.example.com", "deploy-token"))
        payloads = [json.loads(call.args[0]) for call in printed.call_args_list]
        self.assertEqual(payloads[0]["result"]["secrets"], ["DATABASE_URL"])
        self.assertEqual(payloads[1]["result"]["registries"][0]["host"], "ghcr.io")

    def test_secret_and_registry_writes_use_env_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            old_url = _set_env("LUMA_CONTROL_URL", "https://luma.example.com")
            old_token = _set_env("LUMA_DEPLOY_TOKEN", "deploy-token")
            try:
                secret_client = Mock()
                secret_client.set_secret.return_value = {"name": "DATABASE_URL", "saved": True}
                registry_client = Mock()
                registry_client.set_registry.return_value = {"host": "ghcr.io"}
                with patch("luma.cli.common.ControlClient", side_effect=[secret_client, registry_client]) as client_cls, patch(
                    "sys.stdin", io.StringIO("registry-token\n")
                ), patch("builtins.print"):
                    secret_code = main(["--no-env", "secret", "set", "DATABASE_URL", "--value", "postgres://secret"])
                    registry_code = main(["--no-env", "registry", "login", "ghcr.io", "--username", "bot", "--password-stdin"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)
                _restore_env("LUMA_CONTROL_URL", old_url)
                _restore_env("LUMA_DEPLOY_TOKEN", old_token)

        self.assertEqual(secret_code, 0)
        self.assertEqual(registry_code, 0)
        self.assertEqual(client_cls.call_count, 2)
        for client_call in client_cls.call_args_list:
            self.assertEqual(client_call.args[:2], ("https://luma.example.com", "deploy-token"))
        secret_client.set_secret.assert_called_once_with(name="DATABASE_URL", value="postgres://secret")
        registry_client.set_registry.assert_called_once_with(host="ghcr.io", username="bot", password="registry-token")

    def test_node_status_accepts_node_alias_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.status.return_value = {
                    "clusterId": "luma-test",
                    "nodes": {
                        "registered": 2,
                        "items": [
                            {
                                "name": "mini",
                                "displayName": "mini",
                                "aliases": ["home-mac-mini"],
                                "region": "home",
                                "status": "labeled",
                                "agentStatus": "ready",
                                "agentOs": "darwin",
                                "agentLastSeen": 0,
                            },
                            {
                                "name": "lab",
                                "displayName": "lab",
                                "region": "home",
                                "status": "labeled",
                                "agentStatus": "ready",
                                "agentOs": "linux",
                                "agentLastSeen": 0,
                            },
                        ],
                    },
                    "nomad": {
                        "available": True,
                        "nodes": [
                            {"name": "mini", "lumaNode": "mini", "hostname": "Mac.lan"},
                            {"name": "lab", "lumaNode": "lab", "hostname": "ubuntu"},
                        ],
                    },
                }
                with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["node", "status", "home-mac-mini"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

        self.assertEqual(code, 0)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("mini", printed_text)
        self.assertNotIn("\n  lab", printed_text)

    def test_node_exit_cleans_local_nomad_and_runtime_state(self):
        remote = Mock()
        remote.sudo_result.return_value = Mock(code=0, output="stopped\n")
        remote.sudo.return_value = ""

        with patch("luma.cli.nodes.LocalExecutor", return_value=remote):
            results = exit_local_node()

        self.assertEqual(results, ["Nomad agent stopped", "Removed /opt/luma"])
        self.assertIn("nomad", remote.sudo_result.call_args.args[0])
        remote.sudo.assert_called_once_with("rm -rf /opt/luma")

    def test_node_exit_optional_deep_cleanup(self):
        remote = Mock()
        remote.sudo_result.side_effect = [
            Mock(code=0, output="skipped\n"),
            Mock(code=0, output="done\n"),
            Mock(code=0, output="done\n"),
        ]
        remote.sudo.return_value = ""

        with patch("luma.cli.nodes.LocalExecutor", return_value=remote):
            results = exit_local_node(tailscale=True, prune_docker=True)

        self.assertEqual(
            results,
            ["Nomad agent stop skipped", "Removed /opt/luma", "Tailscale logged out", "Docker pruned"],
        )
        commands = [call.args[0] for call in remote.sudo_result.call_args_list]
        self.assertTrue(any("tailscale logout" in command for command in commands))
        prune_commands = [command for command in commands if "docker system prune -af --volumes" in command]
        self.assertEqual(len(prune_commands), 1)
        self.assertIn("/opt/luma/events/node-exit.jsonl", prune_commands[0])
        self.assertIn("logger -t luma", prune_commands[0])

    def test_node_join_prompts_for_worker_config_during_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                secret_values = iter(["ts-key", "sudo-pass"])
                client = Mock()
                client.register_node.return_value = {
                    "nodeName": "worker-1",
                    "region": "global",
                    "nomadRpcAddr": "100.64.0.1:4647",
                }
                client.label_node.return_value = {"message": "labels applied"}
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)
                ), patch("luma.cli.nodes.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.nodes.install_docker", return_value="Docker available"
                ), patch(
                    "luma.cli.common.ControlClient", return_value=client
                ), patch("luma.cli.nodes.install_nomad_node", return_value=[]), patch(
                    "luma.cli.nodes.local_nomad_node_info", return_value=("worker-1", "node-id-1")
                ), patch(
                    "luma.cli.nodes._local_tailscale_ip", return_value="100.64.0.10"
                ):
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "global",
                            "--name",
                            "global-sg-1",
                        ]
                    )
                self.assertEqual(code, 1)  # Missing agent credentials must not report a completed join.
                client.register_node.assert_called_once_with(node_name="global-sg-1", region="global")
                client.label_node.assert_called_once_with(
                    node_name="worker-1",
                    region="global",
                    registered_name="global-sg-1",
                    node_id="node-id-1",
                    tailscale_ip="100.64.0.10",
                )
                self.assertIn("TAILSCALE_AUTHKEY", configured_keys(config_path))
                self.assertNotIn("LUMA_SUDO_PASSWORD", configured_keys(config_path))
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_home_node_join_requires_tailscale_key_when_disconnected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", return_value=""
                ), patch("luma.cli.nodes._local_tailscale_connected", return_value=False), patch(
                    "luma.cli.common.ControlClient"
                ) as client_cls, patch("builtins.print"):
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "home",
                            "--name",
                            "home-mac-mini",
                        ]
                    )
                self.assertEqual(code, 1)
                client_cls.assert_not_called()
                self.assertNotIn("TAILSCALE_AUTHKEY", configured_keys(config_path))
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_home_node_join_prompts_for_required_tailscale_key_before_registering(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                secret_values = iter(["ts-key", "sudo-pass"])
                client = Mock()
                client.register_node.return_value = {
                    "nodeName": "home-mac-mini",
                    "region": "home",
                    "nomadRpcAddr": "100.64.0.1:4647",
                }
                client.label_node.return_value = {"message": "labels applied"}
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)
                ), patch("luma.cli.nodes._local_tailscale_connected", side_effect=[False, False]), patch(
                    "luma.cli.nodes.configure_dns", return_value="DNS ok"
                ), patch("luma.cli.nodes.install_docker", return_value="Docker available"
                ), patch("luma.cli.common.ControlClient", return_value=client), patch(
                    "luma.cli.nodes.install_nomad_node", return_value=[]
                ), patch("luma.cli.nodes.local_nomad_node_info", return_value=("docker-home", "home-id-1")
                ), patch(
                    "luma.cli.nodes._local_tailscale_ip", return_value="100.64.0.20"
                ):
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "home",
                            "--name",
                            "home-mac-mini",
                        ]
                    )
                self.assertEqual(code, 1)  # Missing agent credentials must not report a completed join.
                client.register_node.assert_called_once_with(node_name="home-mac-mini", region="home")
                client.label_node.assert_called_once_with(
                    node_name="docker-home",
                    region="home",
                    registered_name="home-mac-mini",
                    node_id="home-id-1",
                    tailscale_ip="100.64.0.20",
                )
                self.assertIn("TAILSCALE_AUTHKEY", configured_keys(config_path))
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_nomad_node_join_labels_and_installs_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                client = Mock()
                client.register_node.return_value = {
                    "nodeName": "bot",
                    "region": "global",
                    "nomadRpcAddr": "100.64.0.125:4647",
                }
                client.label_node.return_value = {
                    "message": "labels applied",
                    "agentToken": "agent-token",
                    "nodeName": "bot",
                }
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", return_value="sudo-pass"
                ), patch("luma.cli.nodes.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.nodes.install_docker", return_value="Docker available"
                ), patch("luma.cli.common.ControlClient", return_value=client), patch(
                    "luma.cli.nodes.install_nomad_node", return_value=[]
                ) as install_nomad, patch(
                    "luma.cli.nodes.local_nomad_node_info", return_value=("bot-host", "nomad-node-id")
                ), patch(
                    "luma.cli.nodes._local_tailscale_ip", return_value="100.80.0.20"
                ), patch(
                    "luma.cli.nodes._install_node_agent_from_token"
                ) as install_agent, patch("luma.cli.nodes.wait_for_node_readiness", return_value={"agentVersion": "test"}) as verify:
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "global",
                            "--name",
                            "bot",
                        ]
                    )

                self.assertEqual(code, 0)
                verify.assert_called_once_with(client, node_name="bot", node_id="nomad-node-id")
                client.register_node.assert_called_once_with(node_name="bot", region="global")
                install_nomad.assert_called_once()
                install_kwargs = install_nomad.call_args.kwargs
                self.assertEqual(install_kwargs["server_addrs"], ["100.64.0.125:4647"])
                self.assertIsNone(install_kwargs["egress_proxy"])
                client.label_node.assert_called_once_with(
                    node_name="bot-host",
                    region="global",
                    registered_name="bot",
                    node_id="nomad-node-id",
                    tailscale_ip="100.80.0.20",
                )
                install_agent.assert_called_once()
                self.assertEqual(install_agent.call_args.kwargs["agent_token"], "agent-token")
                self.assertEqual(install_agent.call_args.kwargs["node_id"], "nomad-node-id")
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_node_join_checks_docker_before_registering(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                client = Mock()
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", return_value="sudo-pass"
                ), patch("luma.cli.nodes.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.nodes.install_docker", side_effect=LumaError("Docker is not ready")
                ), patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print"):
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "global",
                            "--name",
                            "global-sg-1",
                        ]
                    )

                self.assertEqual(code, 1)
                client.register_node.assert_not_called()
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_node_join_unregisters_when_local_nomad_join_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                client = Mock()
                client.register_node.return_value = {
                    "nodeName": "global-sg-1",
                    "region": "global",
                    "nomadRpcAddr": "100.64.0.1:4647",
                }
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", return_value="sudo-pass"
                ), patch("luma.cli.nodes.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.nodes.install_docker", return_value="Docker available"
                ), patch("luma.cli.common.ControlClient", return_value=client), patch(
                    "luma.cli.nodes.install_nomad_node", side_effect=LumaError("nomad join failed")
                ), patch("builtins.print") as printed:
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "global",
                            "--name",
                            "global-sg-1",
                        ]
                    )

                self.assertEqual(code, 1)
                client.unregister_node.assert_called_once_with(node_name="global-sg-1")
                output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("[ok] Rolled back node registration: global-sg-1", output)
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_node_join_rejects_profile_argument(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.print"), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "node",
                    "join",
                    "https://luma.example.com",
                    "--token",
                    "join-token",
                    "--profile",
                    "home-node",
                    "--region",
                    "home",
                ]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_macos_tailscale_uses_authkey_when_available(self):
        node = LumaConfig({"nodes": {"mini": {"host": "localhost", "region": "home"}}}, None).get_node("mini")
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(code=0, output="Darwin\n"),
            Mock(code=0, output="/usr/local/bin/tailscale\n"),
            Mock(code=1, output="not logged in\n"),
            Mock(code=0, output=""),
        ]

        results = setup_tailscale(node, authkey="ts-key", executor=remote)

        self.assertEqual(results, ["Tailscale connected: luma-mini"])
        commands = [call.args[0] for call in remote.run_result.call_args_list]
        self.assertTrue(any("tailscale up" in command and "--authkey ts-key" in command for command in commands))
        self.assertTrue(any("tailscale up" in command and "--accept-routes" in command for command in commands))

    def test_tailscale_up_retries_with_reset_for_existing_nondefault_flags(self):
        node = LumaConfig({"nodes": {"mini": {"host": "localhost", "region": "home"}}}, None).get_node("mini")
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo.return_value = ""
        remote.sudo_result.side_effect = [
            Mock(code=1, output="not logged in\n"),
            Mock(
                code=1,
                output=(
                    "Error: changing settings via 'tailscale up' requires mentioning all "
                    "non-default flags. tailscale up --auth-key=ts-key --accept-routes\n"
                ),
            ),
            Mock(code=0, output=""),
        ]

        results = setup_tailscale(node, authkey="ts-key", executor=remote)

        self.assertEqual(results, ["Tailscale installed", "Tailscale connected: luma-mini"])
        commands = [call.args[0] for call in remote.sudo_result.call_args_list]
        self.assertIn("tailscale status", commands[0])
        self.assertIn("tailscale up", commands[1])
        self.assertNotIn("--reset", commands[1])
        self.assertIn("--accept-routes", commands[1])
        self.assertIn("tailscale up", commands[2])
        self.assertIn("--reset", commands[2])
        self.assertIn("--accept-routes", commands[2])

    def test_linux_tailscale_reports_already_installed_when_binary_exists(self):
        node = LumaConfig({"nodes": {"mini": {"host": "localhost", "region": "home"}}}, None).get_node("mini")
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo.return_value = "luma_tailscale_present\n"
        remote.sudo_result.return_value = Mock(code=0, output="")

        results = setup_tailscale(node, executor=remote)

        self.assertEqual(results, ["Tailscale already installed", "Tailscale already logged in"])
        command = remote.sudo.call_args.args[0]
        self.assertIn("command -v tailscale", command)
        self.assertIn("curl -fsSL https://tailscale.com/install.sh | sh", command)

    def test_install_nomad_node_skips_binary_download_when_pinned_version_exists(self):
        node = LumaConfig({"nodes": {"worker": {"host": "localhost", "region": "cn"}}}, None).get_node("worker")
        remote = Mock()
        remote.sudo.side_effect = [
            "luma_nomad_binary_present\n",
            "",
            "",
            "",
        ]
        uname = Mock(machine="x86_64")
        with patch("luma.bootstrap.LocalExecutor", return_value=remote), patch(
            "luma.bootstrap.setup_tailscale", return_value=["Tailscale already logged in"]
        ), patch("luma.bootstrap._tailscale_ip", return_value="100.80.0.20"), patch(
            "luma.nomad_node.detect_os", return_value="linux"
        ), patch("luma.nomad_node.detect_cpu_total_compute", return_value=None), patch(
            "luma.bootstrap.os.uname", return_value=uname
        ), patch(
            "luma.bootstrap.verify_local_nomad_node", return_value="Nomad agent ready"
        ):
            results = install_nomad_node(
                node,
                role="client",
                region="cn",
                node_name="worker",
                server_addrs=["100.64.0.125:4647"],
                install_docker_first=False,
            )

        self.assertIn("Nomad binary already installed", results)
        self.assertIn("Nomad config written", results)
        self.assertIn("Nomad agent started", results)
        binary_command = remote.sudo.call_args_list[0].args[0]
        self.assertIn("nomad version", binary_command)
        self.assertIn("grep -Eq", binary_command)
        self.assertLess(binary_command.index("nomad version"), binary_command.index("curl -fsSL"))

    def test_noninteractive_config_skips_missing_optional_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                with patch("sys.stdin.isatty", return_value=False):
                    result = ensure_interactive_config("worker", path=config_path)
                self.assertIsNone(result)
                self.assertFalse(config_path.exists())
            finally:
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_noninteractive_config_still_requires_required_values(self):
        old_token = _set_env("CLOUDFLARE_API_TOKEN", "")
        try:
            with patch("sys.stdin.isatty", return_value=False):
                with self.assertRaises(LumaError) as ctx:
                    ensure_interactive_config("manager", keys=["CLOUDFLARE_API_TOKEN"])
            self.assertIn("CLOUDFLARE_API_TOKEN", str(ctx.exception))
        finally:
            _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_user_config_loads_missing_or_empty_env_without_overriding_existing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            config_path.write_text(
                '{"version":1,"env":{"CLOUDFLARE_API_TOKEN":"from-config","TAILSCALE_AUTHKEY":"ts-key"}}\n',
                encoding="utf-8",
            )
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "from-env")
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            try:
                load_user_config(config_path)
                import os

                self.assertEqual(os.environ["CLOUDFLARE_API_TOKEN"], "from-env")
                self.assertEqual(os.environ["TAILSCALE_AUTHKEY"], "ts-key")
            finally:
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)

    def test_deploy_without_context_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                ),
                encoding="utf-8",
            )
            try:
                code = main(["deploy", str(service_path), "--skip-dns", "--skip-orchestrator"])
                self.assertEqual(code, 1)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_deploy_env_file_must_exist_when_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_home = _set_env("LUMA_CONFIG_HOME", str(root / "home"))
            service_path = root / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                ),
                encoding="utf-8",
            )
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                with patch("luma.cli.common.ControlClient", return_value=client):
                    code = main(["deploy", str(service_path), "--env", str(root / "missing.env")])
                self.assertEqual(code, 1)
                client.deploy_events.assert_not_called()
                client.deploy.assert_not_called()
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_control_client_requires_https(self):
        with self.assertRaises(Exception):
            ControlClient("http://luma.example.com", "secret")

    def test_control_client_resolve_ip_keeps_https_endpoint_host(self):
        client = ControlClient("https://luma.example.com:8443", "secret", insecure=True, resolve_ip="203.0.113.10")
        self.assertEqual(client._request_url("/v1/health"), "https://203.0.113.10:8443/v1/health")
        self.assertEqual(client._host_header, "luma.example.com:8443")

    def test_control_client_resolve_ip_requires_insecure(self):
        with self.assertRaises(LumaError):
            ControlClient("https://luma.example.com", "secret", resolve_ip="203.0.113.10")

    def test_control_client_reports_node_api_error_directly(self):
        client = ControlClient("https://luma.example.com", "secret")
        error = urllib.error.HTTPError(
            "https://luma.example.com/v1/nodes/register",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error": "nodeName, profile, and region are required"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(LumaError) as raised:
            client.register_node(node_name="m3max", region="home")

        self.assertIn("control API error 400", str(raised.exception))
        self.assertIn("nodeName, profile, and region are required", str(raised.exception))

    def test_control_client_decodes_gzip_registry_inventory(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.headers = {"Content-Encoding": "gzip"}
        response.read.return_value = gzip.compress(b'{"summary":{"repositoryCount":12}}')
        response.read1.side_effect = [response.read.return_value, b""]
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            payload = client.registry_inventory()

        self.assertEqual(payload["summary"]["repositoryCount"], 12)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.headers["Accept-encoding"], "gzip")

    def test_control_client_reports_storage_api_error_directly(self):
        client = ControlClient("https://luma.example.com", "secret")
        error = urllib.error.HTTPError(
            "https://luma.example.com/v1/storage",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error": "storage class cn-nfs endpoint is required for nfs"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(LumaError) as raised:
            client.set_storage(name="cn-nfs", provider="nfs", node="cn-node", path="/srv/luma")

        self.assertIn("control API error 400", str(raised.exception))
        self.assertIn("endpoint is required for nfs", str(raised.exception))

    def test_control_client_storage_mutations_use_operational_timeout(self):
        client = ControlClient("https://luma.example.com", "secret")
        with patch.object(client, "request", return_value={"saved": True}) as request:
            client.set_storage(
                name="cn-nfs",
                provider="nfs",
                node="cn-node",
                path="/srv/luma",
            )
        self.assertEqual(request.call_args.kwargs["timeout"], 360)

        with patch.object(client, "request", return_value={"removed": True}) as request:
            client.remove_storage(name="cn-nfs")
        self.assertEqual(request.call_args.kwargs["timeout"], 360)

    def test_control_client_reports_missing_agent_token_endpoint_as_old_control(self):
        client = ControlClient("https://luma.example.com", "secret")
        error = urllib.error.HTTPError(
            "https://luma.example.com/v1/nodes/agent-token",
            404,
            "Not Found",
            {},
            io.BytesIO(b'{"error": "not found"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(LumaError) as raised:
            client.issue_agent_token(node_name="home-mac-mini", node_id="node-1")

        self.assertIn("does not support node-agent credentials", str(raised.exception))
        self.assertIn("luma update manager", str(raised.exception))

    def test_control_client_reports_timeout_without_traceback(self):
        client = ControlClient("https://luma.example.com", "secret")
        with patch("urllib.request.urlopen", side_effect=TimeoutError("read timed out")), self.assertRaises(LumaError) as raised:
            client.deploy(manifest="name: api\nimage: nginx\nregion: cn\nexposure: none\n", source_name="service.yaml", timeout=42)

        self.assertIn("control API timed out after", str(raised.exception))
        self.assertIn("/v1/deployments", str(raised.exception))
        self.assertIn("manager may still be applying", str(raised.exception))

    def test_control_client_build_deploy_sends_provider_manifest_and_env(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.read1.side_effect = [response.read.return_value, b"", response.read.return_value, b""]
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            client.build_deploy(
                provider_id="gitea:lin",
                repository="acme/app",
                build_node="builder",
                manifest="name: api\nregion: cn\nexposure: none\n",
                env_secrets={"DATABASE_URL": "postgres://secret"},
            )

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://luma.example.com/v1/builds")
        self.assertEqual(body["providerId"], "gitea:lin")
        self.assertEqual(body["repository"], "acme/app")
        self.assertEqual(body["manifest"], "name: api\nregion: cn\nexposure: none\n")
        self.assertEqual(body["envSecrets"], {"DATABASE_URL": "postgres://secret"})

    def test_control_client_cancels_repository_import_build(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.read.return_value = b'{"run":{"id":"build-1","status":"canceling"}}'
        response.read1.side_effect = [response.read.return_value, b""]
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            result = client.cancel_build("build-1")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://luma.example.com/v1/builds/build-1/cancel")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {})
        self.assertEqual(result["run"]["status"], "canceling")

    def test_control_client_direct_build_proxy_mode_is_capability_gated_and_explicit(self):
        client = ControlClient("https://luma.example.com", "secret")
        health_response = MagicMock()
        health_response.read.return_value = b'{"capabilities":["build-proxy-mode-v1"]}'
        health_response.read1.side_effect = [health_response.read.return_value, b""]
        health_response.__enter__.return_value = health_response
        build_response = MagicMock()
        build_response.read.return_value = b'{"ok":true}'
        build_response.read1.side_effect = [build_response.read.return_value, b""]
        build_response.__enter__.return_value = build_response

        with patch("urllib.request.urlopen", side_effect=[health_response, build_response]) as urlopen:
            client.build_deploy(repo_url="https://github.com/acme/app", proxy_mode="direct")

        request = urlopen.call_args_list[1].args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["proxyMode"], "direct")
        self.assertIn("proxy", body)
        self.assertEqual(body["proxy"], "")

    def test_control_client_direct_build_proxy_mode_rejects_older_control(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.read.return_value = b'{"capabilities":[]}'
        response.read1.side_effect = [response.read.return_value, b""]
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response), self.assertRaisesRegex(
            LumaError, "update the manager"
        ):
            client.build_deploy(repo_url="https://github.com/acme/app", proxy_mode="direct")

    def test_node_label_waits_longer_than_manager_node_discovery(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.read1.side_effect = [response.read.return_value, b""]
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            client.label_node(node_name="orbstack", region="home", registered_name="mac-mini-home", node_id="node-id")

        timeout = urlopen.call_args.kwargs["timeout"]
        self.assertAlmostEqual(timeout, 120, delta=1)

    def test_secret_set_sends_value_to_control_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.set_secret.return_value = {"name": "DATABASE_URL", "saved": True}
                with patch("luma.cli.common.ControlClient", return_value=client), patch(
                    "luma.cli.session.getpass.getpass", return_value="postgres://secret"
                ), patch("builtins.print") as printed:
                    code = main(["secret", "set", "DATABASE_URL"])
                self.assertEqual(code, 0)
                client.set_secret.assert_called_once_with(name="DATABASE_URL", value="postgres://secret")
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertNotIn("postgres://secret", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_secret_set_can_read_value_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.set_secret.return_value = {"name": "DATABASE_URL", "saved": True}
                with patch("luma.cli.common.ControlClient", return_value=client), patch(
                    "sys.stdin", io.StringIO("postgres://secret\n")
                ), patch("builtins.print") as printed:
                    code = main(["secret", "set", "DATABASE_URL", "--value-stdin"])
                self.assertEqual(code, 0)
                client.set_secret.assert_called_once_with(name="DATABASE_URL", value="postgres://secret")
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertNotIn("postgres://secret", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_secret_set_rejects_value_and_stdin_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                with patch("luma.cli.common.ControlClient") as client_cls:
                    code = main(["secret", "set", "DATABASE_URL", "--value", "a", "--value-stdin"])
                self.assertEqual(code, 1)
                client_cls.return_value.set_secret.assert_not_called()
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)



if __name__ == "__main__":
    unittest.main()
