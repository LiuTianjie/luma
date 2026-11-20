"""Unit tests for Mihomo egress config generation (proxy-providers + failover)."""
from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from luma.egress import (
    DEFAULT_HEALTH_CHECK_INTERVAL,
    DEFAULT_HEALTH_CHECK_TIMEOUT,
    DEFAULT_HEALTH_CHECK_URL,
    DEFAULT_PROVIDER_INTERVAL,
    DEFAULT_PROVIDER_NAME,
    DEFAULT_PROVIDER_PATH,
    DEFAULT_URL_TEST_TOLERANCE,
    ensure_mihomo_direct_domains,
    minimal_mihomo_config_from_bytes,
    minimal_mihomo_config_from_file,
    minimal_mihomo_config_from_text,
    minimal_mihomo_config_from_url,
    minimal_mihomo_provider_config,
)
from luma.errors import LumaError


def _sample_subscription_yaml() -> str:
    return yaml.safe_dump(
        {
            "proxies": [
                {"name": "hk-1", "type": "trojan", "server": "hk.example.com", "port": 443, "password": "secret-a"},
                {"name": "sg-2", "type": "vmess", "server": "sg.example.com", "port": 443, "uuid": "uuid-b"},
            ],
            "proxy-groups": [{"name": "OLD", "type": "select", "proxies": ["hk-1", "sg-2"]}],
            "rule-providers": {"remote": {"type": "http", "url": "https://example.com/rules"}},
            "rules": ["MATCH,OLD"],
        },
        allow_unicode=True,
        sort_keys=False,
    )


def _mock_urlopen_response(payload: bytes) -> MagicMock:
    response = MagicMock()
    response.read.return_value = payload
    cm = MagicMock()
    cm.__enter__.return_value = response
    cm.__exit__.return_value = False
    return cm


class EgressProviderConfigTests(unittest.TestCase):
    def test_url_config_uses_native_proxy_providers_and_failover_groups(self):
        sub_url = "https://sub.example.com/clash?token=abc123"
        payload = _sample_subscription_yaml().encode("utf-8")

        with patch("luma.egress.urllib.request.urlopen", return_value=_mock_urlopen_response(payload)) as urlopen:
            rendered = minimal_mihomo_config_from_url(sub_url)

        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, sub_url)
        self.assertEqual(request.headers.get("User-agent"), "Clash.Meta")

        config = yaml.safe_load(rendered)
        self.assertEqual(config["mixed-port"], 7890)
        self.assertTrue(config["allow-lan"])
        self.assertEqual(config["bind-address"], "0.0.0.0")
        self.assertEqual(config["mode"], "rule")
        self.assertFalse(config["ipv6"])
        self.assertEqual(config["external-controller"], "127.0.0.1:9090")
        self.assertIsInstance(config["secret"], str)
        self.assertGreaterEqual(len(config["secret"]), 32)
        self.assertEqual(config["dns"]["enable"], True)
        self.assertEqual(config["dns"]["listen"], "127.0.0.1:1053")

        # Subscription nodes must be managed dynamically via proxy-providers, not statically frozen.
        self.assertNotIn("proxies", config)
        self.assertNotIn("rule-providers", config)

        providers = config["proxy-providers"]
        self.assertIn(DEFAULT_PROVIDER_NAME, providers)
        provider = providers[DEFAULT_PROVIDER_NAME]
        self.assertEqual(provider["type"], "http")
        self.assertEqual(provider["url"], sub_url)
        self.assertEqual(provider["path"], DEFAULT_PROVIDER_PATH)
        self.assertEqual(provider["interval"], DEFAULT_PROVIDER_INTERVAL)
        self.assertEqual(
            provider["health-check"],
            {
                "enable": True,
                "url": DEFAULT_HEALTH_CHECK_URL,
                "interval": DEFAULT_HEALTH_CHECK_INTERVAL,
                "timeout": DEFAULT_HEALTH_CHECK_TIMEOUT,
                "lazy": True,
            },
        )

        groups = config["proxy-groups"]
        self.assertEqual(len(groups), 2)
        auto_group, fallback_group = groups
        self.assertEqual(
            auto_group,
            {
                "name": "EGRESS-AUTO",
                "type": "url-test",
                "use": [DEFAULT_PROVIDER_NAME],
                "url": DEFAULT_HEALTH_CHECK_URL,
                "interval": DEFAULT_HEALTH_CHECK_INTERVAL,
                "tolerance": DEFAULT_URL_TEST_TOLERANCE,
                "lazy": True,
            },
        )
        self.assertEqual(
            fallback_group,
            {
                "name": "EGRESS",
                "type": "fallback",
                "proxies": ["EGRESS-AUTO"],
                "use": [DEFAULT_PROVIDER_NAME],
                "url": DEFAULT_HEALTH_CHECK_URL,
                "interval": DEFAULT_HEALTH_CHECK_INTERVAL,
                "lazy": True,
            },
        )
        self.assertEqual(config["rules"], ["MATCH,EGRESS"])

    def test_url_config_accepts_base64_subscription_during_validation(self):
        sub_url = "https://sub.example.com/b64"
        encoded = base64.b64encode(_sample_subscription_yaml().encode("utf-8"))

        with patch("luma.egress.urllib.request.urlopen", return_value=_mock_urlopen_response(encoded)):
            config = yaml.safe_load(minimal_mihomo_config_from_url(sub_url))

        self.assertEqual(config["proxy-providers"]["egress-sub"]["url"], sub_url)

    def test_url_config_rejects_invalid_urls_before_network_call(self):
        invalid_urls = [
            "",
            "   ",
            "file:///etc/passwd",
            "ftp://example.com/sub",
            "https://sub.example.com/sub\ninjected: true",
            "https://sub.example.com/with space",
        ]
        for bad_url in invalid_urls:
            with patch("luma.egress.urllib.request.urlopen") as urlopen:
                with self.assertRaises(LumaError):
                    minimal_mihomo_config_from_url(bad_url)
                urlopen.assert_not_called()

    def test_url_config_fails_fast_when_subscription_download_or_payload_is_invalid(self):
        with patch("luma.egress.urllib.request.urlopen", side_effect=OSError("network unreachable")):
            with self.assertRaisesRegex(LumaError, "failed to download egress subscription"):
                minimal_mihomo_config_from_url("https://sub.example.com/down")

        for bad_payload in (b"not yaml", b"proxies: []\n", b"proxies:\n"):
            with patch("luma.egress.urllib.request.urlopen", return_value=_mock_urlopen_response(bad_payload)):
                with self.assertRaisesRegex(LumaError, "failed to download egress subscription"):
                    minimal_mihomo_config_from_url("https://sub.example.com/empty")

    def test_url_config_can_skip_prefetch_validation(self):
        with patch("luma.egress.urllib.request.urlopen") as urlopen:
            config = yaml.safe_load(
                minimal_mihomo_config_from_url("https://sub.example.com/lazy", validate=False)
            )
        urlopen.assert_not_called()
        self.assertEqual(config["proxy-providers"]["egress-sub"]["url"], "https://sub.example.com/lazy")

    def test_provider_config_generates_distinct_secrets_or_uses_env_override(self):
        with patch.dict("os.environ", {"LUMA_EGRESS_SECRET": ""}, clear=False):
            cfg1 = yaml.safe_load(minimal_mihomo_provider_config("https://sub.example.com/1"))
            cfg2 = yaml.safe_load(minimal_mihomo_provider_config("https://sub.example.com/1"))
        self.assertNotEqual(cfg1["secret"], cfg2["secret"])
        self.assertGreaterEqual(len(cfg1["secret"]), 32)
        self.assertGreaterEqual(len(cfg2["secret"]), 32)

        with patch.dict("os.environ", {"LUMA_EGRESS_SECRET": "operator-secret-123"}, clear=False):
            cfg_env = yaml.safe_load(minimal_mihomo_provider_config("https://sub.example.com/1"))
        self.assertEqual(cfg_env["secret"], "operator-secret-123")


class EgressStaticTextConfigTests(unittest.TestCase):
    def test_text_config_uses_url_test_and_fallback_groups_with_hardened_defaults(self):
        config = yaml.safe_load(minimal_mihomo_config_from_text(_sample_subscription_yaml()))

        self.assertEqual(config["mixed-port"], 7890)
        self.assertTrue(config["allow-lan"])
        self.assertEqual(config["bind-address"], "0.0.0.0")
        self.assertEqual(config["mode"], "rule")
        self.assertEqual(config["external-controller"], "127.0.0.1:9090")
        self.assertGreaterEqual(len(config["secret"]), 32)
        self.assertEqual(config["dns"]["listen"], "127.0.0.1:1053")
        self.assertEqual([p["name"] for p in config["proxies"]], ["hk-1", "sg-2"])

        groups = config["proxy-groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            groups[0],
            {
                "name": "EGRESS-AUTO",
                "type": "url-test",
                "proxies": ["hk-1", "sg-2"],
                "url": DEFAULT_HEALTH_CHECK_URL,
                "interval": DEFAULT_HEALTH_CHECK_INTERVAL,
                "tolerance": DEFAULT_URL_TEST_TOLERANCE,
                "lazy": True,
            },
        )
        self.assertEqual(
            groups[1],
            {
                "name": "EGRESS",
                "type": "fallback",
                "proxies": ["EGRESS-AUTO", "hk-1", "sg-2"],
                "url": DEFAULT_HEALTH_CHECK_URL,
                "interval": DEFAULT_HEALTH_CHECK_INTERVAL,
                "lazy": True,
            },
        )
        self.assertEqual(config["rules"], ["MATCH,EGRESS"])

    def test_file_and_bytes_helpers_reject_empty_or_null_proxies(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub_file = Path(tmp) / "sub.yaml"
            sub_file.write_text(_sample_subscription_yaml(), encoding="utf-8")
            config = yaml.safe_load(minimal_mihomo_config_from_file(sub_file))
            self.assertEqual(config["proxy-groups"][1]["name"], "EGRESS")
            self.assertEqual(config["proxy-groups"][1]["type"], "fallback")

        for raw in (b"proxies:\n", b"proxies: []\n", b"proxies:\n  - name: missing-type\n"):
            with self.assertRaisesRegex(LumaError, "no usable proxies"):
                minimal_mihomo_config_from_bytes(raw)


class EgressDirectDomainRulesTests(unittest.TestCase):
    def test_direct_domains_preserve_proxy_providers_and_strategy_groups(self):
        original = minimal_mihomo_provider_config("https://sub.example.com/clash")
        updated, changed = ensure_mihomo_direct_domains(
            original,
            ["registry.example.net.", "REGISTRY.EXAMPLE.NET"],
        )
        self.assertTrue(changed)

        parsed_original = yaml.safe_load(original)
        parsed_updated = yaml.safe_load(updated)
        self.assertEqual(
            parsed_updated["rules"],
            ["DOMAIN,registry.example.net,DIRECT", "MATCH,EGRESS"],
        )
        self.assertEqual(parsed_updated["proxy-providers"], parsed_original["proxy-providers"])
        self.assertEqual(parsed_updated["proxy-groups"], parsed_original["proxy-groups"])
        self.assertEqual(parsed_updated["secret"], parsed_original["secret"])
        self.assertEqual(parsed_updated["dns"], parsed_original["dns"])

        again, changed_again = ensure_mihomo_direct_domains(updated, ["registry.example.net"])
        self.assertFalse(changed_again)
        self.assertEqual(again, updated)


if __name__ == "__main__":
    unittest.main()
