import base64
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import yaml

from luma import __version__
from luma.assets import asset_text
from luma.config import LumaConfig
from luma.cloudflare import sync_control_dns
from luma.bootstrap import (
    _acme_email,
    _ensure_control_image,
    _force_update_service_image,
    _is_tailscale_manager_addr,
    _last_command_value,
    _portainer_agent_image_candidates,
    _reset_portainer_state,
    _traefik_ports,
    bootstrap_node,
    bootstrap_manager_local,
    initialize_portainer,
    install_control_config,
    install_docker,
    setup_tailscale,
)
from luma.control.client import ControlClient
from luma.control.context import load_current_context, save_context
from luma.control.server import handle_control_status, handle_deployment, handle_node_label, handle_node_register, handle_secret_list, handle_secret_set, resolve_service_image
from luma.control.state import init_state, load_state
from luma.envfile import load_env_file
from luma.egress import minimal_mihomo_config_from_bytes
from luma.errors import LumaError
from luma.portainer import resolve_webhook, upsert_stack
from luma.profiles import PROFILES
from luma.service import load_service
from luma.cli import _node_join_examples, _portainer_url_from_state, build_parser, exit_local_node, main
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
        self.assertEqual(generated["proxy-groups"][0]["proxies"], ["proxy-a"])


class CliTests(unittest.TestCase):
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

    def test_update_manager_installs_cli_then_refreshes_bootstrap(self):
        completed = Mock(returncode=0)
        with patch("luma.cli._run_luma_installer") as installer, patch(
            "luma.cli._luma_executable", return_value="/usr/local/bin/luma"
        ), patch(
            "luma.cli.subprocess.run", return_value=completed
        ) as run:
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
        run.assert_called_once_with(
            [
                "/usr/local/bin/luma",
                "bootstrap",
                "manager",
                "--domain",
                "luma.example.com",
                "--profile",
                "single-node",
            ],
            check=False,
        )

    def test_update_infers_domain_and_refreshes_when_control_is_older(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "control.json").write_text(
                json.dumps({"clusterId": "luma-test", "domain": "luma.example.com"}) + "\n",
                encoding="utf-8",
            )
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(state_dir))
            completed = Mock(returncode=0)
            try:
                with patch("luma.cli._run_luma_installer") as installer, patch(
                    "luma.cli._luma_executable", return_value="/usr/local/bin/luma"
                ), patch("luma.cli._installed_cli_version", return_value="0.1.10"), patch(
                    "luma.cli._control_version_for_update", return_value="0.1.9"
                ), patch(
                    "luma.cli.subprocess.run", return_value=completed
                ) as run:
                    code = main(["update"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None)
        run.assert_called_once_with(
            [
                "/usr/local/bin/luma",
                "bootstrap",
                "manager",
                "--domain",
                "luma.example.com",
                "--profile",
                "single-node",
            ],
            check=False,
        )

    def test_update_skips_manager_refresh_when_control_matches_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "control.json").write_text(
                json.dumps({"clusterId": "luma-test", "domain": "luma.example.com"}) + "\n",
                encoding="utf-8",
            )
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(state_dir))
            try:
                with patch("luma.cli._run_luma_installer") as installer, patch(
                    "luma.cli._installed_cli_version", return_value="0.1.10"
                ), patch("luma.cli._control_version_for_update", return_value="0.1.10"), patch(
                    "luma.cli.subprocess.run"
                ) as run, patch("builtins.print") as printed:
                    code = main(["update"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None)
        run.assert_not_called()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Manager bootstrap refresh skipped", printed_text)
        self.assertIn("control API already matches CLI version 0.1.10", printed_text)

    def test_update_without_manager_state_updates_cli_only(self):
        with patch("luma.cli._existing_control_state", return_value=None), patch(
            "luma.cli._run_luma_installer"
        ) as installer, patch("luma.cli.subprocess.run") as run, patch("builtins.print") as printed:
            code = main(["update"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None)
        run.assert_not_called()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("CLI updated", printed_text)
        self.assertIn("Manager bootstrap refresh skipped", printed_text)

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
        self.assertIn("Control API: ok (luma-test, 0.1.2)", printed_text)
        self.assertIn("DNS provider: cloudflare", printed_text)
        self.assertIn("DNS ready: yes", printed_text)
        self.assertIn("Portainer ready: yes", printed_text)
        self.assertIn("Registered node details:", printed_text)
        self.assertIn("docker-home\thome\tlabeled\tmini-gaojiu", printed_text)
        self.assertIn("Swarm nodes: 2", printed_text)
        self.assertIn("manager\tmanager\tready\tactive\tcn\tyes", printed_text)

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
                    "luma.cli.ControlClient", return_value=client
                ), patch("luma.cli.join_local_node", return_value=[]), patch(
                    "luma.cli.local_docker_node_name", return_value="worker-1"
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
                client.label_node.assert_called_once_with(node_name="worker-1", region="global", registered_name="global-sg-1")
                self.assertIn("TAILSCALE_AUTHKEY", configured_keys(config_path))
                self.assertIn("LUMA_SUDO_PASSWORD", configured_keys(config_path))
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
                ), patch("luma.cli.ControlClient", return_value=client), patch(
                    "luma.cli.join_local_node", return_value=[]
                ), patch("luma.cli.local_docker_node_name", return_value="docker-home"):
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
                client.label_node.assert_called_once_with(node_name="docker-home", region="home", registered_name="home-mac-mini")
                self.assertIn("TAILSCALE_AUTHKEY", configured_keys(config_path))
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
        ]
        remote.run.return_value = ""

        results = setup_tailscale(node, authkey="ts-key", executor=remote)

        self.assertEqual(results, ["Tailscale connected: luma-mini"])
        commands = [call.args[0] for call in remote.run.call_args_list]
        self.assertTrue(any("tailscale up" in command and "--authkey ts-key" in command for command in commands))

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

    def test_control_client_reports_timeout_without_traceback(self):
        client = ControlClient("https://luma.example.com", "secret")
        with patch("urllib.request.urlopen", side_effect=TimeoutError("read timed out")), self.assertRaises(LumaError) as raised:
            client.deploy(manifest="name: api\nimage: nginx\nregion: cn\nexposure: none\n", source_name="service.yaml", timeout=42)

        self.assertIn("control API timed out after 42s", str(raised.exception))
        self.assertIn("/v1/deployments", str(raised.exception))
        self.assertIn("manager may still be applying", str(raised.exception))

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

    def test_control_image_builds_when_remote_pull_is_unavailable(self):
        remote = Mock()
        remote.run.return_value = ""
        remote.sudo.side_effect = [Exception("pull failed"), "no\n", "build\n", ""]

        result = _ensure_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Control image built: ghcr.io/liutianjie/luma-control:latest")
        self.assertGreaterEqual(remote.upload.call_count, 4)
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker pull ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))
        self.assertTrue(any("docker build" in cmd and "-t ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))

    def test_control_image_pulls_published_image_during_bootstrap(self):
        remote = Mock()
        remote.sudo.return_value = ""

        result = _ensure_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Control image pulled: ghcr.io/liutianjie/luma-control:latest")
        self.assertEqual(remote.upload.call_count, 0)
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker pull ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker build" in cmd for cmd in docker_commands))

    def test_control_service_force_updates_image_after_stack_deploy(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo.return_value = ""

        result = _force_update_service_image(remote, "luma-control_luma-control", "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Service image refreshed: luma-control_luma-control -> ghcr.io/liutianjie/luma-control:latest")
        command = remote.sudo.call_args.args[0]
        self.assertIn("docker service update --image ghcr.io/liutianjie/luma-control:latest --force luma-control_luma-control", command)

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
                self.assertNotIn("egress", labels)
                self.assertNotIn("role.global-worker", labels)
                self.assertEqual(result["nodeName"], "b")
                with self.assertRaises(Exception):
                    handle_node_label(state["deployToken"], {"nodeName": "b", "region": "global"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_label_node_merges_requested_name_into_actual_docker_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_node_register(state["joinToken"], {"nodeName": "global-sg-1", "region": "global"})
                with patch("luma.control.server.label_swarm_node"):
                    result = handle_node_label(
                        state["joinToken"],
                        {"nodeName": "docker-hostname", "registeredName": "global-sg-1", "region": "global"},
                    )
                saved = load_state()
                self.assertEqual(result["nodeName"], "docker-hostname")
                self.assertEqual(result["displayName"], "global-sg-1")
                self.assertNotIn("global-sg-1", saved["nodes"])
                self.assertEqual(saved["nodes"]["docker-hostname"]["displayName"], "global-sg-1")
                self.assertEqual(saved["nodes"]["docker-hostname"]["status"], "labeled")
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

    def test_deployment_uses_control_state_and_portainer_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
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
                with patch("luma.control.server.sync_dns", return_value="DNS updated"), patch(
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
                with self.assertRaises(Exception):
                    handle_deployment(state["joinToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

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
                self.assertTrue(result["portainer"]["ready"])
                self.assertEqual(result["nodes"]["registered"], 2)
                self.assertEqual(result["nodes"]["items"][0]["name"], "docker-home")
                self.assertEqual(result["nodes"]["items"][0]["displayName"], "mini-gaojiu")
                self.assertEqual(result["swarm"]["nodes"][0]["hostname"], "docker-home")
                self.assertEqual(result["swarm"]["nodes"][1]["leader"], True)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

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
                with patch("luma.control.server.sync_dns", return_value="DNS updated"), patch(
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
                with self.assertRaises(LumaError):
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
                with patch("luma.control.server.sync_dns") as sync, patch("luma.control.server.deploy_with_portainer") as webhook:
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
                ensure.side_effect = [LumaError("upstream failed"), None]
                selected, result = resolve_service_image(config, service)
            self.assertEqual(selected.image, "mirror.local/traefik/whoami:latest")
            self.assertTrue(result["fallback"])

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
