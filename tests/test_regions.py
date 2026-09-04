from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from luma.compose import load_compose_deployment
from luma.control.server import (
    handle_deployment_preview,
    handle_node_register,
    handle_region_create,
    handle_region_list,
    handle_region_remove,
)
from luma.control.state import init_state, save_state
from luma.errors import LumaError
from luma.regions import parse_region_name, region_uses_egress_proxy, validate_region_exposure
from luma.service import load_service


def _set_env(name: str, value: str | None) -> str | None:
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    return old


def _restore_env(name: str, old: str | None) -> None:
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old


class RegionNameTests(unittest.TestCase):
    def test_parse_accepts_custom_slug(self) -> None:
        self.assertEqual(parse_region_name("Batch-A"), "batch-a")

    def test_parse_rejects_reserved_and_invalid(self) -> None:
        with self.assertRaisesRegex(LumaError, "reserved"):
            parse_region_name("all")
        with self.assertRaisesRegex(LumaError, "lowercase"):
            parse_region_name("1queue")
        with self.assertRaisesRegex(LumaError, "required"):
            parse_region_name("")

    def test_custom_region_yaml_allows_none_only(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as fh:
            fh.write("name: worker\nimage: nginx:alpine\nregion: batch-a\nexposure: none\n")
            path = Path(fh.name)
        try:
            service = load_service(path)
            self.assertEqual(service.region, "batch-a")
            self.assertEqual(service.exposure, "none")
        finally:
            path.unlink(missing_ok=True)

    def test_custom_region_yaml_rejects_cn_edge(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as fh:
            fh.write(
                "name: api\nimage: nginx:alpine\nregion: batch-a\nexposure: cn-edge\n"
                "domain: api.example.com\nport: 80\n"
            )
            path = Path(fh.name)
        try:
            with self.assertRaisesRegex(LumaError, "exposure=cn-edge requires region=cn"):
                load_service(path)
        finally:
            path.unlink(missing_ok=True)

    def test_builtin_exposure_rules_still_hold(self) -> None:
        with self.assertRaisesRegex(LumaError, "exposure=cn-edge requires region=cn"):
            validate_region_exposure("global", "cn-edge")

    def test_compose_custom_region_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text("services:\n  worker:\n    image: nginx:alpine\n", encoding="utf-8")
            sidecar = root / "luma.compose.yml"
            sidecar.write_text("name: queue\nregion: batch-a\ncompose: docker-compose.yml\n", encoding="utf-8")
            deployment = load_compose_deployment(sidecar)
            self.assertEqual(deployment.region, "batch-a")


class RegionControlApiTests(unittest.TestCase):
    def test_create_list_join_and_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.64.0.1:4647"
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")

                created = handle_region_create(state["deployToken"], {"name": "batch-a", "egress": "proxy"})
                self.assertEqual(created["name"], "batch-a")
                self.assertFalse(created["builtin"])
                self.assertEqual(created["egress"], "proxy")
                self.assertEqual(created["exposures"], ["none"])

                listed = handle_region_list(state["deployToken"])
                names = [item["name"] for item in listed["regions"]]
                self.assertEqual(names[:3], ["cn", "global", "home"])
                self.assertIn("batch-a", names)

                with self.assertRaisesRegex(LumaError, "already exists"):
                    handle_region_create(state["deployToken"], {"name": "cn"})

                registered = handle_node_register(state["joinToken"], {"nodeName": "batch-a-01", "region": "batch-a"})
                self.assertEqual(registered["region"], "batch-a")
                self.assertEqual(registered["egress"], "proxy")
                self.assertTrue(str(registered["egressProxy"]).endswith(":7890"))

                with self.assertRaisesRegex(LumaError, "unknown region"):
                    handle_node_register(state["joinToken"], {"nodeName": "ghost", "region": "mars"})

                with self.assertRaisesRegex(LumaError, "nodes or storage classes"):
                    handle_region_remove(state["deployToken"], {"name": "batch-a"})

                manifest = yaml.safe_dump(
                    {
                        "name": "queue-worker",
                        "image": "nginx:alpine",
                        "region": "batch-a",
                        "exposure": "none",
                        "replicas": 8,
                    }
                )
                preview = handle_deployment_preview(
                    state["deployToken"],
                    {"manifest": manifest, "sourceName": "queue-worker.yaml"},
                )
                self.assertEqual(preview["summary"]["region"], "batch-a")
                self.assertEqual(preview["summary"]["replicas"], 8)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_remove_unused_custom_region(self) -> None:
        from luma.control.state import load_state

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_region_create(state["deployToken"], {"name": "lab-gpu", "egress": "direct"})
                saved = load_state()
                self.assertFalse(region_uses_egress_proxy(saved, "lab-gpu"))
                removed = handle_region_remove(state["deployToken"], {"name": "lab-gpu"})
                self.assertTrue(removed["removed"])
                with self.assertRaisesRegex(LumaError, "cannot remove built-in"):
                    handle_region_remove(state["deployToken"], {"name": "home"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_preview_rejects_unregistered_custom_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump(
                    {"name": "queue-worker", "image": "nginx:alpine", "region": "batch-a", "exposure": "none"}
                )
                with self.assertRaisesRegex(LumaError, "unknown region"):
                    handle_deployment_preview(state["deployToken"], {"manifest": manifest, "sourceName": "queue-worker.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_agent_join_accepts_custom_region_name(self) -> None:
        from luma.agent import join_nomad_node

        with patch("luma.bootstrap.install_nomad_node", return_value=["ok"]), patch(
            "luma.bootstrap.local_nomad_node_info", return_value=("batch-a-01", "node-id")
        ), patch("luma.bootstrap._tailscale_ip", return_value="100.64.0.8"):
            result = join_nomad_node(node_name="batch-a-01", region="batch-a", server_addr="100.64.0.1:4647")
        self.assertEqual(result["region"], "batch-a")


if __name__ == "__main__":
    unittest.main()
