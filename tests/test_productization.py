import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from luma.assets import asset_text
from luma.config import LumaConfig
from luma.bootstrap import _acme_email, _last_command_value, _traefik_ports
from luma.control.client import ControlClient
from luma.control.context import load_current_context
from luma.control.server import handle_deployment, handle_node_label, handle_node_register, resolve_service_image
from luma.control.state import init_state
from luma.envfile import load_env_file
from luma.egress import minimal_mihomo_config_from_bytes
from luma.errors import LumaError
from luma.portainer import resolve_webhook, upsert_stack
from luma.profiles import PROFILES
from luma.service import load_service
from luma.cli import build_parser, main
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
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)
                ), patch("builtins.input", return_value="ops@example.com"), patch(
                    "luma.cli.bootstrap_manager_local", return_value=[]
                ):
                    code = main(["bootstrap", "manager", "--domain", "luma.example.com", "--profile", "single-node"])
                self.assertEqual(code, 0)
                self.assertTrue(config_path.exists())
                self.assertIn("EGRESS_SUBSCRIPTION_URL", configured_keys(config_path))
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_cf)
                _restore_env("TRAEFIK_ACME_EMAIL", old_email)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("EGRESS_SUBSCRIPTION_URL", old_egress)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

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
