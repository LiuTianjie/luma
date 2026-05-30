import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from luma.assets import asset_text
from luma.config import LumaConfig
from luma.bootstrap import (
    _acme_email,
    _ensure_control_image,
    _last_command_value,
    _portainer_agent_image_candidates,
    _traefik_ports,
    bootstrap_node,
    bootstrap_manager_local,
    initialize_portainer,
    install_control_config,
    install_docker,
)
from luma.control.client import ControlClient
from luma.control.context import load_current_context, save_context
from luma.control.server import handle_deployment, handle_node_label, handle_node_register, handle_secret_list, handle_secret_set, resolve_service_image
from luma.control.state import init_state
from luma.envfile import load_env_file
from luma.egress import minimal_mihomo_config_from_bytes
from luma.errors import LumaError
from luma.portainer import resolve_webhook, upsert_stack
from luma.profiles import PROFILES
from luma.service import load_service
from luma.cli import _portainer_url_from_state, build_parser, main
from luma.userconfig import configured_keys, load_user_config


class ProductConfigTests(unittest.TestCase):
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
                self.assertIn("TRAEFIK_ACME_EMAIL", keys)
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertNotIn("cf-token", printed_text)
                self.assertNotIn("sudo-pass", printed_text)
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_bootstrap_prompts_for_missing_manager_config_during_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_cf = _set_env("CLOUDFLARE_API_TOKEN", "")
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

                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)
                ), patch("builtins.input", return_value="ops@example.com"), patch(
                    "luma.cli.bootstrap_manager_local", side_effect=bootstrap_side_effect
                ), patch("builtins.print") as printed:
                    code = main(["bootstrap", "manager", "--domain", "luma.example.com", "--profile", "single-node"])
                self.assertEqual(code, 0)
                self.assertTrue(config_path.exists())
                self.assertIn("EGRESS_SUBSCRIPTION_URL", configured_keys(config_path))
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("Portainer URL: https://203.0.113.10:9443", printed_text)
                self.assertIn("Portainer username: admin", printed_text)
                self.assertIn("Portainer password: sudo jq", printed_text)
                self.assertNotIn("portainer-secret", printed_text)
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_cf)
                _restore_env("TRAEFIK_ACME_EMAIL", old_email)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("EGRESS_SUBSCRIPTION_URL", old_egress)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_portainer_url_from_state_strips_api_suffix(self):
        self.assertEqual(
            _portainer_url_from_state({"portainerApiUrl": "https://203.0.113.10:9443/api"}),
            "https://203.0.113.10:9443",
        )

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
                    "profile": "global-worker",
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
                            "--profile",
                            "global-worker",
                            "--region",
                            "global",
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertIn("TAILSCALE_AUTHKEY", configured_keys(config_path))
                self.assertIn("LUMA_SUDO_PASSWORD", configured_keys(config_path))
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

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

        with patch("luma.bootstrap.bootstrap_node", return_value=["Bootstrap node complete"]), patch(
            "luma.bootstrap.initialize_portainer", side_effect=bind_portainer
        ), patch("luma.bootstrap.install_control_state", side_effect=save_state), patch(
            "luma.bootstrap.local_swarm_join_info",
            return_value={"managerAddr": "127.0.0.1:2377", "swarmJoinToken": "token", "swarmId": "swarm"},
        ), patch("luma.bootstrap.sync_control_dns", side_effect=sync_dns), patch(
            "luma.bootstrap.install_control_config", return_value="Config installed"
        ), patch("luma.bootstrap.deploy_control_stack", return_value="Control deployed"):
            bootstrap_manager_local(config, node, PROFILES["single-node"], "luma.example.com", state)

        self.assertEqual(sequence[:2], ["save", "sync-dns"])
        self.assertGreaterEqual(len(saved_states), 1)
        self.assertEqual(saved_states[0]["portainerAdminPassword"], "secret")

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
        remote.sudo.return_value = "build\n"

        result = _ensure_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Control image built: ghcr.io/liutianjie/luma-control:latest")
        self.assertGreaterEqual(remote.upload.call_count, 4)
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker pull ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))
        self.assertTrue(any("docker build" in cmd and "-t ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))

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
                    handle_node_register(state["deployToken"], {"nodeName": "b", "profile": "global-worker", "region": "global"})
                result = handle_node_register(state["joinToken"], {"nodeName": "b", "profile": "global-worker", "region": "global"})
                self.assertEqual(result["nodeName"], "b")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_join_token_can_label_node_after_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with patch("luma.control.server.label_swarm_node") as label:
                    result = handle_node_label(state["joinToken"], {"nodeName": "b", "profile": "global-worker", "region": "global"})
                label.assert_called_once()
                labels = label.call_args.args[1]
                self.assertEqual(labels["region"], "global")
                self.assertEqual(labels["role.global-worker"], "true")
                self.assertEqual(result["nodeName"], "b")
                with self.assertRaises(Exception):
                    handle_node_label(state["deployToken"], {"nodeName": "b", "profile": "global-worker", "region": "global"})
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
                with patch("luma.control.server.sync_dns", return_value="DNS updated"), patch(
                    "luma.control.server.deploy_with_portainer", return_value="Portainer deploy triggered"
                ):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
                self.assertIn(str(root / "stacks" / "cn" / "api" / "stack.yml"), result["written"])
                with self.assertRaises(Exception):
                    handle_deployment(state["joinToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

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
                with patch("luma.control.server.sync_dns", return_value="DNS updated"), patch(
                    "luma.control.server.deploy_with_portainer", return_value="Portainer deploy triggered"
                ) as deploy:
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
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
                self.assertIsNone(result["dns"])
                self.assertIsNone(result["webhook"])
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
