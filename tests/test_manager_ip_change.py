from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from luma.config import LumaConfig
from luma.cli import build_parser
from luma.errors import LumaError
from luma.manager import _updated_config, manager_ip_change


OLD_IP = "8.147.65.253"
NEW_IP = "8.145.62.128"


def _config(path: Path) -> dict:
    return {
        "providers": {
            "dns": {
                "type": "cloudflare",
                "zone": "itool.tech",
                "zoneId": "zone-id",
                "apiTokenEnv": "CLOUDFLARE_API_TOKEN",
                "edgeTarget": OLD_IP,
            }
        },
        "nodes": {
            "manager": {
                "host": "manager",
                "publicIp": OLD_IP,
                "region": "cn",
                "roles": ["nomad-manager", "edge"],
            }
        },
        "defaults": {"engine": "nomad"},
    }


class FakeCloudflareClient:
    instances = []

    def __init__(self, token: str):
        self.token = token
        self.calls = []
        type(self).instances.append(self)

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return {
                "success": True,
                "result": [
                    {
                        "id": "record-1",
                        "name": "luma.itool.tech",
                        "type": "A",
                        "content": OLD_IP,
                        "ttl": 300,
                        "proxied": False,
                    },
                    {
                        "id": "record-2",
                        "name": "app.itool.tech",
                        "type": "A",
                        "content": OLD_IP,
                        "ttl": 1,
                        "proxied": True,
                    },
                ],
                "result_info": {"total_pages": 1},
            }
        return {"success": True, "result": {}}


class ManagerIpChangeTests(unittest.TestCase):
    def setUp(self):
        FakeCloudflareClient.instances.clear()

    def test_parser_exposes_requested_command_shape(self):
        args = build_parser().parse_args(
            [
                "manager",
                "ip-change",
                "--old",
                OLD_IP,
                "--new",
                NEW_IP,
                "--domain",
                "luma.itool.tech",
                "--dry-run",
            ]
        )
        self.assertEqual(args.command, "manager")
        self.assertEqual(args.manager_command, "ip-change")
        self.assertEqual(args.old_ip, OLD_IP)
        self.assertEqual(args.new_ip, NEW_IP)
        self.assertTrue(args.dry_run)

    def test_dry_run_is_read_only_and_does_not_expose_token(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "luma.yaml"
            original = _config(path)
            path.write_text(yaml.safe_dump(original), encoding="utf-8")
            state = {
                "domain": "luma.itool.tech",
                "secrets": {"CLOUDFLARE_API_TOKEN": "super-secret-token"},
                "history": f"manager used to be {OLD_IP}",
            }
            messages = []
            with patch("luma.manager.CloudflareClient", FakeCloudflareClient), patch(
                "luma.manager._running_control_image",
                return_value="registry.internal/luma-control:v0.1.279",
            ), patch("luma.manager._probe_manager") as probe, patch(
                "luma.manager._install_manager_config"
            ) as install, patch("luma.manager.refresh_manager_control_local") as refresh:
                result = manager_ip_change(
                    old_ip=OLD_IP,
                    new_ip=NEW_IP,
                    domain="luma.itool.tech",
                    state=state,
                    config_path=path,
                    dry_run=True,
                    emit=messages.append,
                )

            self.assertTrue(result["dryRun"])
            self.assertEqual(len(result["dnsRecords"]), 2)
            self.assertEqual(yaml.safe_load(path.read_text(encoding="utf-8")), original)
            self.assertEqual(state["history"], f"manager used to be {OLD_IP}")
            self.assertNotIn("super-secret-token", "\n".join(messages))
            probe.assert_called_once_with("luma.itool.tech", NEW_IP)
            install.assert_not_called()
            refresh.assert_not_called()
            self.assertFalse(
                any(call[0] == "PATCH" for call in FakeCloudflareClient.instances[0].calls)
            )

    def test_apply_updates_typed_fields_patches_exact_records_and_reuses_image(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "luma.yaml"
            raw = _config(path)
            raw["notes"] = {"historicalIp": OLD_IP}
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            state = {
                "domain": "luma.itool.tech",
                "secrets": {"CLOUDFLARE_API_TOKEN": "super-secret-token"},
                "history": f"do not rewrite {OLD_IP}",
            }
            installed = {}

            def fake_install(config_path, updated):
                installed.update(copy.deepcopy(updated))
                return str(config_path) + ".backup"

            with patch("luma.manager.CloudflareClient", FakeCloudflareClient), patch(
                "luma.manager._running_control_image",
                return_value="registry.internal/luma-control:v0.1.279",
            ), patch("luma.manager._probe_manager"), patch(
                "luma.manager._probe_normal_domain", return_value=True
            ), patch("luma.manager._flush_dns_cache"), patch(
                "luma.manager._install_manager_config", side_effect=fake_install
            ), patch("luma.manager.refresh_manager_control_local") as refresh:
                result = manager_ip_change(
                    old_ip=OLD_IP,
                    new_ip=NEW_IP,
                    domain="luma.itool.tech",
                    state=state,
                    config_path=path,
                    emit=lambda _: None,
                )

            self.assertEqual(installed["nodes"]["manager"]["publicIp"], NEW_IP)
            self.assertEqual(installed["providers"]["dns"]["edgeTarget"], NEW_IP)
            self.assertEqual(installed["notes"]["historicalIp"], OLD_IP)
            self.assertEqual(state["history"], f"do not rewrite {OLD_IP}")
            patches = [
                call
                for call in FakeCloudflareClient.instances[0].calls
                if call[0] == "PATCH"
            ]
            self.assertEqual(len(patches), 2)
            self.assertTrue(all(call[2] == {"content": NEW_IP} for call in patches))
            self.assertEqual(result["updatedDnsRecords"], 2)
            self.assertEqual(result["unmanagedConfigPaths"], ["notes.historicalIp"])
            refreshed_config = refresh.call_args.args[0]
            refreshed_node = refresh.call_args.args[1]
            self.assertEqual(refreshed_config.raw["nodes"]["manager"]["publicIp"], NEW_IP)
            self.assertEqual(refreshed_node.public_ip, NEW_IP)

    def test_refuses_ambiguous_edge_target(self):
        raw = _config(Path("luma.yaml"))
        raw["providers"]["dns"]["edgeTarget"] = "203.0.113.50"
        with self.assertRaisesRegex(LumaError, "edgeTarget"):
            _updated_config(LumaConfig(raw, Path("luma.yaml")), old=OLD_IP, new=NEW_IP)

    def test_updates_legacy_top_level_dns_target(self):
        raw = _config(Path("luma.yaml"))
        raw["dns"] = raw.pop("providers")["dns"]
        updated, _, changes, unmanaged = _updated_config(
            LumaConfig(raw, Path("luma.yaml")), old=OLD_IP, new=NEW_IP
        )
        self.assertEqual(updated["dns"]["edgeTarget"], NEW_IP)
        self.assertIn("dns.edgeTarget", changes)
        self.assertEqual(unmanaged, [])

    def test_refuses_domain_mismatch_before_cloudflare_access(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "luma.yaml"
            path.write_text(yaml.safe_dump(_config(path)), encoding="utf-8")
            with self.assertRaisesRegex(LumaError, "does not match"):
                manager_ip_change(
                    old_ip=OLD_IP,
                    new_ip=NEW_IP,
                    domain="other.itool.tech",
                    state={"domain": "luma.itool.tech"},
                    config_path=path,
                    dry_run=True,
                    emit=lambda _: None,
                )


if __name__ == "__main__":
    unittest.main()
