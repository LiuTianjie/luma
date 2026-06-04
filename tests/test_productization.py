import base64
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import yaml

from luma import __version__
from luma.agent import _agent_executable_args, _systemd_unit, execute_agent_task
from luma.assets import asset_text
from luma.config import LumaConfig
from luma.compose import load_compose_deployment
from luma.cloudflare import delete_dns, sync_control_dns
from luma.bootstrap import (
    _acme_email,
    _ensure_control_image,
    _ensure_control_image_pull_egress,
    _force_update_service_image,
    _is_tailscale_manager_addr,
    _last_command_value,
    _portainer_agent_image_candidates,
    _resolve_control_image,
    _reset_portainer_state,
    _traefik_ports,
    bootstrap_node,
    bootstrap_manager_local,
    deploy_control_stack,
    initialize_portainer,
    install_control_config,
    install_docker,
    refresh_manager_control_local,
    setup_tailscale,
    verify_local_swarm_node,
)
from luma.control.client import ControlClient
from luma.control.context import load_current_context, save_context
from luma.control.server import ControlHandler, _run_host_prep_container, ensure_image_present, ensure_image_pull_egress_proxy, handle_application_restart, handle_compose_deployment, handle_compose_deployment_preview, handle_control_status, handle_dashboard, handle_deployment, handle_deployment_config, handle_deployment_preview, handle_node_agent_complete, handle_node_agent_lease, handle_node_agent_token, handle_node_label, handle_node_register, handle_node_unregister, handle_registry_list, handle_registry_remove, handle_registry_set, handle_secret_list, handle_secret_set, handle_service_remove, handle_storage_apply, handle_storage_list, handle_storage_probe, handle_storage_remove, handle_storage_set, image_pull_requires_egress, resolve_service_image
from luma.control.state import init_state, load_state, save_state
from luma.envfile import load_env_file
from luma.egress import minimal_mihomo_config_from_bytes
from luma.errors import LumaError
from luma.portainer import deploy_with_portainer, remove_luma_portainer_registry, remove_stack, resolve_webhook, upsert_stack
from luma.profiles import PROFILES
from luma.registry import registry_provider_type
from luma.service import load_service
from luma.cli import _node_join_examples, _portainer_url_from_state, _run_with_wait_heartbeat, build_parser, exit_local_node, main
from luma.userconfig import configured_keys, ensure_interactive_config, load_user_config


class ProductConfigTests(unittest.TestCase):
    def test_version_files_stay_in_sync(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        init_file = (root / "luma" / "__init__.py").read_text(encoding="utf-8")
        asset_pyproject = (root / "luma" / "assets" / "pyproject.toml").read_text(encoding="utf-8")

        pyproject_version = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
        init_version = re.search(r'^__version__ = "([^"]+)"$', init_file, re.MULTILINE)
        asset_version = re.search(r'^version = "([^"]+)"$', asset_pyproject, re.MULTILINE)

        self.assertIsNotNone(pyproject_version)
        self.assertIsNotNone(init_version)
        self.assertIsNotNone(asset_version)
        self.assertEqual(pyproject_version.group(1), init_version.group(1))
        self.assertEqual(pyproject_version.group(1), asset_version.group(1))

    def test_pyproject_has_publish_metadata(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('name = "luma-infra"', pyproject)
        self.assertIn('luma = "luma.cli:main"', pyproject)
        self.assertIn("[project.urls]", pyproject)
        self.assertIn('license = "MIT"', pyproject)
        self.assertIn('license-files = ["LICENSE"]', pyproject)

    def test_doctor_checks_control_status_without_legacy_ssh(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "config"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.verify_login.return_value = {"clusterId": "luma-test"}
                client.status.return_value = {
                    "dns": {"ready": False, "missing": ["dns.token"]},
                    "portainer": {"ready": True},
                    "swarm": {"available": True, "nodes": []},
                    "nodes": {"items": [{"name": "manager", "agentStatus": "missing"}]},
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["--no-env", "doctor"])

                self.assertEqual(code, 1)
                client.status.assert_called_once()
                output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("Control status: ok", output)
                self.assertIn("DNS readiness: fail", output)
                self.assertIn("Portainer readiness: ok", output)
                self.assertIn("Swarm availability: ok", output)
                self.assertIn("Registered nodes: ok", output)
                self.assertIn("Node agent heartbeats: ok", output)
                self.assertIn("missing: dns.token", output)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_node_agent_unit_uses_python_module_when_invoked_from_stdin(self):
        with patch.dict(os.environ, {"LUMA_AGENT_EXECUTABLE": ""}, clear=False), patch("shutil.which", return_value=None), patch(
            "sys.argv", ["-"]
        ):
            args = _agent_executable_args(Path("/opt/luma/node-agent/agent.json"))
            unit = _systemd_unit(Path("/opt/luma/node-agent/agent.json"))

        self.assertIn("-m", args)
        self.assertIn("luma.cli", args)
        self.assertNotIn("ExecStart=- ", unit)
        self.assertIn("node-agent run --config /opt/luma/node-agent/agent.json", unit)

    def test_node_agent_can_remove_managed_volume_path(self):
        with patch("luma.agent._run_fixed_host_task") as run:
            result = execute_agent_task(
                {
                    "action": "remove-managed-volume-path",
                    "payload": {"root": "/srv/luma", "relative": "nextcloud/nextcloud-db"},
                }
            )
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn("/srv/luma/nextcloud/nextcloud-db", command)
        self.assertIn("rm -rf", command)
        self.assertEqual(result["path"], "/srv/luma/nextcloud/nextcloud-db")

    def test_node_agent_can_remove_docker_volume(self):
        with patch("luma.agent._run_fixed_host_task") as run:
            result = execute_agent_task({"action": "remove-docker-volume", "payload": {"name": "nextcloud_nextcloud-db"}})
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn('"$docker_cli" volume inspect nextcloud_nextcloud-db', command)
        self.assertIn('"$docker_cli" volume rm -f nextcloud_nextcloud-db', command)
        self.assertFalse(run.call_args.kwargs.get("prefer_container", True))
        self.assertEqual(result["name"], "nextcloud_nextcloud-db")

    def test_node_agent_can_probe_postgres_storage_workload(self):
        with patch("luma.agent._run_fixed_host_task") as run:
            result = execute_agent_task(
                {
                    "action": "probe-storage-class",
                    "payload": {
                        "name": "db-storage",
                        "endpoint": "storage.example:/srv/luma-db",
                        "mountOptions": "nfsvers=4,rw",
                        "workload": "postgres",
                        "probeId": "probe-1",
                    },
                }
            )
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn('"$docker_cli" volume create --driver local', command)
        self.assertIn("--opt type=nfs", command)
        self.assertIn("addr=storage.example,nfsvers=4,rw", command)
        self.assertIn("postgres:16-alpine", command)
        self.assertIn("initdb", command)
        self.assertNotIn("timeout ", command)
        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 300)
        self.assertIn('"$docker_cli" rm -f', run.call_args.kwargs["cleanup_command"])
        self.assertFalse(run.call_args.kwargs.get("prefer_container", True))
        self.assertEqual(result["workload"], "postgres")

    def test_node_agent_can_probe_mysql_storage_workload(self):
        with patch("luma.agent._run_fixed_host_task") as run:
            result = execute_agent_task(
                {
                    "action": "probe-storage-class",
                    "payload": {
                        "name": "db-storage",
                        "endpoint": "storage.example:/srv/luma-db",
                        "workload": "mysql",
                        "probeId": "probe-1",
                    },
                }
            )
        command = run.call_args.args[0]
        self.assertIn("mysql:8", command)
        self.assertIn("mysqld --initialize-insecure", command)
        self.assertNotIn("timeout ", command)
        self.assertEqual(result["workload"], "mysql")

    def test_installer_does_not_change_system_dns(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install-luma.sh").read_text(encoding="utf-8")
        self.assertNotIn("resolvectl dns", installer)
        self.assertNotIn("/etc/systemd/resolved.conf.d/luma.conf", installer)

    def test_new_config_model_reads_nodes_and_provider_dns(self):
        config = LumaConfig(
            {
                "project": "example",
                "providers": {
                    "dns": {"type": "cloudflare", "zone": "example.com"},
                },
                "nodes": {
                    "manager-1": {
                        "host": "manager-1",
                        "publicIp": "203.0.113.10",
                        "region": "cn",
                        "roles": ["swarm-manager", "edge", "egress"],
                    }
                },
            },
            None,
        )
        self.assertEqual(config.project_name, "example")
        self.assertEqual(config.dns["provider"], "cloudflare")
        self.assertEqual(config.default_dns_target(), "203.0.113.10")
        self.assertTrue(config.get_node("manager-1").has_role("edge"))

    def test_control_dns_without_target_is_skipped_with_fix_hint(self):
        old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
        try:
            config = LumaConfig(
                {"providers": {"dns": {"type": "cloudflare", "zone": "example.com", "zoneId": "zone-id"}}},
                None,
            )
            result = sync_control_dns(config, "luma.example.com")
            self.assertIn("Control DNS skipped: missing DNS target", result)
            self.assertIn("luma configure --role manager", result)
        finally:
            _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_delete_dns_removes_matching_cloudflare_record(self):
        with tempfile.TemporaryDirectory() as tmp:
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
            service = load_service(service_path)
            config = LumaConfig({"providers": {"dns": {"type": "cloudflare", "zoneId": "zone-id"}}}, None)
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                client = Mock()
                client.request.side_effect = [{"result": [{"id": "record-1"}]}, {"result": {}}]
                with patch("luma.cloudflare.CloudflareClient", return_value=client):
                    result = delete_dns(config, service)
                self.assertEqual(result, "DNS deleted: api.example.com")
                client.request.assert_any_call("GET", "/zones/zone-id/dns_records?type=A&name=api.example.com")
                client.request.assert_any_call("DELETE", "/zones/zone-id/dns_records/record-1")
            finally:
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_profiles_have_expected_roles(self):
        self.assertIn("edge", PROFILES["single-node"].roles)
        self.assertIn("egress", PROFILES["egress-gateway"].roles)
        self.assertEqual(PROFILES["home-node"].labels["region"], "home")

    def test_external_edge_dns_target_prefers_global_node(self):
        config = LumaConfig(
            {
                "nodes": {
                    "manager-1": {
                        "host": "manager-1",
                        "publicIp": "203.0.113.10",
                        "region": "cn",
                        "roles": ["edge"],
                    },
                    "sg": {
                        "host": "sg",
                        "publicIp": "203.0.113.9",
                        "region": "global",
                        "roles": ["edge"],
                    },
                }
            },
            None,
        )
        self.assertEqual(config.dns_target_for(exposure="external-edge", region="global"), "203.0.113.9")

    def test_traefik_ports_can_be_overridden_for_cloud_hosts(self):
        config = LumaConfig({"defaults": {"ports": {"traefikHttp": 10080, "traefikHttps": 10443}}}, None)
        self.assertEqual(_traefik_ports(config), (10080, 10443))

    def test_acme_email_defaults_from_dns_zone(self):
        config = LumaConfig({"providers": {"dns": {"zone": "example.net"}}}, None)
        self.assertEqual(_acme_email(config), "admin@example.net")

    def test_portainer_agent_image_candidates_include_official_fallback(self):
        config = LumaConfig({}, None)
        candidates = _portainer_agent_image_candidates(config)
        self.assertEqual(candidates[0], "docker.1panel.live/portainer/agent:2.21.5")
        self.assertIn("portainer/agent:2.21.5", candidates)

    def test_portainer_agent_image_override_disables_fallbacks(self):
        config = LumaConfig({"defaults": {"images": {"portainerAgent": "registry.local/agent:test"}}}, None)
        self.assertEqual(_portainer_agent_image_candidates(config), ["registry.local/agent:test"])

    def test_sudo_prompt_is_stripped_from_command_values(self):
        self.assertEqual(_last_command_value("[sudo] password for user: SWMTKN-1-token\n"), "SWMTKN-1-token")

    def test_tailscale_manager_addr_detection(self):
        self.assertTrue(_is_tailscale_manager_addr("100.64.0.1:2377"))
        self.assertTrue(_is_tailscale_manager_addr("100.127.255.254"))
        self.assertFalse(_is_tailscale_manager_addr("100.128.0.1:2377"))
        self.assertFalse(_is_tailscale_manager_addr("203.0.113.10:2377"))

    def test_packaged_core_stack_assets_are_available(self):
        self.assertIn("traefik", asset_text("stacks/core/traefik/stack.yml"))
        self.assertIn("luma-control", asset_text("stacks/core/luma-control/stack.yml"))
        self.assertIn("Luma Status", asset_text("dashboard/index.html"))
        self.assertIn("/v1/dashboard", asset_text("dashboard/app.js"))
        root = Path(__file__).resolve().parents[1]
        self.assertIn('"assets/dashboard/*"', (root / "pyproject.toml").read_text(encoding="utf-8"))

    def test_luma_control_stack_uses_healthcheck_and_start_first_update(self):
        stack = yaml.safe_load(asset_text("stacks/core/luma-control/stack.yml"))
        service = stack["services"]["luma-control"]

        self.assertIn("/v1/health", " ".join(service["healthcheck"]["test"]))
        update_config = service["deploy"]["update_config"]
        self.assertEqual(update_config["order"], "start-first")
        self.assertEqual(update_config["failure_action"], "rollback")
        self.assertEqual(update_config["parallelism"], 1)


class EgressConfigTests(unittest.TestCase):
    def test_egress_config_generation_accepts_base64_subscription(self):
        subscription = yaml.safe_dump(
            {
                "proxies": [
                    {"name": "proxy-a", "type": "trojan", "server": "example.com", "port": 443, "password": "x"}
                ],
                "proxy-groups": [{"name": "old", "type": "select", "proxies": ["proxy-a"]}],
                "rule-providers": {"remote": {"type": "http", "url": "https://example.com/rules"}},
                "rules": ["MATCH,old"],
            },
            allow_unicode=True,
        ).encode()
        generated = yaml.safe_load(minimal_mihomo_config_from_bytes(base64.b64encode(subscription)))
        self.assertEqual(generated["mixed-port"], 7890)
        self.assertEqual(generated["mode"], "rule")
        self.assertEqual(generated["rules"], ["MATCH,EGRESS"])
        self.assertNotIn("rule-providers", generated)
        group = generated["proxy-groups"][0]
        self.assertEqual(group["type"], "url-test")
        self.assertEqual(group["proxies"], ["proxy-a"])
        self.assertEqual(group["url"], "https://www.gstatic.com/generate_204")
        self.assertEqual(group["interval"], 300)


class CliTests(unittest.TestCase):
    def test_last_command_value_preserves_digest_colon_after_sudo_prompt(self):
        output = '[sudo] password for tao: ["ghcr.io/liutianjie/luma-control@sha256:abc123"]\n'

        self.assertEqual(_last_command_value(output), '["ghcr.io/liutianjie/luma-control@sha256:abc123"]')

    def test_init_writes_new_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "luma.yaml"
            code = main(["--config", str(path), "init"])
            self.assertEqual(code, 0)
            data = yaml.safe_load(path.read_text())
            self.assertIn("providers", data)
            self.assertIn("nodes", data)

    def test_parser_exposes_tailscale_connect(self):
        args = build_parser().parse_args(["tailscale", "connect", "manager-1"])
        self.assertEqual(args.command, "tailscale")
        self.assertEqual(args.tailscale_command, "connect")
        self.assertEqual(args.node, "manager-1")

    def test_repair_commands_default_to_local_server(self):
        for argv in (["tailscale", "connect"], ["portainer", "setup"], ["egress", "setup"]):
            args = build_parser().parse_args(argv)
            self.assertIsNone(args.node)

    def test_parser_exposes_preflight(self):
        args = build_parser().parse_args(["preflight"])
        self.assertEqual(args.command, "preflight")

    def test_parser_exposes_configure(self):
        args = build_parser().parse_args(["configure", "--role", "worker"])
        self.assertEqual(args.command, "configure")
        self.assertEqual(args.role, "worker")

    def test_bootstrap_supports_skip_egress(self):
        args = build_parser().parse_args(["node", "bootstrap", "manager-1", "--profile", "single-node", "--skip-egress"])
        self.assertEqual(args.command, "node")
        self.assertEqual(args.node_command, "bootstrap")
        self.assertTrue(args.skip_egress)

    def test_bootstrap_manager_supports_public_port_overrides(self):
        args = build_parser().parse_args(
            ["bootstrap", "manager", "--domain", "luma.example.com", "--http-port", "10080", "--https-port", "10443"]
        )
        self.assertEqual(args.command, "bootstrap")
        self.assertEqual(args.bootstrap_command, "manager")
        self.assertEqual(args.http_port, 10080)
        self.assertEqual(args.https_port, 10443)

    def test_deploy_defaults_to_portainer_control_plane(self):
        args = build_parser().parse_args(["deploy", "app.yaml"])
        self.assertEqual(args.command, "deploy")
        self.assertEqual(args.via, "portainer")
        self.assertEqual(args.timeout, 1800)

    def test_service_remove_parser_defaults_to_full_cleanup(self):
        args = build_parser().parse_args(["service", "remove", "app.yaml"])
        self.assertEqual(args.command, "service")
        self.assertEqual(args.service_command, "remove")
        self.assertFalse(args.skip_dns)
        self.assertFalse(args.skip_portainer)
        self.assertFalse(args.delete_storage)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.timeout, 300)
        args = build_parser().parse_args(["service", "remove", "app", "--delete-storage"])
        self.assertTrue(args.delete_storage)

    def test_compose_and_storage_parsers_accept_planned_commands(self):
        args = build_parser().parse_args(["compose", "deploy", "luma.compose.yml", "--dry-run"])
        self.assertEqual(args.command, "compose")
        self.assertEqual(args.compose_command, "deploy")
        self.assertTrue(args.dry_run)
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
                "--workload",
                "filesystem",
                "--workload",
                "postgres",
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
        self.assertEqual(args.workloads, ["filesystem", "postgres"])
        self.assertFalse(args.external)
        self.assertEqual(args.control_url, "https://luma.example.com")
        args = build_parser().parse_args(
            [
                "storage",
                "probe",
                "home-nfs",
                "--workload",
                "postgres",
                "--node",
                "home-mac-mini",
                "--timeout",
                "600",
            ]
        )
        self.assertEqual(args.storage_command, "probe")
        self.assertEqual(args.name, "home-nfs")
        self.assertEqual(args.workload, "postgres")
        self.assertEqual(args.node, "home-mac-mini")
        self.assertEqual(args.timeout, 600)
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

    def test_storage_set_command_validates_new_shape_before_control_call(self):
        cases = (
            ["storage", "set", "home-nfs", "--node", "home-nas"],
            ["storage", "set", "home-nfs", "--node", "home-nas", "--path", "/srv/luma", "--endpoint", "home-nas:/srv/luma"],
            ["storage", "set", "company-nfs", "--external", "--endpoint", "nfs.example.com:/srv/luma"],
        )
        for argv in cases:
            with self.subTest(argv=argv), patch("luma.cli.ControlClient") as client_cls, patch("builtins.print"):
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
                with patch("luma.cli.ControlClient") as client_cls, patch("builtins.print"):
                    code = main(["service", "remove", str(service_path), "--timeout", "12", "--dry-run"])
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
                    "portainer": "Portainer stack would be removed: api",
                    "generatedFiles": "Generated files would be removed: /opt/luma/stacks/cn/api",
                    "steps": [
                        {"name": "Remove Portainer stack", "status": "ok", "message": "Portainer stack would be removed: api"},
                    ],
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["service", "remove", "api", "--dry-run"])
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
                client.deploy_events.side_effect = LumaError("control API error 404: not found")
                client.deploy.return_value = {
                    "service": "api",
                    "image": {"selected": "nginx:alpine"},
                    "webhook": "Portainer stack updated for api: api",
                    "steps": [
                        {"name": "Sync DNS", "status": "ok", "message": "DNS skipped: service is not public"},
                        {"name": "Deploy Portainer stack", "status": "ok", "message": "Portainer stack updated for api: api"},
                    ],
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["deploy", str(service_path), "--timeout", "42"])
                self.assertEqual(code, 0)
                client.deploy.assert_called_once()
                self.assertEqual(client.deploy.call_args.kwargs["timeout"], 42)
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("[start] Load deploy context", printed_text)
                self.assertIn("[start] Waiting for control plane response (timeout 42s)", printed_text)
                self.assertIn("[ok] Sync DNS: DNS skipped: service is not public", printed_text)
                self.assertIn("[ok] Deploy Portainer stack: Portainer stack updated for api: api", printed_text)
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
                client.deploy_events.return_value = iter(
                    [
                        {"name": "Resolve image", "status": "start", "message": "started"},
                        {"name": "Resolve image", "status": "ok", "message": "nginx:alpine"},
                        {"status": "done", "result": {"service": "api", "image": {"selected": "nginx:alpine"}}},
                    ]
                )
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
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
                client.deploy_compose_events.side_effect = LumaError("control API error 404: not found")
                client.deploy_compose.return_value = {"deployment": "app-stack", "steps": []}
                with patch("luma.cli.ControlClient", return_value=client):
                    code = main(["compose", "deploy", str(sidecar_path), "--timeout", "12"])
                self.assertEqual(code, 0)
                client.deploy_compose.assert_called_once()
                kwargs = client.deploy_compose.call_args.kwargs
                self.assertIn("app-stack", kwargs["manifest"])
                self.assertIn("nginx:alpine", kwargs["compose_content"])
                self.assertEqual(kwargs["timeout"], 12)
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
                client.deploy_events.return_value = iter(
                    [
                        {"name": "Resolve image", "status": "start", "message": "started"},
                        {"name": "Resolve image", "status": "ok", "message": "nginx:alpine"},
                        {"status": "done", "result": {"service": "api", "image": {"selected": "nginx:alpine"}}},
                    ]
                )
                with patch("luma.cli.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
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
            with patch("luma.cli.ControlClient") as client_cls, patch("builtins.print") as printed:
                code = main(["deploy", str(service_path), "--dry-run", "--format", "json"])

        self.assertEqual(code, 0)
        client_cls.assert_not_called()
        payload = json.loads(printed.call_args_list[-1].args[0])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["result"]["dryRun"])
        self.assertEqual(payload["result"]["service"]["name"], "api")
        self.assertEqual(payload["result"]["artifacts"][0]["kind"], "stack")

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

    def test_depoly_aliases_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
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
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                code = main(["depoly", str(service_path)])
                self.assertEqual(code, 1)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_login_writes_context_without_printing_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            client = Mock()
            client.verify_login.return_value = {"clusterId": "luma-test"}
            try:
                with patch("luma.cli.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
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

    def test_configure_writes_user_config_without_printing_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "")
            old_target = _set_env("LUMA_DNS_EDGE_TARGET", "")
            try:
                secret_values = iter(["cf-token", "ts-key", "sub-url", "sudo-pass"])
                with patch("luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)), patch(
                    "builtins.input", return_value="ops@example.com"
                ), patch("builtins.print") as printed:
                    code = main(["configure", "--role", "manager"])
                self.assertEqual(code, 0)
                self.assertTrue(config_path.exists())
                keys = configured_keys(config_path)
                self.assertIn("CLOUDFLARE_API_TOKEN", keys)
                self.assertIn("LUMA_DNS_EDGE_TARGET", keys)
                self.assertIn("TRAEFIK_ACME_EMAIL", keys)
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertNotIn("cf-token", printed_text)
                self.assertNotIn("sudo-pass", printed_text)
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)
                _restore_env("LUMA_DNS_EDGE_TARGET", old_target)

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
                    "luma.cli.bootstrap_manager_local", side_effect=bootstrap_side_effect
                ), patch(
                    "luma.cli.find_zone", return_value={"id": "zone-example"}
                ), patch("builtins.print") as printed:
                    code = main(
                        [
                            "--config",
                            str(project_config),
                            "bootstrap",
                            "manager",
                            "--domain",
                            "luma.example.com",
                            "--profile",
                            "single-node",
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
                self.assertIn("Portainer URL: https://203.0.113.10:9443", printed_text)
                self.assertIn("Portainer username: admin", printed_text)
                self.assertIn("Portainer password: sudo jq", printed_text)
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
                        "providers": {"portainer": {"webhookUrlEnv": "PORTAINER_WEBHOOK_URL"}},
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
                    if zone_name == "itool.tech":
                        return {"id": "zone-itool"}
                    raise LumaError("not found")

                with patch("luma.cli.find_zone", side_effect=find_zone_side_effect), patch(
                    "luma.cli.bootstrap_manager_local", side_effect=bootstrap_side_effect
                ), patch("builtins.print"):
                    code = main(
                        [
                            "--config",
                            str(config_path),
                            "bootstrap",
                            "manager",
                            "--domain",
                            "luma.itool.tech",
                            "--node",
                            "manager",
                            "--skip-egress",
                        ]
                    )
                self.assertEqual(code, 0)
                dns = captured["config"]["providers"]["dns"]
                self.assertEqual(dns["type"], "cloudflare")
                self.assertEqual(dns["zone"], "itool.tech")
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

    def test_portainer_url_from_state_strips_api_suffix(self):
        self.assertEqual(
            _portainer_url_from_state({"portainerApiUrl": "https://203.0.113.10:9443/api"}),
            "https://203.0.113.10:9443",
        )

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
        with patch("luma.cli._run_luma_installer") as installer, patch("luma.cli._reexec_after_luma_update") as reexec, patch(
            "luma.cli._existing_control_state", return_value=state
        ), patch(
            "luma.cli.refresh_manager_control_local", return_value=["Control refreshed"]
        ) as refresh:
            code = main(
                [
                    "update",
                    "manager",
                    "--domain",
                    "luma.example.com",
                    "--profile",
                    "single-node",
                    "--install-ref",
                    "main",
                ]
            )

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref="main")
        reexec.assert_called_once()
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[2], "luma.example.com")
        self.assertIs(refresh.call_args.args[3], state)

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
            state = {"clusterId": "luma-test", "domain": "luma.itool.tech", "deployToken": "deploy", "joinToken": "join"}
            old_cf = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            old_target = _set_env("LUMA_DNS_EDGE_TARGET", "")
            try:
                captured = {}

                def refresh_side_effect(config, _node, _domain, current_state, **_kwargs):
                    captured["config"] = config.raw
                    captured["state"] = dict(current_state)
                    return ["Control refreshed"]

                def find_zone_side_effect(_config, zone_name):
                    if zone_name == "itool.tech":
                        return {"id": "zone-itool"}
                    raise LumaError("not found")

                with patch("luma.cli._run_luma_installer"), patch("luma.cli._reexec_after_luma_update"), patch(
                    "luma.cli._existing_control_state", return_value=state
                ), patch("luma.cli.find_zone", side_effect=find_zone_side_effect), patch(
                    "luma.cli.refresh_manager_control_local", side_effect=refresh_side_effect
                ), patch("builtins.print"):
                    code = main(
                        [
                            "--config",
                            str(config_path),
                            "update",
                            "manager",
                            "--domain",
                            "luma.itool.tech",
                            "--node",
                            "manager",
                        ]
                    )

                self.assertEqual(code, 0)
                dns = captured["config"]["providers"]["dns"]
                self.assertEqual(dns["type"], "cloudflare")
                self.assertEqual(dns["zone"], "itool.tech")
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
                with patch("luma.cli._run_luma_installer") as installer, patch("luma.cli._reexec_after_luma_update"), patch(
                    "luma.cli.refresh_manager_control_local", return_value=["Control refreshed"]
                ) as refresh:
                    code = main(["update"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None)
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[2], "luma.example.com")

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
                with patch("luma.cli._run_luma_installer") as installer, patch("luma.cli._reexec_after_luma_update"), patch(
                    "luma.cli.refresh_manager_control_local", return_value=["Control refreshed"]
                ) as refresh, patch("builtins.print") as printed:
                    code = main(["update"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None)
        refresh.assert_called_once()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Manager control-plane refresh required", printed_text)
        self.assertIn("local manager control state found", printed_text)

    def test_update_joined_node_skips_agent_refresh_when_control_is_too_old(self):
        with patch("luma.cli._run_luma_installer") as installer, patch("luma.cli._reexec_after_luma_update"), patch(
            "luma.cli._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch("luma.cli._local_agent_config", return_value=None), patch("luma.cli._safe_local_docker_node_id", return_value="node-1"), patch(
            "luma.cli._refresh_local_node_agent",
            side_effect=LumaError("control API does not support node-agent credentials yet. Update the manager control plane first."),
        ), patch("builtins.print") as printed:
            code = main(["update"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("[info] Role: joined node", printed_text)
        self.assertIn("[skip] Luma node agent skipped", printed_text)
        self.assertIn("[ok] Joined node update complete", printed_text)

    def test_update_after_reexec_skips_installer_and_refreshes_manager(self):
        old_reexec = _set_env("LUMA_UPDATE_REEXECED", "1")
        try:
            with patch("luma.cli._run_luma_installer") as installer, patch(
                "luma.cli._manager_refresh_decision", return_value=(True, "local manager control state found")
            ), patch("luma.cli._refresh_manager_control") as refresh, patch("luma.cli._try_refresh_manager_agent"), patch("builtins.print") as printed:
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
        with patch("luma.cli._existing_control_state", return_value=None), patch(
            "luma.cli._run_luma_installer"
        ) as installer, patch("luma.cli._reexec_after_luma_update"), patch("luma.cli.refresh_manager_control_local") as refresh, patch("builtins.print") as printed:
            code = main(["update"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None)
        refresh.assert_not_called()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("CLI updated", printed_text)
        self.assertIn("Manager control-plane refresh skipped", printed_text)

    def test_update_manager_rejects_bootstrap_only_options(self):
        with patch("luma.cli._run_luma_installer"), patch("luma.cli._reexec_after_luma_update"), patch(
            "luma.cli._existing_control_state", return_value={"domain": "luma.example.com"}
        ), patch("luma.cli.refresh_manager_control_local") as refresh:
            code = main(["update", "manager", "--domain", "luma.example.com", "--http-port", "8080"])

        self.assertEqual(code, 1)
        refresh.assert_not_called()

    def test_update_manager_does_not_call_full_bootstrap_paths(self):
        state = {"clusterId": "luma-test", "domain": "luma.example.com", "deployToken": "deploy", "joinToken": "join"}
        with patch("luma.cli._run_luma_installer"), patch("luma.cli._reexec_after_luma_update"), patch("luma.cli._existing_control_state", return_value=state), patch(
            "luma.cli.refresh_manager_control_local", return_value=["Control refreshed"]
        ), patch("luma.cli.bootstrap_manager_local") as bootstrap_manager, patch("luma.cli.bootstrap_node") as bootstrap_node_call, patch(
            "luma.cli.install_docker"
        ) as docker, patch("luma.cli.setup_egress") as egress:
            code = main(["update", "manager", "--domain", "luma.example.com"])

        self.assertEqual(code, 0)
        bootstrap_manager.assert_not_called()
        bootstrap_node_call.assert_not_called()
        docker.assert_not_called()
        egress.assert_not_called()

    def test_version_local_skips_control_check(self):
        with patch("luma.cli.ControlClient") as client_cls, patch("builtins.print") as printed:
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
        with patch("luma.cli.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
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
                    "portainer": {
                        "apiUrl": "https://100.64.0.1:9443/api",
                        "endpointIdConfigured": True,
                        "swarmIdConfigured": True,
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
                            {"name": "docker-home", "region": "home", "status": "labeled", "displayName": "mini-gaojiu"},
                        ],
                    },
                    "swarm": {
                        "available": True,
                        "nodes": [
                            {
                                "hostname": "manager",
                                "role": "manager",
                                "state": "ready",
                                "availability": "active",
                                "region": "cn",
                                "leader": True,
                            },
                            {
                                "hostname": "docker-home",
                                "role": "worker",
                                "state": "ready",
                                "availability": "active",
                                "region": "home",
                                "leader": False,
                            },
                        ],
                    },
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
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
        self.assertIn("Portainer", printed_text)
        self.assertIn("Storage", printed_text)
        self.assertIn("Summary: storageClasses=1", printed_text)
        self.assertIn("cn-nfs", printed_text)
        self.assertIn("/srv/luma", printed_text)
        self.assertIn("Nodes", printed_text)
        self.assertIn("Summary: registered=2, swarm=2", printed_text)
        self.assertIn("NAME         REGION", printed_text)
        self.assertIn("docker-home  home    labeled     ready  worker", printed_text)
        self.assertIn("manager      cn      labeled     ready  manager", printed_text)

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
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
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
                with patch("luma.cli.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
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
                with patch("luma.cli.ControlClient", return_value=client) as client_cls, patch("builtins.print"):
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
            with patch("luma.cli.ControlClient") as client_cls, patch("builtins.print") as printed:
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
                with patch("luma.cli.ControlClient", side_effect=[secret_client, registry_client]) as client_cls, patch(
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
        for call in client_cls.call_args_list:
            self.assertEqual(call.args[:2], ("https://luma.example.com", "deploy-token"))
        payloads = [json.loads(call.args[0]) for call in printed.call_args_list]
        self.assertEqual(payloads[0]["result"]["secrets"], ["DATABASE_URL"])
        self.assertEqual(payloads[1]["result"]["registries"][0]["host"], "ghcr.io")

    def test_node_exit_cleans_local_swarm_and_runtime_state(self):
        remote = Mock()
        remote.sudo_result.return_value = Mock(code=0, output="left\n")
        remote.sudo.return_value = ""

        with patch("luma.cli.LocalExecutor", return_value=remote):
            results = exit_local_node()

        self.assertEqual(results, ["Swarm left", "Removed /opt/luma"])
        self.assertIn("docker swarm leave --force", remote.sudo_result.call_args.args[0])
        remote.sudo.assert_called_once_with("rm -rf /opt/luma")

    def test_node_exit_optional_deep_cleanup(self):
        remote = Mock()
        remote.sudo_result.side_effect = [
            Mock(code=0, output="skipped\n"),
            Mock(code=0, output="done\n"),
            Mock(code=0, output="done\n"),
        ]
        remote.sudo.return_value = ""

        with patch("luma.cli.LocalExecutor", return_value=remote):
            results = exit_local_node(tailscale=True, prune_docker=True)

        self.assertEqual(
            results,
            ["Swarm leave skipped", "Removed /opt/luma", "Tailscale logged out", "Docker pruned"],
        )
        commands = [call.args[0] for call in remote.sudo_result.call_args_list]
        self.assertTrue(any("tailscale logout" in command for command in commands))
        self.assertTrue(any("docker system prune -af --volumes" in command for command in commands))

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
                    "managerAddr": "100.64.0.1:2377",
                    "swarmJoinToken": "swarm-token",
                }
                client.label_node.return_value = {"message": "labels applied"}
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)
                ), patch("luma.cli.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.install_docker", return_value="Docker available"
                ), patch(
                    "luma.cli.ControlClient", return_value=client
                ), patch("luma.cli.join_local_node", return_value=[]), patch(
                    "luma.cli.local_docker_node_name", return_value="worker-1"
                ), patch(
                    "luma.cli.local_docker_node_id", return_value="node-id-1"
                ), patch(
                    "luma.cli._local_tailscale_ip", return_value="100.64.0.10"
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
                self.assertEqual(code, 0)
                client.register_node.assert_called_once_with(node_name="global-sg-1", region="global")
                client.label_node.assert_called_once_with(
                    node_name="worker-1",
                    region="global",
                    registered_name="global-sg-1",
                    node_id="node-id-1",
                    tailscale_ip="100.64.0.10",
                )
                self.assertIn("TAILSCALE_AUTHKEY", configured_keys(config_path))
                self.assertIn("LUMA_SUDO_PASSWORD", configured_keys(config_path))
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_verify_local_swarm_node_rejects_error_state(self):
        remote = Mock()
        remote.run_result.return_value = Mock(
            code=0,
            output=(
                "state=error nodeID= error=rpc error: code = DeadlineExceeded "
                "desc = context deadline exceeded while waiting for connections to become ready\n"
            ),
        )

        with self.assertRaisesRegex(LumaError, "Docker Swarm joined locally but is not healthy"):
            verify_local_swarm_node(remote)

    def test_verify_local_swarm_node_requires_node_id(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(code=0, output="state=active nodeID= error=\n"),
            Mock(code=0, output="state=active nodeID= error=\n"),
        ]

        with patch("luma.bootstrap.time.monotonic", side_effect=[0, 3, 30]), patch("luma.bootstrap.time.sleep"):
            with self.assertRaisesRegex(LumaError, "did not produce an active local node"):
                verify_local_swarm_node(remote)

    def test_home_node_join_requires_tailscale_key_when_disconnected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", return_value=""
                ), patch("luma.cli._local_tailscale_connected", return_value=False), patch(
                    "luma.cli.ControlClient"
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
                    "managerAddr": "100.64.0.1:2377",
                    "swarmJoinToken": "swarm-token",
                }
                client.label_node.return_value = {"message": "labels applied"}
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)
                ), patch("luma.cli._local_tailscale_connected", side_effect=[False, False]), patch(
                    "luma.cli.configure_dns", return_value="DNS ok"
                ), patch("luma.cli.install_docker", return_value="Docker available"
                ), patch("luma.cli.ControlClient", return_value=client), patch(
                    "luma.cli.join_local_node", return_value=[]
                ), patch("luma.cli.local_docker_node_name", return_value="docker-home"), patch(
                    "luma.cli.local_docker_node_id", return_value="home-id-1"
                ), patch(
                    "luma.cli._local_tailscale_ip", return_value="100.64.0.20"
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
                self.assertEqual(code, 0)
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
                ), patch("luma.cli.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.install_docker", side_effect=LumaError("Docker is not ready")
                ), patch("luma.cli.ControlClient", return_value=client), patch("builtins.print"):
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

    def test_node_join_unregisters_when_local_swarm_join_fails(self):
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
                    "managerAddr": "100.64.0.1:2377",
                    "swarmJoinToken": "swarm-token",
                }
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", return_value="sudo-pass"
                ), patch("luma.cli.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.install_docker", return_value="Docker available"
                ), patch("luma.cli.ControlClient", return_value=client), patch(
                    "luma.cli.join_local_node", side_effect=LumaError("swarm join failed")
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

    def test_node_join_rejects_legacy_profile_argument(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.print"):
            code = main(
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
        self.assertEqual(code, 1)

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
                code = main(["deploy", str(service_path), "--skip-dns", "--skip-webhook"])
                self.assertEqual(code, 1)
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

    def test_control_client_reports_legacy_node_api_during_region_first_join(self):
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

        self.assertIn("control API is older than this CLI", str(raised.exception))
        self.assertIn("luma update", str(raised.exception))

    def test_control_client_reports_legacy_storage_api_for_simplified_storage_set(self):
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

        self.assertIn("control API is older than this CLI", str(raised.exception))
        self.assertIn("storage endpoints for managed NFS", str(raised.exception))
        self.assertIn("luma update manager", str(raised.exception))

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

        self.assertIn("control API timed out after 42s", str(raised.exception))
        self.assertIn("/v1/deployments", str(raised.exception))
        self.assertIn("manager may still be applying", str(raised.exception))

    def test_node_label_waits_longer_than_manager_node_discovery(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            client.label_node(node_name="orbstack", region="home", registered_name="mac-mini-gaojiu", node_id="node-id")

        timeout = urlopen.call_args.kwargs["timeout"]
        self.assertGreaterEqual(timeout, 120)

    def test_control_client_sends_storage_probe_timeout_in_body(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            client.probe_storage(name="cn-nfs", workload="postgres", node="home-mac-mini", timeout=180)

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["timeout"], 180)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 240)

    def test_secret_set_sends_value_to_control_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.set_secret.return_value = {"name": "DATABASE_URL", "saved": True}
                with patch("luma.cli.ControlClient", return_value=client), patch(
                    "luma.cli.getpass.getpass", return_value="postgres://secret"
                ), patch("builtins.print") as printed:
                    code = main(["secret", "set", "DATABASE_URL"])
                self.assertEqual(code, 0)
                client.set_secret.assert_called_once_with(name="DATABASE_URL", value="postgres://secret")
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertNotIn("postgres://secret", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)


class EnvFileTests(unittest.TestCase):
    def test_load_env_file_preserves_existing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "LUMA_TEST_VALUE=from-file",
                        "export LUMA_QUOTED='hello world'",
                        "LUMA_EMPTY=",
                    ]
                ),
                encoding="utf-8",
            )
            import os

            old_value = os.environ.get("LUMA_TEST_VALUE")
            old_quoted = os.environ.get("LUMA_QUOTED")
            old_empty = os.environ.get("LUMA_EMPTY")
            try:
                os.environ["LUMA_TEST_VALUE"] = "from-env"
                loaded = load_env_file(path)
                self.assertEqual(os.environ["LUMA_TEST_VALUE"], "from-env")
                self.assertEqual(os.environ["LUMA_QUOTED"], "hello world")
                self.assertEqual(os.environ["LUMA_EMPTY"], "")
                self.assertNotIn("LUMA_TEST_VALUE", loaded)
            finally:
                _restore_env("LUMA_TEST_VALUE", old_value)
                _restore_env("LUMA_QUOTED", old_quoted)
                _restore_env("LUMA_EMPTY", old_empty)


class PortainerWebhookTests(unittest.TestCase):
    def test_initialize_portainer_creates_local_endpoint_when_empty(self):
        state = {
            "portainerApiUrl": "https://127.0.0.1:9443/api",
            "portainerAdminUsername": "admin",
            "portainerAdminPassword": "secret",
        }
        with patch(
            "luma.bootstrap._portainer_request",
            side_effect=[
                (200, {}),
                (200, {"jwt": "jwt-token"}),
                (200, []),
            ],
        ) as request, patch(
            "luma.bootstrap._portainer_form_request",
            return_value=(200, {"Id": 7, "Name": "luma-local"}),
        ) as form_request:
            result = initialize_portainer(Mock(), state)
        self.assertEqual(result, "Portainer initialized")
        self.assertEqual(state["portainerEndpointId"], 7)
        self.assertEqual(state["portainerEndpointName"], "luma-local")
        form_request.assert_called_once_with(
            "https://127.0.0.1:9443/api",
            "POST",
            "/endpoints",
            {
                "Name": "luma-local",
                "EndpointCreationType": "2",
                "URL": "tcp://tasks.agent:9001",
                "TLS": "true",
                "TLSSkipVerify": "true",
                "TLSSkipClientVerify": "true",
            },
            token="jwt-token",
        )
        self.assertEqual(request.call_count, 3)

    def test_initialize_portainer_uses_env_password_override(self):
        state = {
            "portainerApiUrl": "https://127.0.0.1:9443/api",
            "portainerAdminUsername": "admin",
            "portainerAdminPassword": "stale",
        }
        with patch.dict(os.environ, {"LUMA_PORTAINER_ADMIN_PASSWORD": "current"}), patch(
            "luma.bootstrap._portainer_request",
            side_effect=[
                (200, {}),
                (200, {"jwt": "jwt-token"}),
                (200, [{"Id": 7, "Name": "luma-local"}]),
            ],
        ) as request:
            result = initialize_portainer(Mock(), state)
        self.assertEqual(result, "Portainer initialized")
        self.assertEqual(state["portainerAdminPassword"], "current")
        request.assert_any_call(
            "https://127.0.0.1:9443/api",
            "POST",
            "/auth",
            {"Username": "admin", "Password": "current"},
        )

    def test_registry_auth_bypasses_webhook_and_uses_portainer_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "private-api",
                        "image": "ghcr.io/acme/private-api:1",
                        "region": "cn",
                        "exposure": "none",
                        "portainer": {"webhookUrl": "https://portainer.example.com/hook"},
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({}, None)
            state = {
                "portainerApiUrl": "https://portainer.example.com/api",
                "portainerAdminUsername": "admin",
                "portainerAdminPassword": "secret",
                "portainerEndpointId": 1,
                "swarmId": "swarm-test",
            }
            registry_auth = {"username": "octo", "password": "ghp_secret", "serveraddress": "ghcr.io"}
            with patch("luma.portainer.upsert_stack", return_value="Portainer stack updated") as upsert, patch(
                "luma.portainer.trigger_webhook_url"
            ) as webhook:
                result = deploy_with_portainer(config, service, "services: {}", state, registry_auth=registry_auth)
            self.assertEqual(result, "Portainer stack updated")
            upsert.assert_called_once()
            self.assertEqual(upsert.call_args.kwargs["registry_auth"], registry_auth)
            webhook.assert_not_called()

    def test_latest_image_bypasses_webhook_and_uses_portainer_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:latest",
                        "region": "cn",
                        "exposure": "none",
                        "portainer": {"webhookUrl": "https://portainer.example.com/hook"},
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({}, None)
            state = {
                "portainerApiUrl": "https://portainer.example.com/api",
                "portainerAdminUsername": "admin",
                "portainerAdminPassword": "secret",
                "portainerEndpointId": 1,
                "swarmId": "swarm-test",
            }
            with patch("luma.portainer.upsert_stack", return_value="Portainer stack updated") as upsert, patch(
                "luma.portainer.trigger_webhook_url"
            ) as webhook:
                result = deploy_with_portainer(config, service, "services: {}", state)
            self.assertEqual(result, "Portainer stack updated")
            upsert.assert_called_once()
            webhook.assert_not_called()

    def test_digest_image_bypasses_webhook_and_uses_portainer_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx@sha256:abc123",
                        "region": "cn",
                        "exposure": "none",
                        "portainer": {"webhookUrl": "https://portainer.example.com/hook"},
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({}, None)
            state = {
                "portainerApiUrl": "https://portainer.example.com/api",
                "portainerAdminUsername": "admin",
                "portainerAdminPassword": "secret",
                "portainerEndpointId": 1,
                "swarmId": "swarm-test",
            }
            with patch("luma.portainer.upsert_stack", return_value="Portainer stack updated") as upsert, patch(
                "luma.portainer.trigger_webhook_url"
            ) as webhook:
                result = deploy_with_portainer(config, service, "services: {}", state)
            self.assertEqual(result, "Portainer stack updated")
            upsert.assert_called_once()
            webhook.assert_not_called()

    def test_bootstrap_manager_saves_portainer_credentials_before_dns(self):
        config = LumaConfig(
            {
                "nodes": {
                    "manager": {
                        "host": "localhost",
                        "publicIp": "127.0.0.1",
                        "roles": ["swarm-manager", "edge"],
                    }
                }
            },
            None,
        )
        node = config.get_node("manager")
        state = {"clusterId": "luma-test", "domain": "luma.example.com"}
        sequence = []
        saved_states = []

        def bind_portainer(_remote, current_state):
            current_state["portainerAdminUsername"] = "admin"
            current_state["portainerAdminPassword"] = "secret"
            return "Portainer initialized"

        def save_state(_remote, current_state):
            sequence.append("save")
            saved_states.append(dict(current_state))
            return "Secret written: /opt/luma/control/control.json"

        def sync_dns(_config, _domain):
            sequence.append("sync-dns")
            return "Control DNS synced"

        with patch("luma.bootstrap.bootstrap_node", return_value=["Bootstrap node complete"]) as bootstrap, patch(
            "luma.bootstrap.initialize_portainer", side_effect=bind_portainer
        ), patch("luma.bootstrap.install_control_state", side_effect=save_state), patch(
            "luma.bootstrap.local_swarm_join_info",
            return_value={"managerAddr": "100.64.0.1:2377", "swarmJoinToken": "token", "swarmId": "swarm"},
        ), patch("luma.bootstrap.sync_control_dns", side_effect=sync_dns), patch(
            "luma.bootstrap.install_control_config", return_value="Config installed"
        ), patch("luma.bootstrap.deploy_control_stack", return_value="Control deployed"):
            bootstrap_manager_local(config, node, PROFILES["single-node"], "luma.example.com", state)

        self.assertEqual(sequence[:2], ["save", "sync-dns"])
        self.assertGreaterEqual(len(saved_states), 1)
        self.assertEqual(saved_states[0]["portainerAdminPassword"], "secret")
        self.assertEqual(saved_states[-1]["portainerApiUrl"], "https://100.64.0.1:9443/api")
        self.assertFalse(bootstrap.call_args.kwargs["reset_portainer_state"])

    def test_bootstrap_manager_recreates_portainer_after_bind_failure(self):
        config = LumaConfig(
            {
                "nodes": {
                    "manager": {
                        "host": "localhost",
                        "publicIp": "127.0.0.1",
                        "roles": ["swarm-manager", "edge"],
                    }
                }
            },
            None,
        )
        node = config.get_node("manager")
        state = {"clusterId": "luma-test", "domain": "luma.example.com"}
        bind_calls = []

        def bind_portainer(_remote, current_state):
            bind_calls.append("bind")
            if len(bind_calls) == 1:
                current_state["portainerAdminPassword"] = "secret"
                raise LumaError("Portainer endpoint discovery failed: HTTP 500 broken endpoint")
            current_state["portainerEndpointId"] = 7
            return "Portainer initialized"

        with patch("luma.bootstrap.bootstrap_node", return_value=["Bootstrap node complete"]), patch(
            "luma.bootstrap.initialize_portainer", side_effect=bind_portainer
        ), patch("luma.bootstrap._reset_portainer_state", return_value="Portainer state reset") as reset, patch(
            "luma.bootstrap._deploy_portainer", return_value=["Portainer redeployed"]
        ) as redeploy, patch(
            "luma.bootstrap.install_control_state", return_value="Secret written: /opt/luma/control/control.json"
        ), patch(
            "luma.bootstrap.local_swarm_join_info",
            return_value={"managerAddr": "127.0.0.1:2377", "swarmJoinToken": "token", "swarmId": "swarm"},
        ), patch("luma.bootstrap.sync_control_dns", return_value="Control DNS synced"), patch(
            "luma.bootstrap.install_control_config", return_value="Config installed"
        ), patch("luma.bootstrap.deploy_control_stack", return_value="Control deployed"):
            bootstrap_manager_local(config, node, PROFILES["single-node"], "luma.example.com", state)

        self.assertEqual(len(bind_calls), 2)
        reset.assert_called_once()
        redeploy.assert_called_once()
        self.assertEqual(state["portainerAdminPassword"], "secret")
        self.assertEqual(state["portainerEndpointId"], 7)

    def test_bootstrap_node_can_reset_portainer_before_deploy(self):
        config = LumaConfig(
            {
                "nodes": {
                    "manager": {
                        "host": "localhost",
                        "publicIp": "127.0.0.1",
                        "roles": ["swarm-manager", "edge"],
                    }
                }
            },
            None,
        )
        node = config.get_node("manager")
        sequence = []

        def mark(name):
            def inner(*_args, **_kwargs):
                sequence.append(name)
                return name

            return inner

        with patch("luma.bootstrap.configure_dns", side_effect=mark("dns")), patch(
            "luma.bootstrap.install_docker", side_effect=mark("docker")
        ), patch("luma.bootstrap.setup_tailscale", side_effect=mark("tailscale")), patch(
            "luma.bootstrap.ensure_swarm", side_effect=mark("swarm")
        ), patch("luma.bootstrap.ensure_networks", side_effect=mark("networks")), patch(
            "luma.bootstrap.apply_labels", side_effect=mark("labels")
        ), patch("luma.bootstrap.prepare_paths", side_effect=mark("paths")), patch(
            "luma.bootstrap.configure_firewall", side_effect=mark("firewall")
        ), patch("luma.bootstrap._deploy_traefik", side_effect=mark("traefik")), patch(
            "luma.bootstrap._reset_portainer_state", side_effect=mark("reset-portainer")
        ), patch("luma.bootstrap._deploy_portainer", side_effect=lambda *_args, **_kwargs: [mark("portainer")()]):
            bootstrap_node(
                config,
                node,
                PROFILES["single-node"],
                run_egress=False,
                reset_portainer_state=True,
                executor=Mock(),
            )

        self.assertLess(sequence.index("reset-portainer"), sequence.index("portainer"))

    def test_reset_portainer_state_removes_containers_using_data_volume(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo.return_value = ""

        result = _reset_portainer_state(remote)

        self.assertEqual(result, "Portainer state reset")
        command = remote.sudo.call_args.args[0]
        self.assertIn("docker ps -aq --filter volume=portainer_portainer_data", command)
        self.assertIn("docker rm -f $containers", command)
        self.assertIn("docker volume rm portainer_portainer_data", command)

    def test_portainer_agent_is_manager_only(self):
        stack = yaml.safe_load(asset_text("stacks/core/portainer/stack.yml"))
        agent = stack["services"]["agent"]

        self.assertEqual(agent["environment"]["AGENT_CLUSTER_ADDR"], "tasks.agent")
        self.assertIn("node.role == manager", agent["deploy"]["placement"]["constraints"])
        self.assertIn("node.platform.os == linux", agent["deploy"]["placement"]["constraints"])
        self.assertEqual(stack["services"]["portainer"]["command"], "-H tcp://tasks.agent:9001 --tlsskipverify")

    def test_install_control_config_generates_config_without_local_path(self):
        config = LumaConfig({}, None)
        node = config.default_manager()
        if node is None:
            from luma.config import NodeConfig

            node = NodeConfig(
                name="manager",
                host="localhost",
                public_ip="127.0.0.1",
                region="cn",
                roles=["swarm-manager", "edge"],
            )
        remote = Mock()
        remote.write_secret.return_value = "Secret written: /opt/luma/luma.yaml"

        result = install_control_config(remote, config, node)

        self.assertEqual(result, "Secret written: /opt/luma/luma.yaml")
        content = remote.write_secret.call_args.args[0]
        data = yaml.safe_load(content)
        self.assertEqual(data["project"], "luma")
        self.assertEqual(data["nodes"]["manager"]["host"], "localhost")
        self.assertEqual(data["defaults"]["publicNetwork"], "public")

    def test_control_image_pull_failure_is_fatal(self):
        remote = Mock()
        remote.sudo.side_effect = Exception("pull failed")

        with self.assertRaisesRegex(LumaError, "failed to pull Luma Control image"):
            _ensure_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        remote.upload.assert_not_called()
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker pull ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker image inspect" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker build" in cmd for cmd in docker_commands))

    def test_control_image_pulls_published_image_during_bootstrap(self):
        remote = Mock()
        remote.sudo.return_value = ""

        result = _ensure_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Control image pulled: ghcr.io/liutianjie/luma-control:latest")
        self.assertEqual(remote.upload.call_count, 0)
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker pull ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker build" in cmd for cmd in docker_commands))

    def test_control_latest_image_resolves_to_pulled_repo_digest(self):
        remote = Mock()

        def sudo(command):
            if "docker service inspect egress_mihomo" in command:
                return ""
            if "docker info --format" in command:
                return "HTTPProxy=http://127.0.0.1:7890 HTTPSProxy=http://127.0.0.1:7890\n"
            if "docker pull" in command:
                return ""
            if "docker image inspect --format" in command:
                return '["ghcr.io/liutianjie/luma-control@sha256:abc123","mirror.local/luma-control@sha256:def456"]\n'
            return ""

        remote.sudo.side_effect = sudo

        image, result = _resolve_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(image, "ghcr.io/liutianjie/luma-control@sha256:abc123")
        self.assertIn("Control image pulled: ghcr.io/liutianjie/luma-control:latest", result)
        self.assertIn("resolved digest: ghcr.io/liutianjie/luma-control@sha256:abc123", result)

    def test_control_pinned_image_does_not_require_repo_digest_lookup(self):
        remote = Mock()

        def sudo(command):
            if "docker service inspect egress_mihomo" in command:
                return ""
            if "docker info --format" in command:
                return "HTTPProxy=http://127.0.0.1:7890 HTTPSProxy=http://127.0.0.1:7890\n"
            return ""

        remote.sudo.side_effect = sudo

        image, result = _resolve_control_image(remote, "ghcr.io/liutianjie/luma-control@sha256:abc123")

        self.assertEqual(image, "ghcr.io/liutianjie/luma-control@sha256:abc123")
        self.assertIn("Control image pull egress ready for ghcr.io", result)
        self.assertIn("Control image pulled: ghcr.io/liutianjie/luma-control@sha256:abc123", result)
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertFalse(any("docker image inspect --format" in cmd for cmd in docker_commands))

    def test_control_image_pull_configures_docker_daemon_egress_proxy(self):
        remote = Mock()
        info_calls = 0

        def sudo(command):
            nonlocal info_calls
            if "docker service inspect egress_mihomo" in command:
                return ""
            if "docker info --format" in command:
                info_calls += 1
                if info_calls == 1:
                    return "HTTPProxy= HTTPSProxy=\n"
                return "HTTPProxy=http://127.0.0.1:7890 HTTPSProxy=http://127.0.0.1:7890\n"
            if "systemctl restart docker" in command:
                return ""
            return ""

        remote.sudo.side_effect = sudo

        result = _ensure_control_image_pull_egress(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Control image pull egress configured for ghcr.io: Docker daemon proxy http://127.0.0.1:7890")
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker service inspect egress_mihomo" in cmd for cmd in docker_commands))
        self.assertTrue(any("HTTP_PROXY=http://127.0.0.1:7890" in cmd for cmd in docker_commands))
        self.assertTrue(any("NO_PROXY=localhost,127.0.0.1" in cmd for cmd in docker_commands))

    def test_control_image_pull_requires_running_egress_gateway(self):
        remote = Mock()
        remote.sudo.side_effect = Exception("missing")

        with self.assertRaisesRegex(LumaError, "control image pull egress requires a running egress_mihomo"):
            _ensure_control_image_pull_egress(remote, "ghcr.io/liutianjie/luma-control:latest")

        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker service inspect egress_mihomo" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker pull" in cmd for cmd in docker_commands))

    def test_control_stack_deploy_uses_resolved_digest_image(self):
        digest_image = "ghcr.io/liutianjie/luma-control@sha256:abc123"
        remote = Mock()
        remote.run_result.return_value = Mock(code=1, output="")
        remote.sudo.return_value = ""
        uploaded = {}
        progress = []

        def capture_upload(local_path, remote_path):
            uploaded["stack"] = Path(local_path).read_text(encoding="utf-8")

        remote.upload.side_effect = capture_upload
        config = LumaConfig({}, None)

        with patch(
            "luma.bootstrap._ensure_control_image_pull_egress",
            return_value="Control image pull egress ready for ghcr.io: Docker daemon proxy http://127.0.0.1:7890",
        ), patch("luma.bootstrap._ensure_control_image", return_value="Control image pulled: ghcr.io/liutianjie/luma-control:latest"), patch(
            "luma.bootstrap._control_image_repo_digest", return_value=digest_image
        ), patch(
            "luma.bootstrap._wait_service_ready", return_value="Service ready: luma-control_luma-control"
        ):
            result = deploy_control_stack(remote, config, "luma.example.com", emit=progress.append)

        self.assertIn("Control image pull egress ready for ghcr.io", result[0])
        self.assertEqual(result[1], "Control image pulled: ghcr.io/liutianjie/luma-control:latest")
        self.assertEqual(result[2], f"Control image digest resolved: {digest_image}")
        progress_text = "\n".join(progress)
        self.assertIn("[start] Ensure control image pull egress", progress_text)
        self.assertIn("[start] Pull Luma control image", progress_text)
        self.assertIn("[start] Resolve Luma control image digest", progress_text)
        self.assertIn(f"image: {digest_image}", uploaded["stack"])
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any(f"docker service update --image {digest_image}" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker service update --image ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))

    def test_control_stack_deploy_can_skip_pull_egress_precheck(self):
        digest_image = "ghcr.io/liutianjie/luma-control@sha256:abc123"
        remote = Mock()
        remote.run_result.return_value = Mock(code=1, output="")
        remote.sudo.return_value = ""
        uploaded = {}
        progress = []

        def capture_upload(local_path, remote_path):
            uploaded["stack"] = Path(local_path).read_text(encoding="utf-8")

        remote.upload.side_effect = capture_upload
        config = LumaConfig({}, None)

        with patch("luma.bootstrap._ensure_control_image_pull_egress") as ensure_egress, patch(
            "luma.bootstrap._ensure_control_image",
            return_value="Control image pulled: ghcr.io/liutianjie/luma-control:latest",
        ), patch("luma.bootstrap._control_image_repo_digest", return_value=digest_image), patch(
            "luma.bootstrap._wait_service_ready", return_value="Service ready: luma-control_luma-control"
        ):
            result = deploy_control_stack(
                remote,
                config,
                "luma.example.com",
                emit=progress.append,
                require_pull_egress=False,
            )

        ensure_egress.assert_not_called()
        self.assertEqual(result[0], "Control image pulled: ghcr.io/liutianjie/luma-control:latest")
        self.assertNotIn("[start] Ensure control image pull egress", "\n".join(progress))
        self.assertIn(f"image: {digest_image}", uploaded["stack"])

    def test_control_service_force_updates_image_after_stack_deploy(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo.return_value = ""

        result = _force_update_service_image(remote, "luma-control_luma-control", "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Service image refreshed: luma-control_luma-control -> ghcr.io/liutianjie/luma-control:latest")
        command = remote.sudo.call_args.args[0]
        self.assertIn("docker service update --image ghcr.io/liutianjie/luma-control:latest", command)
        self.assertIn("--update-order start-first", command)
        self.assertIn("--update-failure-action rollback", command)
        self.assertIn("--update-parallelism 1", command)
        self.assertIn("--force luma-control_luma-control", command)

    def test_manager_control_refresh_only_updates_control_stack(self):
        config = LumaConfig(
            {
                "nodes": {
                    "manager": {
                        "host": "localhost",
                        "publicIp": "127.0.0.1",
                        "roles": ["swarm-manager", "edge"],
                    }
                }
            },
            None,
        )
        node = config.get_node("manager")
        state = {"clusterId": "luma-test", "deployToken": "deploy", "joinToken": "join", "portainerAdminPassword": "secret"}
        with patch("luma.bootstrap.local_swarm_join_info", return_value={"managerAddr": "127.0.0.1:2377", "swarmJoinToken": "swarm", "swarmId": "sid"}), patch(
            "luma.bootstrap.install_control_config", return_value="config"
        ) as install_config, patch("luma.bootstrap.install_control_state", return_value="state") as install_state, patch(
            "luma.bootstrap.deploy_control_stack", return_value=["control"]
        ) as deploy_control, patch("luma.bootstrap._deploy_traefik") as traefik, patch(
            "luma.bootstrap._deploy_portainer"
        ) as portainer, patch("luma.bootstrap.bind_portainer_credentials") as bind, patch(
            "luma.bootstrap.install_docker"
        ) as docker, patch("luma.bootstrap.setup_egress") as egress, patch(
            "luma.bootstrap._refresh_core_services"
        ) as refresh_core:
            result = refresh_manager_control_local(config, node, "luma.example.com", state)

        self.assertIn("config", result)
        self.assertIn("state", result)
        self.assertIn("control", result)
        self.assertEqual(state["domain"], "luma.example.com")
        self.assertEqual(state["managerAddr"], "127.0.0.1:2377")
        self.assertEqual(state["portainerAdminPassword"], "secret")
        install_config.assert_called_once()
        install_state.assert_called_once()
        deploy_control.assert_called_once()
        traefik.assert_not_called()
        portainer.assert_not_called()
        bind.assert_not_called()
        docker.assert_not_called()
        egress.assert_not_called()
        refresh_core.assert_not_called()

    def test_install_docker_repairs_known_bad_apt_mirror(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo_result.return_value = Mock(code=1, output="")
        remote.sudo.return_value = ""

        result = install_docker(remote)

        self.assertEqual(result, "Docker installed")
        command = remote.sudo.call_args.args[0]
        self.assertIn("command -v apt-get", command)
        self.assertIn("mirrors.ivolces.com/ubuntu", command)
        self.assertIn("mirrors.aliyun.com/ubuntu", command)
        self.assertIn("apt-get install -y docker.io docker-compose-v2", command)
        self.assertNotIn("apt-get install -y docker.io docker-compose-v2 curl ca-certificates ufw python3-yaml || true", command)

    def test_install_docker_on_macos_requires_docker_cli(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(code=0, output="Darwin\n"),
            Mock(code=1, output=""),
        ]

        with self.assertRaisesRegex(LumaError, "Install Docker Desktop"):
            install_docker(remote)

    def test_install_docker_on_macos_requires_running_daemon(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(code=0, output="Darwin\n"),
            Mock(code=0, output=""),
            Mock(code=1, output=""),
        ]

        with self.assertRaisesRegex(LumaError, "Start Docker Desktop"):
            install_docker(remote)

    def test_service_webhook_env_overrides_global_webhook(self):
        with tempfile.TemporaryDirectory() as tmp:
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
                        "portainer": {"webhookUrlEnv": "PORTAINER_WEBHOOK_API"},
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"providers": {"portainer": {"webhookUrlEnv": "PORTAINER_WEBHOOK_URL"}}}, None)
            import os

            old_global = os.environ.get("PORTAINER_WEBHOOK_URL")
            old_api = os.environ.get("PORTAINER_WEBHOOK_API")
            try:
                os.environ["PORTAINER_WEBHOOK_URL"] = "https://portainer/global"
                os.environ["PORTAINER_WEBHOOK_API"] = "https://portainer/api"
                webhook_url, webhook_env = resolve_webhook(config, service)
                self.assertEqual(webhook_url, "https://portainer/api")
                self.assertEqual(webhook_env, "PORTAINER_WEBHOOK_API")
            finally:
                _restore_env("PORTAINER_WEBHOOK_URL", old_global)
                _restore_env("PORTAINER_WEBHOOK_API", old_api)


class ControlApiTests(unittest.TestCase):
    def test_node_and_deploy_tokens_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with self.assertRaises(Exception):
                    handle_node_register(state["deployToken"], {"nodeName": "b", "region": "global"})
                result = handle_node_register(state["joinToken"], {"nodeName": "b", "region": "global"})
                self.assertEqual(result["nodeName"], "b")
                self.assertEqual(result["region"], "global")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_join_token_can_label_node_after_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with patch("luma.control.server.label_swarm_node") as label:
                    result = handle_node_label(state["joinToken"], {"nodeName": "b", "region": "global"})
                label.assert_called_once()
                labels = label.call_args.args[1]
                self.assertEqual(labels["region"], "global")
                self.assertEqual(labels["luma.node.name"], "b")
                self.assertNotIn("egress", labels)
                self.assertNotIn("role.global-worker", labels)
                self.assertEqual(result["nodeName"], "b")
                with self.assertRaises(Exception):
                    handle_node_label(state["deployToken"], {"nodeName": "b", "region": "global"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_label_node_keeps_requested_name_as_luma_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_node_register(state["joinToken"], {"nodeName": "global-sg-1", "region": "global"})
                with patch("luma.control.server.label_swarm_node"):
                    result = handle_node_label(
                        state["joinToken"],
                        {
                            "nodeName": "docker-hostname",
                            "nodeId": "node-id-1",
                            "registeredName": "global-sg-1",
                            "region": "global",
                            "tailscaleIP": "100.64.0.30",
                            "tailscaleName": "global-sg-1.ts.net",
                        },
                    )
                saved = load_state()
                self.assertEqual(result["nodeName"], "global-sg-1")
                self.assertEqual(result["displayName"], "global-sg-1")
                self.assertEqual(result["tailscaleIP"], "100.64.0.30")
                self.assertEqual(result["tailscaleName"], "global-sg-1.ts.net")
                self.assertIn("global-sg-1", saved["nodes"])
                self.assertEqual(saved["nodes"]["global-sg-1"]["swarmHostname"], "docker-hostname")
                self.assertEqual(saved["nodes"]["global-sg-1"]["swarmNodeId"], "node-id-1")
                self.assertEqual(saved["nodes"]["global-sg-1"]["tailscaleIP"], "100.64.0.30")
                self.assertEqual(saved["nodes"]["global-sg-1"]["tailscaleName"], "global-sg-1.ts.net")
                self.assertEqual(saved["nodes"]["global-sg-1"]["labels"]["luma.node.id"], "node-id-1")
                self.assertEqual(saved["nodes"]["global-sg-1"]["status"], "labeled")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_register_rejects_unknown_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with self.assertRaises(Exception):
                    handle_node_register(state["joinToken"], {"nodeName": "b", "region": "mars"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_removes_registered_only_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_node_register(state["joinToken"], {"nodeName": "m3max", "region": "home"})
                with patch("luma.control.server.docker_request", return_value=[]):
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "m3max"})
                saved = load_state()
                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                self.assertFalse(result["swarmRemoved"])
                self.assertNotIn("m3max", saved.get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_removes_matching_swarm_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "m4mini": {
                        "region": "home",
                        "displayName": "m4mini",
                        "swarmHostname": "orbstack",
                        "swarmNodeId": "node-id-1",
                        "labels": {"luma.node.name": "m4mini", "luma.node.id": "node-id-1", "region": "home"},
                    }
                }
                save_state(state)
                nodes = [
                    {
                        "ID": "node-id-1",
                        "Description": {"Hostname": "orbstack"},
                        "Spec": {"Role": "worker", "Labels": {"luma.node.name": "m4mini", "luma.node.id": "node-id-1"}},
                    }
                ]
                with patch("luma.control.server.docker_request", side_effect=[nodes, None]) as docker:
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "m4mini"})

                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                self.assertTrue(result["swarmRemoved"])
                self.assertEqual(result["swarmNodeId"], "node-id-1")
                docker.assert_any_call("DELETE", "/nodes/node-id-1?force=1")
                self.assertNotIn("m4mini", load_state().get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_removes_swarm_only_luma_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                nodes = [
                    {
                        "ID": "node-id-2",
                        "Description": {"Hostname": "orbstack"},
                        "Spec": {"Role": "worker", "Labels": {"luma.node.name": "m4mini", "region": "home"}},
                    }
                ]
                with patch("luma.control.server.docker_request", side_effect=[nodes, None]) as docker:
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "m4mini"})

                self.assertTrue(result["removed"])
                self.assertFalse(result["registeredRemoved"])
                self.assertTrue(result["swarmRemoved"])
                docker.assert_any_call("DELETE", "/nodes/node-id-2?force=1")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_refuses_to_remove_manager_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                nodes = [
                    {
                        "ID": "manager-id",
                        "Description": {"Hostname": "manager"},
                        "Spec": {"Role": "manager", "Labels": {"luma.node.name": "manager"}},
                        "ManagerStatus": {"Leader": True},
                    }
                ]
                with patch("luma.control.server.docker_request", return_value=nodes), self.assertRaisesRegex(LumaError, "refusing to remove Swarm manager"):
                    handle_node_unregister(state["deployToken"], {"nodeName": "manager"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_keeps_state_when_swarm_remove_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"m4mini": {"region": "home", "displayName": "m4mini"}}
                save_state(state)
                with patch("luma.control.server.docker_request", side_effect=LumaError("Docker unavailable")), self.assertRaisesRegex(LumaError, "Docker unavailable"):
                    handle_node_unregister(state["deployToken"], {"nodeName": "m4mini"})

                self.assertIn("m4mini", load_state().get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_resolves_luma_node_name_to_swarm_node_id_constraint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "mac-mini-gaojiu": {
                        "region": "home",
                        "status": "labeled",
                        "swarmHostname": "orbstack",
                        "swarmNodeId": "node-id-gaojiu",
                        "labels": {
                            "region": "home",
                            "luma.node.name": "mac-mini-gaojiu",
                            "luma.node.id": "node-id-gaojiu",
                        },
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "home-panel",
                        "image": "ghcr.io/me/home-panel:1",
                        "region": "home",
                        "node": "mac-mini-gaojiu",
                        "exposure": "none",
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "home-panel.yaml", "skipDns": True, "skipWebhook": True},
                    )
                self.assertEqual(result["service"], "home-panel")
                stack = (root / "stacks" / "home" / "home-panel" / "stack.yml").read_text(encoding="utf-8")
                self.assertIn("node.labels.luma.node.id == node-id-gaojiu", stack)
                self.assertNotIn("node.hostname", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_uses_control_state_and_portainer_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {
                                "dns": {"type": "cloudflare", "zone": "example.com"},
                                "portainer": {"webhooks": {"api": "PORTAINER_WEBHOOK_API"}},
                            },
                            "defaults": {"stackRoot": str(root / "stacks")},
                            "nodes": {
                                "edge": {
                                    "host": "edge",
                                    "publicIp": "203.0.113.10",
                                    "region": "cn",
                                    "roles": ["edge"],
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                probe_error = urllib.error.HTTPError("https://api.example.com/", 404, "not found", {}, None)
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.sync_dns", return_value="DNS updated"
                ), patch(
                    "luma.control.server.deploy_with_portainer", return_value="Portainer deploy triggered"
                ), patch("luma.control.server.urllib.request.urlopen", side_effect=probe_error):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
                self.assertIn(str(root / "stacks" / "cn" / "api" / "stack.yml"), result["written"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Parse manifest=ok:api -> cn/cn-edge", steps)
                self.assertIn("Sync DNS=ok:DNS updated", steps)
                self.assertIn("Deploy Portainer stack=ok:Portainer deploy triggered", steps)
                self.assertIn("Probe public route=ok:Public route reachable: https://api.example.com/ -> HTTP 404", steps)
                deployment_state = load_state()
                self.assertEqual(deployment_state["deployments"]["services"]["api"]["name"], "api")
                self.assertEqual(deployment_state["deployments"]["services"]["api"]["manifest"], manifest)
                with self.assertRaises(Exception):
                    handle_deployment(state["joinToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_deployment_preview_renders_without_writing_or_deploying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "edge": {
                        "region": "cn",
                        "status": "labeled",
                        "swarmNodeId": "edge-node-id",
                        "labels": {"luma.node.id": "edge-node-id"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "node": "edge",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with patch("luma.control.server.sync_dns") as sync, patch("luma.control.server.deploy_with_portainer") as deploy:
                    result = handle_deployment_preview(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["summary"]["exposure"], "cn-edge")
                self.assertEqual(result["artifacts"][0]["kind"], "stack")
                self.assertIn("node.labels.luma.node.id == edge-node-id", result["artifacts"][0]["content"])
                self.assertFalse((root / "stacks").exists())
                sync.assert_not_called()
                deploy.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_deployment_rejects_existing_compose_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "api": {
                            "kind": "compose",
                            "name": "api",
                            "slug": "api",
                            "manifest": "name: api\nregion: cn\ncompose: docker-compose.yml\n",
                        }
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                with patch("luma.control.server.deploy_with_portainer") as deploy:
                    with self.assertRaisesRegex(LumaError, "deployment name already exists"):
                        handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                deploy.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_config_returns_saved_service_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api.yaml",
                            "updatedAt": 123,
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                result = handle_deployment_config(state["deployToken"], "api")
                self.assertEqual(result["kind"], "service")
                self.assertEqual(result["name"], "api")
                self.assertEqual(result["sourceName"], "console:api.yaml")
                self.assertEqual(result["updatedAt"], 123)
                self.assertEqual(result["manifest"], manifest)
                self.assertEqual(result["composeContent"], "")
                with self.assertRaisesRegex(LumaError, "unauthorized"):
                    handle_deployment_config(state["joinToken"], "api")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_config_returns_saved_compose_manifest_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"})
                compose = yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}})
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "app-stack": {
                            "kind": "compose",
                            "name": "app-stack",
                            "slug": "app-stack",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:luma.compose.yml",
                            "updatedAt": 456,
                        }
                    },
                }
                save_state(state)
                result = handle_deployment_config(state["deployToken"], "app-stack")
                self.assertEqual(result["kind"], "compose")
                self.assertEqual(result["name"], "app-stack")
                self.assertEqual(result["manifest"], sidecar)
                self.assertEqual(result["composeContent"], compose)
                self.assertEqual(result["updatedAt"], 456)
                with self.assertRaisesRegex(LumaError, "deployment not found"):
                    handle_deployment_config(state["deployToken"], "missing")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_preview_rejects_invalid_region_exposure_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "global",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with self.assertRaisesRegex(LumaError, "exposure=cn-edge requires region=cn"):
                    handle_deployment_preview(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_application_restart_force_updates_business_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            calls = []
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)

                def fake_docker(method, path, body=None):
                    calls.append((method, path, body))
                    if method == "GET" and path == "/services":
                        return [
                            {"ID": "svc-api", "Spec": {"Name": "myapp_api"}},
                            {"ID": "svc-worker", "Spec": {"Name": "myapp_worker"}},
                            {"ID": "svc-traefik", "Spec": {"Name": "traefik_traefik"}},
                        ]
                    if method == "GET" and path.startswith("/services/svc-"):
                        service_id = path.rsplit("/", 1)[-1]
                        return {"Version": {"Index": 7}, "Spec": {"Name": service_id, "TaskTemplate": {"ForceUpdate": 2}}}
                    if method == "POST" and path.startswith("/services/"):
                        return None
                    raise AssertionError((method, path, body))

                with patch("luma.control.server.docker_request", side_effect=fake_docker):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})
                self.assertEqual(result["stack"], "myapp")
                self.assertEqual(len(result["restarted"]), 2)
                update_calls = [call for call in calls if call[0] == "POST"]
                self.assertEqual(len(update_calls), 2)
                self.assertIn("/services/svc-api/update?version=7", update_calls[0][1])
                self.assertEqual(update_calls[0][2]["TaskTemplate"]["ForceUpdate"], 3)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_rejects_system_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with self.assertRaisesRegex(LumaError, "system stack"):
                    handle_application_restart(state["deployToken"], {"stack": "traefik"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_service_remove_cleans_dns_portainer_and_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                stack_dir = root / "stacks" / "home" / "home-panel"
                stack_dir.mkdir(parents=True)
                (stack_dir / "stack.yml").write_text("services: {}\n", encoding="utf-8")
                route_file = root / "routes" / "home-panel.yml"
                route_file.parent.mkdir(parents=True)
                route_file.write_text("http:\n", encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zoneId": "zone-id"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "home-panel",
                        "image": "ghcr.io/me/home-panel:1",
                        "region": "home",
                        "exposure": "tailscale-relay",
                        "domain": "panel.example.com",
                        "port": 8080,
                    }
                )
                state["deployments"] = {
                    "services": {
                        "home-panel": {
                            "kind": "service",
                            "name": "home-panel",
                            "slug": "home-panel",
                            "manifest": manifest,
                            "sourceName": "console:home-panel",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                with patch("luma.control.server.delete_dns", return_value="DNS deleted: panel.example.com") as dns, patch(
                    "luma.control.server.remove_stack", return_value="Portainer stack removed: home-panel"
                ) as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "home-panel"})
                dns.assert_called_once()
                stack.assert_called_once()
                self.assertFalse(stack_dir.exists())
                self.assertFalse(route_file.exists())
                self.assertEqual(result["service"], "home-panel")
                self.assertIn(str(stack_dir), result["files"])
                self.assertIn(str(route_file), result["files"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Delete DNS=ok:DNS deleted: panel.example.com", steps)
                self.assertIn("Remove Portainer stack=ok:Portainer stack removed: home-panel", steps)
                self.assertIn("Delete generated files=ok:Generated files removed", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_service_remove_dry_run_does_not_delete_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                stack_dir = root / "stacks" / "cn" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / "stack.yml").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                with patch("luma.control.server.delete_dns") as dns, patch("luma.control.server.remove_stack") as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "api", "dryRun": True})
                dns.assert_not_called()
                stack.assert_not_called()
                self.assertTrue(stack_dir.exists())
                self.assertTrue(result["dryRun"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Portainer stack would be removed: api", steps)
                self.assertIn("Generated files would be removed", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_by_name_uses_registered_manifest_and_forgets_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                stack_dir = root / "stacks" / "cn" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / "stack.yml").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_stack", return_value="Portainer stack removed: api") as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "api"})
                stack.assert_called_once()
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["sourceName"], "console:api")
                self.assertFalse(stack_dir.exists())
                self.assertNotIn("api", load_state()["deployments"]["services"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_falls_back_to_live_swarm_stack_when_state_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {"services": {}, "compose": {}}
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                save_state(state)
                stack_dir = root / "stacks" / "cn" / "gitea"
                stack_dir.mkdir(parents=True)
                (stack_dir / "stack.yml").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zoneId": "zone-id"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                docker_services = [
                    {
                        "ID": "svc-gitea",
                        "Spec": {
                            "Name": "gitea_gitea",
                            "Labels": {
                                "traefik.enable": "true",
                                "traefik.http.routers.gitea.rule": "Host(`gitea.example.com`)",
                                "traefik.http.services.gitea.loadbalancer.server.port": "3000",
                                "traefik.swarm.network": "public",
                            },
                            "TaskTemplate": {
                                "ContainerSpec": {"Image": "gitea/gitea:1.22@sha256:abc"},
                                "Placement": {"Constraints": ["node.labels.region == cn"]},
                            },
                            "Mode": {"Replicated": {"Replicas": 1}},
                        },
                    }
                ]
                with patch("luma.control.server.docker_request", return_value=docker_services), patch(
                    "luma.control.server.delete_dns", return_value="DNS deleted: gitea.example.com"
                ) as dns, patch("luma.control.server.remove_stack", return_value="Portainer stack removed: gitea") as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "gitea"})
                dns.assert_called_once()
                stack.assert_called_once()
                self.assertEqual(stack.call_args.args[1].name, "gitea")
                self.assertEqual(dns.call_args.args[1].domain, "gitea.example.com")
                self.assertEqual(result["service"], "gitea")
                self.assertEqual(result["sourceName"], "live:gitea")
                self.assertFalse(stack_dir.exists())
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_service_remove_by_name_removes_registered_compose_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"})
                compose = yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}})
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "app-stack": {
                            "kind": "compose",
                            "name": "app-stack",
                            "slug": "app-stack",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:app-stack",
                        }
                    },
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "app-stack"
                stack_dir.mkdir(parents=True)
                (stack_dir / "stack.yml").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_stack", return_value="Portainer stack removed: app-stack") as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "app-stack"})
                stack.assert_called_once()
                self.assertEqual(result["deployment"], "app-stack")
                self.assertEqual(result["sourceName"], "console:app-stack")
                self.assertFalse(stack_dir.exists())
                self.assertNotIn("app-stack", load_state()["deployments"]["compose"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_can_delete_single_service_named_volumes_from_recorded_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "volumes": ["api-data:/data", "/host/data:/host-data", "./local-data:/local-data"],
                    }
                )
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                stack_dir = root / "stacks" / "cn" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / "stack.yml").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                task_nodes = [{"id": "node-1", "hostname": "worker-1", "lumaNode": "worker-1"}]
                with patch("luma.control.server.remove_stack", return_value="Portainer stack removed: api"), patch(
                    "luma.control.server._service_task_nodes", return_value=task_nodes
                ) as task_lookup, patch(
                    "luma.control.server._remove_docker_volume_across_nodes",
                    return_value={"name": "api_api-data", "status": "removed local Docker volume"},
                ) as remove_volume:
                    result = handle_service_remove(
                        state["deployToken"],
                        {"name": "api", "skipDns": True, "deleteStorage": True},
                    )
                task_lookup.assert_called_once()
                remove_volume.assert_called_once()
                self.assertEqual(remove_volume.call_args.args[0], "api_api-data")
                self.assertEqual(remove_volume.call_args.args[1], task_nodes)
                self.assertIn("removed=1", result["storageCleanup"])
                self.assertNotIn("api", load_state()["deployments"]["services"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_can_delete_single_service_managed_storage_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma",
                    }
                }
                state["nodes"] = {"home-node": {"region": "home", "swarmHostname": "home-node"}}
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/me/api:1",
                        "region": "home",
                        "volumes": ["api-data:/data"],
                        "storage": {"api-data": {"storageClass": "home-nfs", "path": "api/api-data"}},
                    }
                )
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                stack_dir = root / "stacks" / "home" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / "stack.yml").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                task_nodes = [{"id": "node-1", "hostname": "home-node", "lumaNode": "home-node"}]
                with patch("luma.control.server.remove_stack", return_value="Portainer stack removed: api"), patch(
                    "luma.control.server._storage_node_is_local", return_value=True
                ), patch("luma.control.server._run_host_prep_command", return_value="removed") as host_prep, patch(
                    "luma.control.server._service_task_nodes", return_value=task_nodes
                ), patch(
                    "luma.control.server._remove_docker_volume_across_nodes",
                    return_value={"name": "api_api-data", "status": "removed local Docker volume"},
                ):
                    result = handle_service_remove(state["deployToken"], {"name": "api", "deleteStorage": True, "skipDns": True})
                commands = "\n".join(call.args[0] for call in host_prep.call_args_list)
                self.assertIn("/srv/luma/api/api-data", commands)
                self.assertIn("Managed storage cleanup finished: removed=1", result["storageCleanup"])
                self.assertIn("Docker volume cleanup finished: removed=1", result["storageCleanup"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_single_service_deploy_prepares_managed_storage_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma",
                    }
                }
                state["nodes"] = {"home-node": {"region": "home", "swarmHostname": "home-node"}}
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "registry.local/api:1",
                        "region": "home",
                        "volumes": ["api-data:/data"],
                        "storage": {"api-data": {"storageClass": "home-nfs", "path": "api/api-data"}},
                    }
                )
                with patch("luma.control.server.image_pull_requires_egress", return_value=False), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, registry_auth=None: (service, {"selected": service.image})
                ), patch("luma.control.server._storage_node_is_local", return_value=True), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep, patch("luma.control.server.deploy_with_portainer", return_value="Portainer deploy skipped"):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "console:api", "skipDns": True, "skipWebhook": True})
                commands = "\n".join(call.args[0] for call in host_prep.call_args_list)
                self.assertIn("/srv/luma/api/api-data", commands)
                self.assertIn("storagePreparation", result)
                self.assertIn("api", load_state()["deployments"]["services"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_can_delete_compose_managed_storage_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma",
                    }
                }
                sidecar = yaml.safe_dump(
                    {
                        "name": "nextcloud",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "volumes": {
                            "nextcloud-data": {"storageClass": "home-nfs", "path": "nextcloud/nextcloud-data"},
                            "nextcloud-db": {"storageClass": "home-nfs", "path": "nextcloud/nextcloud-db"},
                        },
                    }
                )
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "nextcloud": {"image": "nextcloud:apache", "volumes": ["nextcloud-data:/var/www/html"]},
                            "postgres": {"image": "postgres:16", "volumes": ["nextcloud-db:/var/lib/postgresql/data"]},
                        },
                        "volumes": {"nextcloud-data": {}, "nextcloud-db": {}},
                    }
                )
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "nextcloud": {
                            "kind": "compose",
                            "name": "nextcloud",
                            "slug": "nextcloud",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:nextcloud",
                        }
                    },
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "nextcloud"
                stack_dir.mkdir(parents=True)
                (stack_dir / "stack.yml").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_stack", return_value="Portainer stack removed: nextcloud"), patch(
                    "luma.control.server._storage_node_is_local", return_value=True
                ), patch("luma.control.server._run_host_prep_command", return_value="removed") as host_prep:
                    result = handle_service_remove(state["deployToken"], {"name": "nextcloud", "deleteStorage": True})
                self.assertEqual(host_prep.call_count, 2)
                commands = "\n".join(call.args[0] for call in host_prep.call_args_list)
                self.assertIn("/srv/luma/nextcloud/nextcloud-data", commands)
                self.assertIn("/srv/luma/nextcloud/nextcloud-db", commands)
                self.assertIn("removed=2", result["storageCleanup"])
                self.assertNotIn("nextcloud", load_state()["deployments"]["compose"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_storage_cleanup_dry_run_does_not_delete_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {"provider": "nfs", "mode": "managed", "node": "home-node", "path": "/srv/luma"}
                }
                sidecar = yaml.safe_dump(
                    {
                        "name": "nextcloud",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "volumes": {"nextcloud-db": {"storageClass": "home-nfs", "path": "nextcloud/nextcloud-db"}},
                    }
                )
                compose = yaml.safe_dump(
                    {
                        "services": {"postgres": {"image": "postgres:16", "volumes": ["nextcloud-db:/var/lib/postgresql/data"]}},
                        "volumes": {"nextcloud-db": {}},
                    }
                )
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "nextcloud": {
                            "kind": "compose",
                            "name": "nextcloud",
                            "slug": "nextcloud",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:nextcloud",
                        }
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_stack") as remove, patch("luma.control.server._run_host_prep_command") as host_prep:
                    result = handle_service_remove(state["deployToken"], {"name": "nextcloud", "deleteStorage": True, "dryRun": True})
                remove.assert_not_called()
                host_prep.assert_not_called()
                self.assertIn("/srv/luma/nextcloud/nextcloud-db", result["storageCleanup"])
                self.assertIn("nextcloud", load_state()["deployments"]["compose"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_rejects_delete_storage_with_skip_portainer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                with self.assertRaisesRegex(LumaError, "delete-storage"):
                    handle_service_remove(state["deployToken"], {"name": "nextcloud", "deleteStorage": True, "skipPortainer": True})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_control_status_reports_dns_and_portainer_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "CLOUDFLARE_API_TOKEN", "value": "cf-token"})
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=False)
                state["portainerApiUrl"] = "https://100.64.0.1:9443/api"
                state["portainerEndpointId"] = 2
                state["swarmId"] = "swarm"
                state["nodes"] = {
                    "manager": {"region": "cn", "status": "labeled", "labels": {"region": "cn"}},
                    "docker-home": {"displayName": "mini-gaojiu", "region": "home", "status": "labeled"},
                }
                state["storageClasses"] = {
                    "cn-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "manager",
                        "path": "/srv/luma",
                        "regions": ["cn"],
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {
                                "dns": {"type": "cloudflare", "zone": "example.com", "zoneId": "zone-id"},
                            },
                            "nodes": {
                                "edge": {
                                    "host": "edge",
                                    "publicIp": "203.0.113.10",
                                    "region": "cn",
                                    "roles": ["edge"],
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                docker_nodes = [
                    {
                        "ID": "manager-id-123456",
                        "Description": {"Hostname": "manager"},
                        "Spec": {"Role": "manager", "Availability": "active", "Labels": {"region": "cn", "ingress": "true"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.1"},
                        "ManagerStatus": {"Leader": True, "Reachability": "reachable"},
                    },
                    {
                        "ID": "home-id-123456",
                        "Description": {"Hostname": "docker-home"},
                        "Spec": {"Role": "worker", "Availability": "active", "Labels": {"region": "home"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.2"},
                    },
                ]
                with patch("luma.control.server.docker_request", return_value=docker_nodes):
                    result = handle_control_status(state["deployToken"])
                self.assertEqual(result["dns"]["provider"], "cloudflare")
                self.assertTrue(result["dns"]["tokenConfigured"])
                self.assertTrue(result["dns"]["zoneIdConfigured"])
                self.assertEqual(result["dns"]["target"], "203.0.113.10")
                self.assertEqual(result["dns"]["missing"], [])
                self.assertTrue(result["portainer"]["ready"])
                self.assertEqual(result["nodes"]["registered"], 2)
                self.assertEqual(result["nodes"]["items"][0]["name"], "docker-home")
                self.assertEqual(result["nodes"]["items"][0]["displayName"], "mini-gaojiu")
                self.assertEqual(result["swarm"]["nodes"][0]["hostname"], "docker-home")
                self.assertEqual(result["swarm"]["nodes"][1]["leader"], True)
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "cn-nfs")
                self.assertEqual(result["storage"]["storageClasses"][0]["path"], "/srv/luma")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_dashboard_payload_reports_nodes_services_and_traffic_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://100.64.0.1:9443/api?token=secret"
                state["portainerAdminPassword"] = "portainer-secret"
                state["portainerEndpointId"] = 2
                state["swarmId"] = "swarm"
                state["nodes"] = {
                    "manager": {"region": "cn", "status": "labeled", "labels": {"region": "cn"}},
                    "home-node": {"displayName": "mini", "region": "home", "status": "labeled"},
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "routes").mkdir()
                (root / "routes" / "home-panel.yml").write_text(
                    yaml.safe_dump(
                        {
                            "http": {
                                "routers": {"home-panel": {"rule": "Host(`panel.example.com`)"}},
                                "services": {
                                    "home-panel": {
                                        "loadBalancer": {"servers": [{"url": "http://100.64.0.2:8080"}]}
                                    }
                                },
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (root / "routes" / "nextcloud-nextcloud.yml").write_text(
                    yaml.safe_dump(
                        {
                            "http": {
                                "routers": {"nextcloud-nextcloud": {"rule": "Host(`next.example.com`)"}},
                                "services": {
                                    "nextcloud-nextcloud": {
                                        "loadBalancer": {"servers": [{"url": "http://100.64.0.2:80"}]}
                                    }
                                },
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {
                                "dns": {
                                    "type": "cloudflare",
                                    "zone": "example.com",
                                    "zoneId": "zone-id",
                                    "edgeTarget": "203.0.113.10",
                                }
                            },
                            "defaults": {"routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                docker_nodes = [
                    {
                        "ID": "node-manager",
                        "Description": {"Hostname": "manager"},
                        "Spec": {"Role": "manager", "Availability": "active", "Labels": {"region": "cn", "ingress": "true"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.1"},
                        "ManagerStatus": {"Leader": True},
                    },
                    {
                        "ID": "node-home",
                        "Description": {"Hostname": "home-node"},
                        "Spec": {"Role": "worker", "Availability": "active", "Labels": {"region": "home"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.2"},
                    },
                ]
                docker_services = [
                    _docker_service(
                        "svc-api",
                        "api_api",
                        "ghcr.io/me/api:1",
                        2,
                        ["node.labels.region == cn"],
                        {
                            "traefik.http.routers.api.rule": "Host(`api.example.com`)",
                            "traefik.http.services.api.loadbalancer.server.port": "3000",
                            "traefik.swarm.network": "public",
                        },
                    ),
                    _docker_service(
                        "svc-worker",
                        "worker_worker",
                        "ghcr.io/me/worker:1",
                        1,
                        ["node.labels.region == global"],
                        {"luma.storage.pg-data": "unmanaged"},
                    ),
                    _docker_service("svc-home", "home-panel_home-panel", "ghcr.io/me/panel:1", 1, ["node.labels.region == home"], {}),
                    _docker_service(
                        "svc-nextcloud",
                        "nextcloud_nextcloud",
                        "nextcloud:apache",
                        1,
                        ["node.labels.region == home"],
                        {"luma.compose.stack": "nextcloud", "luma.compose.service": "nextcloud"},
                    ),
                ]
                docker_tasks = [
                    _docker_task("svc-api", "node-manager", "running"),
                    _docker_task("svc-api", "node-home", "accepted"),
                    _docker_task("svc-api", "node-home", "failed"),
                    _docker_task("svc-worker", "node-manager", "running"),
                    _docker_task("svc-home", "node-home", "running"),
                    _docker_task("svc-nextcloud", "node-home", "running"),
                ]

                def fake_docker(method, path, body=None):
                    self.assertEqual(method, "GET")
                    if path == "/nodes":
                        return docker_nodes
                    if path == "/services":
                        return docker_services
                    if path == "/tasks":
                        return docker_tasks
                    raise AssertionError(path)

                with patch("luma.control.server.docker_request", side_effect=fake_docker):
                    result = handle_dashboard(state["deployToken"])

                self.assertEqual(result["cluster"]["id"], "luma-test")
                self.assertTrue(result["readiness"]["dns"]["ready"])
                self.assertTrue(result["readiness"]["portainer"]["ready"])
                self.assertEqual(result["readiness"]["dns"]["target"], "203.0.113.10")
                self.assertEqual(result["nodes"][0]["name"], "home-node")
                api = next(item for item in result["services"] if item["routeId"] == "api")
                self.assertEqual(api["domain"], "api.example.com")
                self.assertEqual(api["targetPort"], "3000")
                self.assertEqual(api["running"], 1)
                self.assertEqual(api["pending"], 1)
                self.assertEqual(api["failed"], 1)
                self.assertEqual(api["exposure"], "cn-edge")
                self.assertEqual(api["health"], "degraded")
                worker = next(item for item in result["services"] if item["routeId"] == "worker")
                self.assertEqual(worker["exposure"], "none")
                self.assertEqual(worker["health"], "running")
                self.assertEqual(worker["storage"][0]["name"], "pg-data")
                self.assertTrue(any("pg-data is unmanaged" in item for item in result["storage"]["warnings"]))
                home_path = next(item for item in result["trafficPaths"] if item["id"] == "home-panel")
                self.assertEqual(home_path["kind"], "tailscale-relay")
                self.assertIn("http://100.64.0.2:8080", home_path["segments"])
                nextcloud = next(item for item in result["services"] if item["fullName"] == "nextcloud_nextcloud")
                self.assertEqual(nextcloud["routeId"], "nextcloud-nextcloud")
                self.assertEqual(nextcloud["domain"], "next.example.com")
                self.assertEqual(nextcloud["exposure"], "tailscale-relay")
                serialized = json.dumps(result)
                self.assertNotIn("portainer-secret", serialized)
                self.assertNotIn(state["deployToken"], serialized)
                self.assertNotIn("token=secret", serialized)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_dashboard_returns_partial_payload_when_docker_socket_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"manager": {"region": "cn", "status": "registered"}}
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"providers": {"dns": {"type": "cloudflare"}}}), encoding="utf-8")
                with patch("luma.control.server.docker_request", side_effect=LumaError("Docker socket unavailable")):
                    result = handle_dashboard(state["deployToken"])
                self.assertFalse(result["readiness"]["swarm"]["available"])
                self.assertEqual(result["nodes"][0]["name"], "manager")
                self.assertEqual(result["nodes"][0]["state"], "missing")
                self.assertTrue(any("Docker nodes unavailable" in item for item in result["errors"]))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_handler_serves_static_assets_and_rejects_missing_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ControlHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/dashboard/", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"Luma Status", response.read())
            with urllib.request.urlopen(base + "/dashboard/app.js", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"/v1/dashboard", response.read())
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(base + "/v1/dashboard", timeout=5)
            self.assertEqual(raised.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_deployment_passes_referenced_secrets_as_portainer_stack_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://secret"})
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                response = MagicMock()
                response.__enter__.return_value.status = 200
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.sync_dns", return_value="DNS updated"
                ), patch(
                    "luma.control.server.deploy_with_portainer", return_value="Portainer deploy triggered"
                ) as deploy, patch("luma.control.server.urllib.request.urlopen", return_value=response):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["probe"], "Public route reachable: https://api.example.com/ -> HTTP 200")
                deploy.assert_called_once()
                self.assertEqual(deploy.call_args.kwargs["stack_env"], [{"name": "DATABASE_URL", "value": "postgres://secret"}])
                self.assertIn("DATABASE_URL: ${DATABASE_URL}", (root / "stacks" / "cn" / "api" / "stack.yml").read_text())
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_preview_renders_storage_without_writing_or_deploying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["home"]}
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "uptime-kuma": {
                                "image": "louislam/uptime-kuma:1",
                                "volumes": ["kuma-data:/app/data"],
                            }
                        },
                        "volumes": {"kuma-data": {}},
                    }
                )
                sidecar = yaml.safe_dump(
                    {
                        "name": "uptime-kuma",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "volumes": {"kuma-data": {"storageClass": "home-nfs", "path": "uptime-kuma/kuma-data"}},
                        "services": {
                            "uptime-kuma": {
                                "exposure": "tailscale-relay",
                                "domain": "kuma.example.com",
                                "port": 3001,
                            }
                        },
                    }
                )
                with patch("luma.control.server.upsert_stack") as upsert:
                    result = handle_compose_deployment_preview(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
                self.assertEqual(result["deployment"], "uptime-kuma")
                self.assertEqual(result["artifacts"][0]["kind"], "stack")
                self.assertIn("driver_opts", result["artifacts"][0]["content"])
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "home-nfs")
                self.assertFalse((root / "stacks").exists())
                upsert.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_preview_rejects_missing_storage_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["data:/data"]}}, "volumes": {"data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"data": {"storageClass": "missing-nfs", "path": "app/data"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "unknown storage class"):
                    handle_compose_deployment_preview(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_blocks_storage_backend_switch_without_initialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["cn"]}
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "app-stack"
                stack_dir.mkdir(parents=True)
                (stack_dir / "stack.yml").write_text(
                    yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}, "volumes": {"pg-data": {}}}),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "nfs", "path": "pg-data"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "storage backend changed"):
                    handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipWebhook": True},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_allows_storage_backend_switch_after_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["cn"]}
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "app-stack"
                stack_dir.mkdir(parents=True)
                (stack_dir / "stack.yml").write_text(
                    yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}, "volumes": {"pg-data": {}}}),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "nfs", "path": "pg-data", "adopted": True}},
                    }
                )
                result = handle_compose_deployment(
                    state["deployToken"],
                    {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipWebhook": True},
                )
                self.assertEqual(result["deployment"], "app-stack")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_rejects_sidecar_storage_classes_in_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "storageClasses": {"nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "managed by Luma Control"):
                    handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipWebhook": True},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_rejects_unsafe_compose_upload_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}})
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "../docker-compose.yml", "region": "cn"})
                with self.assertRaisesRegex(LumaError, "relative path without"):
                    handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipWebhook": True},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_class_is_managed_in_control_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-nas": {
                        "name": "home-nas",
                        "region": "home",
                        "status": "labeled",
                        "swarmNodeId": "home-node-id",
                        "labels": {"luma.node.name": "home-nas", "luma.node.id": "home-node-id", "region": "home"},
                    },
                }
                save_state(state)
                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "home-nas", "Swarm": {"NodeID": "home-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {
                            "name": "home-nfs",
                            "provider": "nfs",
                            "node": "home-nas",
                            "path": "/srv/luma",
                            "regions": ["home", "cn"],
                            "workloads": ["filesystem", "postgres"],
                        },
                    )
                self.assertTrue(result["saved"])
                listed = handle_storage_list(state["deployToken"])
                self.assertEqual(listed["storageClasses"][0]["name"], "home-nfs")
                self.assertEqual(listed["storageClasses"][0]["path"], "/srv/luma")
                self.assertEqual(listed["storageClasses"][0]["workloads"], ["filesystem", "postgres"])
                self.assertNotIn("exportRoot", listed["storageClasses"][0])
                persisted = load_state()
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["provider"], "nfs")
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["mode"], "managed")
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["path"], "/srv/luma")
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["workloads"], ["filesystem", "postgres"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_validates_new_managed_and_external_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-nas": {"name": "home-nas", "region": "home", "status": "labeled"},
                }
                save_state(state)
                with patch("luma.control.server.docker_request", return_value=[]):
                    with self.assertRaisesRegex(LumaError, "unknown Luma node"):
                        handle_storage_set(state["deployToken"], {"name": "bad-nfs", "provider": "nfs", "node": "missing", "path": "/srv/luma"})
                with self.assertRaisesRegex(LumaError, "cannot set endpoint"):
                    handle_storage_set(
                        state["deployToken"],
                        {"name": "bad-nfs", "provider": "nfs", "node": "home-nas", "path": "/srv/luma", "endpoint": "home-nas:/srv/luma"},
                    )
                with self.assertRaisesRegex(LumaError, "requires at least one region"):
                    handle_storage_set(
                        state["deployToken"],
                        {"name": "company-nfs", "provider": "nfs", "external": True, "endpoint": "nfs.example.com:/srv/luma"},
                    )
                result = handle_storage_set(
                    state["deployToken"],
                    {"name": "company-nfs", "external": True, "endpoint": "nfs.example.com:/srv/luma", "regions": ["cn"], "workloads": ["database"]},
                )
                self.assertTrue(result["saved"])
                saved = load_state()["storageClasses"]["company-nfs"]
                self.assertEqual(saved["provider"], "nfs")
                self.assertEqual(saved["mode"], "external")
                self.assertEqual(saved["regions"], ["cn"])
                self.assertEqual(saved["workloads"], ["database"])
                with self.assertRaisesRegex(LumaError, "workload must be one of"):
                    handle_storage_set(
                        state["deployToken"],
                        {"name": "bad-workload", "external": True, "endpoint": "nfs.example.com:/srv/luma", "regions": ["cn"], "workloads": ["postgresql"]},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_probe_records_verified_workload(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "storage-node": {
                        "name": "storage-node",
                        "region": "home",
                        "agent": {"status": "online", "lastSeen": int(time.time()), "capabilities": ["docker-volume"]},
                        "swarmHostname": "storage-node.local",
                    },
                }
                state["storageClasses"] = {
                    "db-storage": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "storage-node",
                        "path": "/srv/luma-db",
                        "regions": ["home"],
                        "workloads": ["filesystem", "postgres"],
                    }
                }
                save_state(state)
                with patch("luma.control.server._run_node_agent_task", return_value={"taskId": "task-1", "message": "ok"}) as run_task:
                    result = handle_storage_probe(state["deployToken"], {"name": "db-storage", "workload": "postgres"})
                self.assertTrue(result["verified"])
                self.assertEqual(result["node"], "storage-node")
                payload = run_task.call_args.args[3]
                self.assertEqual(payload["endpoint"], "storage-node.local:/srv/luma-db")
                self.assertEqual(payload["workload"], "postgres")
                self.assertEqual(payload["timeout"], 300)
                self.assertEqual(run_task.call_args.kwargs["timeout"], 330)
                saved = load_state()["storageClasses"]["db-storage"]
                self.assertEqual(saved["verifiedWorkloads"], ["postgres"])
                self.assertEqual(saved["workloadProbes"]["postgres"]["taskId"], "task-1")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_preserves_verified_workloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"home-node": {"region": "home", "swarmHostname": "home-node"}}
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma",
                        "workloads": ["filesystem", "postgres"],
                        "verifiedWorkloads": ["postgres"],
                        "workloadProbes": {"postgres": {"taskId": "task-1"}},
                    }
                }
                save_state(state)
                with patch("luma.control.server._ensure_storage_node_swarm_label", return_value=None), patch(
                    "luma.control.server._prepare_managed_nfs_host", return_value={"prepared": "ok"}
                ):
                    handle_storage_set(
                        state["deployToken"],
                        {"name": "home-nfs", "node": "home-node", "path": "/srv/luma", "regions": ["home"], "workloads": ["filesystem", "postgres"]},
                    )
                saved = load_state()["storageClasses"]["home-nfs"]
                self.assertEqual(saved["verifiedWorkloads"], ["postgres"])
                self.assertEqual(saved["workloadProbes"]["postgres"]["taskId"], "task-1")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_drops_verified_workloads_when_backend_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"home-node": {"region": "home", "swarmHostname": "home-node"}}
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma-old",
                        "workloads": ["filesystem", "postgres"],
                        "verifiedWorkloads": ["postgres"],
                        "workloadProbes": {"postgres": {"taskId": "task-1"}},
                    }
                }
                save_state(state)
                with patch("luma.control.server._ensure_storage_node_swarm_label", return_value=None), patch(
                    "luma.control.server._prepare_managed_nfs_host", return_value={"prepared": "ok"}
                ):
                    handle_storage_set(
                        state["deployToken"],
                        {"name": "home-nfs", "node": "home-node", "path": "/srv/luma-new", "regions": ["home"], "workloads": ["filesystem", "postgres"]},
                    )
                saved = load_state()["storageClasses"]["home-nfs"]
                self.assertNotIn("verifiedWorkloads", saved)
                self.assertNotIn("workloadProbes", saved)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_can_adopt_manager_swarm_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {"name": "home-mac-mini", "region": "home", "status": "labeled"},
                }
                save_state(state)
                swarm_nodes = [
                    {
                        "ID": "manager-node-id-1234567890",
                        "Spec": {
                            "Role": "manager",
                            "Labels": {"region": "cn", "ingress": "true"},
                        },
                        "Description": {"Hostname": "iZ0jl8auywzycory05d9cuZ"},
                        "Status": {"State": "ready"},
                        "ManagerStatus": {"Leader": True, "Reachability": "reachable"},
                    }
                ]
                docker_calls = []

                def fake_docker_request(method, path, body=None):
                    docker_calls.append((method, path, body))
                    if method == "GET" and path == "/info":
                        return {"Name": "iZ0jl8auywzycory05d9cuZ", "Swarm": {"NodeID": "manager-node-id-1234567890"}}
                    if method == "GET" and path == "/nodes":
                        return swarm_nodes
                    if method == "GET" and path.startswith("/nodes/"):
                        return {
                            "Version": {"Index": 12},
                            "Spec": {
                                "Role": "manager",
                                "Labels": {"region": "cn", "ingress": "true"},
                            },
                        }
                    if method == "POST" and path.startswith("/nodes/manager-node-id-1234567890/update"):
                        return {}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "provider": "nfs", "node": "iZ0jl8auywzycory05d9cuZ", "path": "/srv/luma"},
                    )
                self.assertTrue(result["saved"])
                persisted = load_state()
                self.assertEqual(persisted["storageClasses"]["cn-nfs"]["path"], "/srv/luma")
                manager = persisted["nodes"]["iZ0jl8auywzycory05d9cuZ"]
                self.assertEqual(manager["region"], "cn")
                self.assertEqual(manager["swarmHostname"], "iZ0jl8auywzycory05d9cuZ")
                self.assertEqual(manager["swarmNodeId"], "manager-node-id-1234567890")
                self.assertEqual(manager["status"], "adopted")
                self.assertEqual(manager["labels"]["luma.node.name"], "iZ0jl8auywzycory05d9cuZ")
                self.assertTrue(any(method == "POST" and "/update" in path for method, path, _ in docker_calls))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_labels_registered_manager_for_storage_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "status": "manager",
                        "swarmHostname": "manager-host",
                        "swarmNodeId": "manager-node-id",
                        "labels": {"region": "cn", "ingress": "true"},
                    }
                }
                save_state(state)
                docker_calls = []

                def fake_docker_request(method, path, body=None):
                    docker_calls.append((method, path, body))
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    if method == "GET" and path == "/nodes":
                        return [
                            {
                                "ID": "manager-node-id",
                                "Spec": {"Role": "manager", "Labels": {"region": "cn", "ingress": "true"}},
                                "Description": {"Hostname": "manager-host"},
                            }
                        ]
                    if method == "GET" and path.startswith("/nodes/"):
                        return {
                            "Version": {"Index": 5},
                            "Spec": {"Role": "manager", "Labels": {"region": "cn", "ingress": "true"}},
                        }
                    if method == "POST" and path.startswith("/nodes/manager-node-id/update"):
                        return {}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "node": "manager-host", "path": "/srv/luma"},
                    )
                self.assertTrue(result["saved"])
                manager = load_state()["nodes"]["manager-host"]
                self.assertEqual(manager["labels"]["luma.node.name"], "manager-host")
                self.assertEqual(manager["labels"]["luma.node.id"], "manager-node-id")
                self.assertTrue(any(method == "POST" and "/update" in path for method, path, _ in docker_calls))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_creates_managed_path_for_local_storage_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "swarmHostname": "manager-host",
                        "swarmNodeId": "manager-node-id",
                        "labels": {"luma.node.name": "manager-host", "luma.node.id": "manager-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                storage_path = root / "srv" / "luma"

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep:
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "node": "manager-host", "path": str(storage_path)},
                    )
                self.assertTrue(result["saved"])
                command = host_prep.call_args.args[0]
                self.assertIn("nfs-kernel-server", command)
                self.assertIn(str(storage_path), command)
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export ready")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_does_not_save_managed_class_when_host_prep_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "swarmHostname": "manager-host",
                        "swarmNodeId": "manager-node-id",
                        "labels": {"luma.node.name": "manager-host", "luma.node.id": "manager-node-id", "region": "cn"},
                    }
                }
                save_state(state)

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", side_effect=LumaError("prep failed")
                ), patch("luma.control.server.remove_stack") as remove:
                    with self.assertRaisesRegex(LumaError, "failed to prepare managed NFS storage"):
                        handle_storage_set(
                            state["deployToken"],
                            {"name": "bad-nfs", "node": "manager-host", "path": "/srv/luma"},
                        )
                self.assertNotIn("bad-nfs", load_state().get("storageClasses", {}))
                remove.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_rejects_remote_managed_storage_when_agent_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command"
                ) as host_prep:
                    with self.assertRaisesRegex(LumaError, "node agent is not ready"):
                        handle_storage_set(
                            state["deployToken"],
                            {"name": "worker-nfs", "node": "worker-storage", "path": "/srv/luma"},
                        )
                host_prep.assert_not_called()
                self.assertNotIn("worker-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_uses_remote_node_agent_before_saving_storage_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                handle_node_agent_lease(
                    agent_token,
                    {
                        "nodeName": "worker-storage",
                        "nodeId": "worker-node-id",
                        "os": "linux",
                        "capabilities": ["nfs-host", "managed-volume-path"],
                        "waitSeconds": 0,
                    },
                )

                errors = []

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                def agent_worker():
                    try:
                        for _ in range(10):
                            leased = handle_node_agent_lease(
                                agent_token,
                                {
                                    "nodeName": "worker-storage",
                                    "nodeId": "worker-node-id",
                                    "os": "linux",
                                    "capabilities": ["nfs-host", "managed-volume-path"],
                                    "waitSeconds": 1,
                                },
                            ).get("task")
                            if not leased:
                                continue
                            self.assertEqual(leased["action"], "prepare-managed-nfs-host")
                            self.assertEqual(leased["payload"]["path"], "/srv/luma")
                            handle_node_agent_complete(
                                agent_token,
                                {
                                    "nodeName": "worker-storage",
                                    "nodeId": "worker-node-id",
                                    "taskId": leased["id"],
                                    "status": "succeeded",
                                    "message": "prepared",
                                    "result": {"message": "host NFS export ready"},
                                },
                            )
                            return
                        errors.append("agent did not receive task")
                    except Exception as exc:
                        errors.append(str(exc))

                thread = threading.Thread(target=agent_worker)
                thread.start()
                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command"
                ) as host_prep:
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "worker-nfs", "node": "worker-storage", "path": "/srv/luma"},
                    )
                thread.join(timeout=5)
                self.assertFalse(errors)
                host_prep.assert_not_called()
                self.assertTrue(result["saved"])
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export ready")
                self.assertIn("worker-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_join_token_cannot_issue_agent_token_by_node_name_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                with self.assertRaisesRegex(LumaError, "nodeId is required"):
                    handle_node_agent_token(state["joinToken"], {"nodeName": "worker-storage"})
                issued = handle_node_agent_token(state["joinToken"], {"nodeName": "anything", "nodeId": "worker-node-id"})
                self.assertEqual(issued["nodeName"], "worker-storage")
                self.assertTrue(issued["agentToken"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_agent_token_issuance_reports_provisioned_until_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                    }
                }
                save_state(state)
                handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage"})
                status = handle_control_status(state["deployToken"])
                item = status["nodes"]["items"][0]
                self.assertEqual(item["agentStatus"], "provisioned")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_agent_long_poll_does_not_drop_task_queued_while_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                leased: list[dict[str, object] | None] = []
                errors: list[str] = []

                def lease_worker():
                    try:
                        result = handle_node_agent_lease(
                            agent_token,
                            {
                                "nodeName": "worker-storage",
                                "nodeId": "worker-node-id",
                                "os": "linux",
                                "capabilities": ["nfs-host"],
                                "waitSeconds": 2,
                            },
                        )
                        leased.append(result.get("task"))
                    except Exception as exc:
                        errors.append(str(exc))

                thread = threading.Thread(target=lease_worker)
                thread.start()
                time.sleep(0.2)
                current = load_state()
                current.setdefault("agentTasks", {})["task-race"] = {
                    "id": "task-race",
                    "nodeName": "worker-storage",
                    "action": "prepare-managed-nfs-host",
                    "payload": {"name": "race", "path": "/srv/luma"},
                    "status": "queued",
                }
                save_state(current)
                thread.join(timeout=5)
                self.assertFalse(errors)
                self.assertTrue(leased)
                self.assertIsNotNone(leased[0])
                self.assertEqual(leased[0]["id"], "task-race")
                self.assertEqual(load_state()["agentTasks"]["task-race"]["status"], "running")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_host_prep_runs_privileged_chroot_container(self):
        docker_calls = []
        raw_calls = []

        def fake_docker_request(method, path, body=None):
            docker_calls.append((method, path, body))
            if method == "GET" and path == "/images/ubuntu%3A22.04/json":
                return {}
            if method == "POST" and path.startswith("/containers/create"):
                return {"Id": "container-id"}
            if method == "POST" and path == "/containers/container-id/start":
                return None
            if method == "POST" and path == "/containers/container-id/wait":
                return {"StatusCode": 0}
            if method == "DELETE" and path.startswith("/containers/container-id"):
                return None
            raise AssertionError(f"unexpected Docker request: {method} {path}")

        def fake_docker_request_raw(method, path, headers=None):
            raw_calls.append((method, path, headers))
            if method == "GET" and path.startswith("/containers/container-id/logs"):
                return 200, "prepared"
            raise AssertionError(f"unexpected raw Docker request: {method} {path}")

        with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_docker_request_raw
        ):
            result = _run_host_prep_container("echo ready")

        self.assertEqual(result, "prepared")
        create_body = next(body for method, path, body in docker_calls if method == "POST" and path.startswith("/containers/create"))
        self.assertEqual(create_body["Cmd"], ["chroot", "/host", "bash", "-lc", "echo ready"])
        self.assertTrue(create_body["HostConfig"]["Privileged"])
        self.assertEqual(create_body["HostConfig"]["PidMode"], "host")
        self.assertEqual(create_body["HostConfig"]["NetworkMode"], "host")
        self.assertIn("/:/host", create_body["HostConfig"]["Binds"])
        self.assertTrue(any(method == "DELETE" and path.startswith("/containers/container-id") for method, path, _ in docker_calls))

    def test_storage_remove_removes_managed_storage_stack_when_portainer_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "cn-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "cn-node",
                        "path": "/srv/luma",
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_stack", return_value="Portainer stack removed: luma-storage-cn-nfs") as remove:
                    result = handle_storage_remove(state["deployToken"], {"name": "cn-nfs"})
                remove.assert_called_once()
                self.assertEqual(remove.call_args.args[1].name, "luma-storage-cn-nfs")
                self.assertEqual(result["storageHost"]["removed"], "Portainer stack removed: luma-storage-cn-nfs")
                self.assertNotIn("cn-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_resolves_storage_class_from_control_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "external",
                        "endpoint": "home-nas:/srv/luma",
                        "regions": ["cn"],
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "pg-data", "initialize": "empty"}},
                    }
                )
                with patch("luma.control.server.upsert_stack", return_value="Portainer stack updated") as upsert:
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipWebhook": False},
                    )
                stack_text = upsert.call_args.args[2]
                self.assertIn("home-nas", stack_text)
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "home-nfs")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_prepares_managed_storage_paths_before_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-nas": {
                        "name": "home-nas",
                        "region": "cn",
                        "status": "labeled",
                        "swarmNodeId": "home-node-id",
                    }
                }
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-nas",
                        "path": "/srv/luma",
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "app-stack/pg-data", "initialize": "empty"}},
                    }
                )

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "home-nas", "Swarm": {"NodeID": "home-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep:
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipWebhook": True},
                    )
                commands = [call.args[0] for call in host_prep.call_args_list]
                self.assertTrue(any("nfs-kernel-server" in command for command in commands))
                self.assertTrue(any("/srv/luma/app-stack/pg-data" in command for command in commands))
                self.assertEqual(result["storagePreparation"][0]["prepared"], "host NFS export ready")
                self.assertEqual(result["storagePreparation"][1]["prepared"], "volume path ready")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_set_prepares_managed_host_and_removes_legacy_storage_stack_when_portainer_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["nodes"] = {
                    "cn-node": {
                        "name": "cn-node",
                        "region": "cn",
                        "status": "labeled",
                        "swarmHostname": "cn-node",
                        "swarmNodeId": "cn-node-id",
                        "labels": {"luma.node.name": "cn-node", "luma.node.id": "cn-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "cn-node", "Swarm": {"NodeID": "cn-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ), patch("luma.control.server.remove_stack", return_value="Portainer stack not found: luma-storage-cn-nfs") as remove:
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "provider": "nfs", "node": "cn-node", "path": "/srv/luma"},
                    )
                remove.assert_called_once()
                self.assertEqual(remove.call_args.args[1].name, "luma-storage-cn-nfs")
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export ready")
                self.assertEqual(result["storageHost"]["legacyStack"], "Portainer stack not found: luma-storage-cn-nfs")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_apply_only_targets_storage_classes_referenced_by_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["nodes"] = {
                    "home-nas": {"name": "home-nas", "region": "cn", "status": "labeled"},
                    "archive-nas": {"name": "archive-nas", "region": "cn", "status": "labeled"},
                }
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-nas",
                        "path": "/srv/luma",
                    },
                    "archive-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "archive-nas",
                        "path": "/srv/archive",
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "pg-data"}},
                    }
                )
                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "home-nas", "Swarm": {"NodeID": "home-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep:
                    result = handle_storage_apply(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
                self.assertEqual(len(result["storage"]["storageClasses"]), 1)
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "home-nfs")
                commands = [call.args[0] for call in host_prep.call_args_list]
                self.assertTrue(any("nfs-kernel-server" in command for command in commands))
                self.assertTrue(any("/srv/luma/pg-data" in command for command in commands))
                self.assertFalse(any("/srv/archive" in command for command in commands))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_apply_rejects_unsafe_managed_volume_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"home-nas": {"name": "home-nas", "region": "cn", "status": "labeled"}}
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-nas",
                        "path": "/srv/luma",
                    }
                }
                save_state(state)
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "../escape"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "relative and cannot contain"):
                    handle_storage_apply(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_registry_credentials_are_saved_without_returning_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                saved = handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                self.assertEqual(saved, {"host": "ghcr.io", "username": "octo", "saved": True})
                listed = handle_registry_list(state["deployToken"])
                serialized = json.dumps(listed)
                self.assertIn("ghcr.io", serialized)
                self.assertIn("octo", serialized)
                self.assertNotIn("ghp_secret", serialized)
                self.assertEqual(load_state()["registries"]["ghcr.io"]["password"], "ghp_secret")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_registry_remove_cleans_luma_portainer_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                with patch("luma.control.server.remove_luma_portainer_registry", return_value=True) as cleanup:
                    result = handle_registry_remove(state["deployToken"], {"host": "ghcr.io"})
                self.assertTrue(result["removed"])
                self.assertTrue(result["portainerRegistryRemoved"])
                cleanup.assert_called_once()
                self.assertNotIn("ghcr.io", load_state().get("registries", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_uses_registry_auth_for_pull_and_portainer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/private-api:1",
                        "region": "cn",
                        "exposure": "none",
                    }
                )
                captured = {}

                def fake_resolve(_config, service, **kwargs):
                    captured["image_auth"] = kwargs.get("registry_auth")
                    return service, {"requested": service.image, "selected": service.image, "registryAuth": bool(kwargs.get("registry_auth"))}

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image", side_effect=fake_resolve
                ), patch(
                    "luma.control.server.deploy_with_portainer", return_value="Portainer deploy triggered"
                ) as deploy:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipWebhook": False},
                    )
                self.assertTrue(result["image"]["registryAuth"])
                self.assertEqual(captured["image_auth"]["username"], "octo")
                deploy.assert_called_once()
                self.assertEqual(deploy.call_args.kwargs["registry_auth"]["password"], "ghp_secret")
                stack = (root / "stacks" / "cn" / "api" / "stack.yml").read_text(encoding="utf-8")
                self.assertNotIn("ghp_secret", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_tailscale_relay_follows_actual_home_task_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "home-panel",
                        "image": "nginx:alpine",
                        "region": "home",
                        "exposure": "tailscale-relay",
                        "domain": "panel.example.com",
                        "port": 8080,
                    }
                )
                docker_nodes = [
                    {
                        "ID": "home-1",
                        "Description": {"Hostname": "orbstack"},
                        "Spec": {"Labels": {"region": "home"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.2"},
                    },
                    {
                        "ID": "home-2",
                        "Description": {"Hostname": "m3max"},
                        "Spec": {"Labels": {"region": "home"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.3"},
                    },
                ]
                docker_tasks = [
                    {
                        "NodeID": "home-2",
                        "DesiredState": "running",
                        "Status": {"State": "running"},
                    },
                ]
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.docker_request", side_effect=[docker_nodes, docker_tasks]
                ), patch(
                    "luma.control.server.resolve_service_image",
                    side_effect=lambda _config, service, **_kwargs: (service, {"requested": service.image, "selected": service.image}),
                ), patch(
                    "luma.control.server.deploy_with_portainer",
                    return_value="Portainer stack updated",
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "home-panel.yaml", "skipDns": True},
                    )
                route = (root / "routes" / "home-panel.yml").read_text(encoding="utf-8")
                self.assertIn("http://100.64.0.3:8080", route)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Resolve relay=ok", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_tailscale_relay_follows_actual_home_task_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump({"services": {"nextcloud": {"image": "nextcloud:apache"}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "nextcloud",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "nextcloud": {
                                "region": "home",
                                "exposure": "tailscale-relay",
                                "domain": "next.example.com",
                                "port": 80,
                            }
                        },
                    }
                )
                docker_nodes = [
                    {
                        "ID": "home-1",
                        "Description": {"Hostname": "orbstack"},
                        "Spec": {"Labels": {"region": "home"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.2"},
                    }
                ]
                docker_tasks = [
                    {
                        "NodeID": "home-1",
                        "DesiredState": "running",
                        "Status": {"State": "running"},
                    }
                ]
                with patch("luma.control.server.upsert_stack", return_value="Portainer stack updated"), patch(
                    "luma.control.server.sync_dns", return_value="DNS synced"
                ), patch("luma.control.server._probe_public_route", return_value="Public route probe skipped"), patch(
                    "luma.control.server.docker_request", side_effect=[docker_nodes, docker_tasks]
                ):
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
                route = (root / "routes" / "nextcloud-nextcloud.yml").read_text(encoding="utf-8")
                self.assertIn("http://100.64.0.2:80", route)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Resolve relay nextcloud=ok", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_fails_when_referenced_secret_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_db = os.environ.get("DATABASE_URL")
            try:
                os.environ.pop("DATABASE_URL", None)
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"providers": {"dns": {"type": "cloudflare", "zone": "example.com"}}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), self.assertRaises(LumaError):
                    handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("DATABASE_URL", old_db)

    def test_secret_list_hides_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://secret"})
                result = handle_secret_list(state["deployToken"])
                self.assertEqual(result, {"secrets": ["DATABASE_URL"]})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_skip_flags_are_honored_by_control_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.sync_dns"
                ) as sync, patch("luma.control.server.deploy_with_portainer") as webhook:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipWebhook": True},
                    )
                sync.assert_not_called()
                webhook.assert_not_called()
                self.assertEqual(result["dns"], "DNS skipped: --skip-dns")
                self.assertEqual(result["webhook"], "Portainer deploy skipped: --skip-webhook")
                self.assertEqual(result["probe"], "Public route probe skipped: --skip-webhook")
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Sync DNS=ok:DNS skipped: --skip-dns", steps)
                self.assertIn("Deploy Portainer stack=ok:Portainer deploy skipped: --skip-webhook", steps)
                self.assertIn("Probe public route=ok:Public route probe skipped: --skip-webhook", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_renders_latest_as_resolved_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/api:latest",
                        "region": "cn",
                        "exposure": "none",
                    }
                )
                digest = "ghcr.io/acme/api@sha256:abc123"

                def fake_raw(method, path, *, headers=None):
                    if method == "GET":
                        return 200, json.dumps({"RepoDigests": [digest]})
                    return 200, "{}"

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.docker_request_raw", side_effect=fake_raw
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipWebhook": True},
                    )
                stack = (root / "stacks" / "cn" / "api" / "stack.yml").read_text(encoding="utf-8")
                self.assertEqual(result["image"]["requested"], "ghcr.io/acme/api:latest")
                self.assertEqual(result["image"]["deployed"], digest)
                self.assertIn(f"image: {digest}", stack)
                self.assertNotIn("ghcr.io/acme/api:latest", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_image_falls_back_to_domestic_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "traefik/whoami:latest",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"defaults": {"imageMirrors": ["mirror.local"]}}, None)
            with patch("luma.control.server.ensure_image_present") as ensure:
                ensure.side_effect = [LumaError("upstream failed"), "mirror.local/traefik/whoami@sha256:abc123"]
                selected, result = resolve_service_image(config, service)
            self.assertEqual(selected.image, "mirror.local/traefik/whoami@sha256:abc123")
            self.assertEqual(result["selected"], "mirror.local/traefik/whoami:latest")
            self.assertEqual(result["deployed"], "mirror.local/traefik/whoami@sha256:abc123")
            self.assertTrue(result["fallback"])

    def test_service_image_pull_sends_registry_auth_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/private-api:1",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({}, None)
            calls = []

            def fake_raw(method, path, *, headers=None):
                calls.append((method, path, headers or {}))
                if method == "GET":
                    return 404, ""
                return 200, "{}"

            with patch("luma.control.server.docker_request_raw", side_effect=fake_raw):
                selected, result = resolve_service_image(
                    config,
                    service,
                    registry_auth={"username": "octo", "password": "ghp_secret1", "serveraddress": "ghcr.io"},
                )
            self.assertEqual(selected.image, "ghcr.io/acme/private-api:1")
            self.assertTrue(result["registryAuth"])
            pull_headers = calls[-1][2]
            self.assertIn("X-Registry-Auth", pull_headers)
            self.assertTrue(pull_headers["X-Registry-Auth"].endswith("="))
            decoded = json.loads(base64.b64decode(pull_headers["X-Registry-Auth"], validate=True).decode("utf-8"))
            self.assertEqual(decoded["username"], "octo")
            self.assertEqual(decoded["password"], "ghp_secret1")
            self.assertEqual(decoded["serveraddress"], "ghcr.io")

    def test_image_pull_egress_registry_whitelist(self):
        self.assertTrue(image_pull_requires_egress("ghcr.io/acme/api:latest"))
        self.assertTrue(image_pull_requires_egress("nginx:alpine"))
        self.assertFalse(image_pull_requires_egress("docker.1panel.live/library/nginx:alpine"))

    def test_image_pull_egress_configures_daemon_proxy_through_node_agent(self):
        state = {
            "nodes": {
                "manager-1": {
                    "swarmNodeId": "node-1",
                    "swarmHostname": "manager-host",
                    "agent": {
                        "status": "online",
                        "lastSeen": int(time.time()),
                        "capabilities": ["docker-egress-proxy"],
                    },
                }
            }
        }
        docker_calls = []

        def fake_docker(method, path, body=None):
            docker_calls.append((method, path))
            if path == "/services/egress_mihomo":
                return {"ID": "egress"}
            if path.startswith("/tasks?"):
                return [{"Status": {"State": "running"}}]
            if path == "/info":
                if len([call for call in docker_calls if call[1] == "/info"]) == 1:
                    return {"Name": "manager-host", "Swarm": {"NodeID": "node-1"}}
                return {"Name": "manager-host", "Swarm": {"NodeID": "node-1"}, "HTTPProxy": "http://127.0.0.1:7890"}
            raise AssertionError(path)

        with patch("luma.control.server.docker_request", side_effect=fake_docker), patch(
            "luma.control.server._run_node_agent_task",
            return_value={"message": "Docker daemon egress proxy configured"},
        ) as agent:
            result = ensure_image_pull_egress_proxy(state, "ghcr.io/acme/api:latest")
        agent.assert_called_once()
        self.assertEqual(agent.call_args.args[2], "configure-docker-egress-proxy")
        self.assertEqual(agent.call_args.kwargs["required_capability"], "docker-egress-proxy")
        self.assertEqual(result, "Docker daemon egress proxy configured")

    def test_latest_service_image_is_pulled_even_when_present_locally(self):
        calls = []
        digest = "ghcr.io/acme/api@sha256:abc123"

        def fake_raw(method, path, *, headers=None):
            calls.append((method, path, headers or {}))
            if method == "GET":
                return 200, json.dumps({"RepoDigests": [digest]})
            return 200, "{}"

        with patch("luma.control.server.docker_request_raw", side_effect=fake_raw):
            resolved = ensure_image_present("ghcr.io/acme/api:latest", force_pull=True)
        self.assertEqual(resolved, digest)
        self.assertEqual([method for method, _path, _headers in calls], ["POST", "GET"])
        self.assertIn("/images/create?fromImage=ghcr.io%2Facme%2Fapi%3Alatest", calls[0][1])
        self.assertEqual(calls[1][1], "/images/ghcr.io%2Facme%2Fapi%3Alatest/json")

    def test_pinned_service_image_uses_local_cache_when_present(self):
        calls = []

        def fake_raw(method, path, *, headers=None):
            calls.append((method, path, headers or {}))
            return 200, "{}"

        with patch("luma.control.server.docker_request_raw", side_effect=fake_raw):
            ensure_image_present("ghcr.io/acme/api:1.0.0")
        self.assertEqual([method for method, _path, _headers in calls], ["GET"])

    def test_service_image_pull_error_points_to_daemon_proxy_for_registry_network_failures(self):
        def fake_raw(method, path, *, headers=None):
            if method == "GET":
                return 404, ""
            return 500, '{"message":"failed to do request: Head \\"https://ghcr.io/v2/acme/api/manifests/latest\\": EOF"}'

        with patch("luma.control.server.docker_request_raw", side_effect=fake_raw), self.assertRaisesRegex(
            LumaError,
            "Docker daemon could not reach the registry",
        ):
            ensure_image_present("ghcr.io/acme/api:latest", force_pull=True)

    def test_config_webhook_mapping_uses_service_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "API Service",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"providers": {"portainer": {"webhooks": {"api-service": "PORTAINER_WEBHOOK_API"}}}}, None)
            import os

            old_api = os.environ.get("PORTAINER_WEBHOOK_API")
            try:
                os.environ["PORTAINER_WEBHOOK_API"] = "https://portainer/api"
                webhook_url, webhook_env = resolve_webhook(config, service)
                self.assertEqual(webhook_url, "https://portainer/api")
                self.assertEqual(webhook_env, "PORTAINER_WEBHOOK_API")
            finally:
                _restore_env("PORTAINER_WEBHOOK_API", old_api)

    def test_missing_webhook_uses_portainer_api_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "API Service",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"providers": {"portainer": {"webhookUrlEnv": "PORTAINER_WEBHOOK_URL"}}}, None)
            state = {
                "portainerApiUrl": "https://portainer.example.com/api",
                "portainerAdminUsername": "admin",
                "portainerAdminPassword": "secret",
                "portainerEndpointId": 1,
                "swarmId": "swarm-test",
            }
            client = Mock()
            client.authenticate.return_value = "jwt"
            client.request.side_effect = [
                [],
                {"Id": 7, "Name": "api-service"},
            ]
            with patch("luma.portainer.PortainerApi", return_value=client):
                result = upsert_stack(config, service, "services: {}", state, missing_webhook_env="PORTAINER_WEBHOOK_URL")
            self.assertIn("Portainer stack created", result)
            client.request.assert_any_call(
                "POST",
                "/stacks/create/swarm/string?endpointId=1",
                {
                    "Name": "api-service",
                    "StackFileContent": "services: {}",
                    "SwarmID": "swarm-test",
                    "Env": [],
                },
                token="jwt",
            )

    def test_portainer_stack_remove_deletes_existing_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump({"name": "api-service", "image": "nginx:alpine", "region": "cn", "exposure": "none"}),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({}, None)
            state = {
                "portainerApiUrl": "https://portainer.example.com/api",
                "portainerAdminUsername": "admin",
                "portainerAdminPassword": "secret",
                "portainerEndpointId": 1,
            }
            client = Mock()
            client.authenticate.return_value = "jwt"
            client.request.side_effect = [[{"Id": 7, "Name": "api-service"}], None]
            with patch("luma.portainer.PortainerApi", return_value=client):
                result = remove_stack(config, service, state)
            self.assertEqual(result, "Portainer stack removed: api-service")
            client.request.assert_any_call("DELETE", "/stacks/7?endpointId=1", token="jwt")

    def test_portainer_stack_create_links_registry_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "private-api",
                        "image": "ghcr.io/acme/private-api:1",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"providers": {"portainer": {"webhookUrlEnv": "PORTAINER_WEBHOOK_URL"}}}, None)
            state = {
                "portainerApiUrl": "https://portainer.example.com/api",
                "portainerAdminUsername": "admin",
                "portainerAdminPassword": "secret",
                "portainerEndpointId": 1,
                "swarmId": "swarm-test",
            }
            client = Mock()
            client.authenticate.return_value = "jwt"
            client.request.side_effect = [
                [],
                {"Id": 42},
                None,
                [],
                {"Id": 7, "Name": "private-api"},
            ]
            registry_auth = {"username": "octo", "password": "ghp_secret", "serveraddress": "ghcr.io"}
            with patch("luma.portainer.PortainerApi", return_value=client):
                result = upsert_stack(
                    config,
                    service,
                    "services: {}",
                    state,
                    missing_webhook_env="PORTAINER_WEBHOOK_URL",
                    registry_auth=registry_auth,
                )
            self.assertIn("Portainer stack created", result)
            client.request.assert_any_call(
                "POST",
                "/registries",
                {
                    "Name": "luma-ghcr-io",
                    "URL": "ghcr.io",
                    "Authentication": True,
                    "Username": "octo",
                    "Password": "ghp_secret",
                    "Type": 3,
                    "TLS": True,
                },
                token="jwt",
            )
            client.request.assert_any_call(
                "PUT",
                "/endpoints/1/registries/42",
                {"UserAccessPolicies": {}, "TeamAccessPolicies": {}, "Namespaces": []},
                token="jwt",
            )
            client.request.assert_any_call(
                "POST",
                "/stacks/create/swarm/string?endpointId=1",
                {
                    "Name": "private-api",
                    "StackFileContent": "services: {}",
                    "SwarmID": "swarm-test",
                    "Env": [],
                },
                token="jwt",
                headers={"X-Registry-Auth": base64.b64encode(b'{"registryId":42}').decode("ascii")},
            )

    def test_ghcr_uses_custom_registry_type_for_default_portainer_ce(self):
        self.assertEqual(registry_provider_type("ghcr.io"), 3)

    def test_portainer_registry_does_not_overwrite_foreign_same_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "private-api",
                        "image": "ghcr.io/acme/private-api:1",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"providers": {"portainer": {"webhookUrlEnv": "PORTAINER_WEBHOOK_URL"}}}, None)
            state = {
                "portainerApiUrl": "https://portainer.example.com/api",
                "portainerAdminUsername": "admin",
                "portainerAdminPassword": "secret",
                "portainerEndpointId": 1,
                "swarmId": "swarm-test",
            }
            client = Mock()
            client.authenticate.return_value = "jwt"
            client.request.side_effect = [
                [{"Id": 9, "Name": "manually-managed", "URL": "ghcr.io"}],
                {"Id": 42},
                None,
                [],
                {"Id": 7, "Name": "private-api"},
            ]
            registry_auth = {"username": "octo", "password": "ghp_secret", "serveraddress": "ghcr.io"}
            with patch("luma.portainer.PortainerApi", return_value=client):
                upsert_stack(
                    config,
                    service,
                    "services: {}",
                    state,
                    missing_webhook_env="PORTAINER_WEBHOOK_URL",
                    registry_auth=registry_auth,
                )
            self.assertFalse(any(call.args[:2] == ("PUT", "/registries/9") for call in client.request.call_args_list))
            client.request.assert_any_call(
                "POST",
                "/registries",
                {
                    "Name": "luma-ghcr-io",
                    "URL": "ghcr.io",
                    "Authentication": True,
                    "Username": "octo",
                    "Password": "ghp_secret",
                    "Type": 3,
                    "TLS": True,
                },
                token="jwt",
            )

    def test_portainer_stack_update_pulls_with_registry_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "private-api",
                        "image": "ghcr.io/acme/private-api:1",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"providers": {"portainer": {"webhookUrlEnv": "PORTAINER_WEBHOOK_URL"}}}, None)
            state = {
                "portainerApiUrl": "https://portainer.example.com/api",
                "portainerAdminUsername": "admin",
                "portainerAdminPassword": "secret",
                "portainerEndpointId": 1,
                "swarmId": "swarm-test",
            }
            client = Mock()
            client.authenticate.return_value = "jwt"
            client.request.side_effect = [
                [{"Id": 42, "Name": "luma-ghcr-io", "URL": "ghcr.io"}],
                None,
                None,
                [{"Id": 7, "Name": "private-api"}],
                None,
            ]
            registry_auth = {"username": "octo", "password": "ghp_secret", "serveraddress": "ghcr.io"}
            with patch("luma.portainer.PortainerApi", return_value=client):
                result = upsert_stack(
                    config,
                    service,
                    "services: {}",
                    state,
                    missing_webhook_env="PORTAINER_WEBHOOK_URL",
                    registry_auth=registry_auth,
                )
            self.assertIn("Portainer stack updated", result)
            client.request.assert_any_call(
                "PUT",
                "/stacks/7?endpointId=1",
                {
                    "StackFileContent": "services: {}",
                    "Env": [],
                    "Prune": True,
                    "PullImage": True,
                },
                token="jwt",
                headers={"X-Registry-Auth": base64.b64encode(b'{"registryId":42}').decode("ascii")},
            )

    def test_portainer_stack_update_pulls_latest_without_registry_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:latest",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"providers": {"portainer": {"webhookUrlEnv": "PORTAINER_WEBHOOK_URL"}}}, None)
            state = {
                "portainerApiUrl": "https://portainer.example.com/api",
                "portainerAdminUsername": "admin",
                "portainerAdminPassword": "secret",
                "portainerEndpointId": 1,
                "swarmId": "swarm-test",
            }
            client = Mock()
            client.authenticate.return_value = "jwt"
            client.request.side_effect = [
                [{"Id": 7, "Name": "api"}],
                None,
            ]
            with patch("luma.portainer.PortainerApi", return_value=client):
                result = upsert_stack(config, service, "services: {}", state, missing_webhook_env="PORTAINER_WEBHOOK_URL")
            self.assertIn("Portainer stack updated", result)
            client.request.assert_any_call(
                "PUT",
                "/stacks/7?endpointId=1",
                {
                    "StackFileContent": "services: {}",
                    "Env": [],
                    "Prune": True,
                    "PullImage": True,
                },
                token="jwt",
            )

    def test_portainer_stack_update_pulls_digest_without_registry_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx@sha256:abc123",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"providers": {"portainer": {"webhookUrlEnv": "PORTAINER_WEBHOOK_URL"}}}, None)
            state = {
                "portainerApiUrl": "https://portainer.example.com/api",
                "portainerAdminUsername": "admin",
                "portainerAdminPassword": "secret",
                "portainerEndpointId": 1,
                "swarmId": "swarm-test",
            }
            client = Mock()
            client.authenticate.return_value = "jwt"
            client.request.side_effect = [
                [{"Id": 7, "Name": "api"}],
                None,
            ]
            with patch("luma.portainer.PortainerApi", return_value=client):
                result = upsert_stack(config, service, "services: {}", state, missing_webhook_env="PORTAINER_WEBHOOK_URL")
            self.assertIn("Portainer stack updated", result)
            client.request.assert_any_call(
                "PUT",
                "/stacks/7?endpointId=1",
                {
                    "StackFileContent": "services: {}",
                    "Env": [],
                    "Prune": True,
                    "PullImage": True,
                },
                token="jwt",
            )

    def test_remove_luma_portainer_registry_deletes_only_luma_owned_match(self):
        config = LumaConfig({}, None)
        state = {
            "portainerApiUrl": "https://portainer.example.com/api",
            "portainerAdminUsername": "admin",
            "portainerAdminPassword": "secret",
        }
        client = Mock()
        client.authenticate.return_value = "jwt"
        client.request.side_effect = [
            [
                {"Id": 9, "Name": "manually-managed", "URL": "ghcr.io"},
                {"Id": 42, "Name": "luma-ghcr-io", "URL": "ghcr.io"},
            ],
            None,
        ]
        with patch("luma.portainer.PortainerApi", return_value=client):
            removed = remove_luma_portainer_registry(config, state, "ghcr.io")
        self.assertTrue(removed)
        client.request.assert_any_call("DELETE", "/registries/42", token="jwt")
        self.assertFalse(any(call.args[:2] == ("DELETE", "/registries/9") for call in client.request.call_args_list))


def _docker_service(service_id, name, image, replicas, constraints, labels):
    return {
        "ID": service_id,
        "Spec": {
            "Name": name,
            "Labels": labels,
            "Mode": {"Replicated": {"Replicas": replicas}},
            "TaskTemplate": {
                "ContainerSpec": {"Image": image},
                "Placement": {"Constraints": constraints},
            },
        },
    }


def _docker_task(service_id, node_id, state):
    return {
        "ServiceID": service_id,
        "NodeID": node_id,
        "DesiredState": "running",
        "Status": {"State": state},
    }


def _restore_env(key, value):
    import os

    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


def _set_env(key, value):
    import os

    old = os.environ.get(key)
    os.environ[key] = value
    return old


if __name__ == "__main__":
    unittest.main()
