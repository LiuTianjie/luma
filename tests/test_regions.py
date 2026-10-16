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
    handle_region_update,
)
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError
from luma.regions import parse_region_name, region_uses_egress_proxy, require_region_exposure, validate_region_exposure
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
    def test_custom_relay_manifest_parses_before_live_capability_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.yml"
            path.write_text("name: api\nimage: nginx:alpine\nregion: render\nnode: air1\nexposure: tailscale-relay\ndomain: api.example.com\nport: 8080\n")
            self.assertEqual(load_service(path).region, "render")
        with self.assertRaisesRegex(LumaError, "does not allow"):
            require_region_exposure({"regions": {"render": {"egress": "direct"}}}, "render", "tailscale-relay")
        enabled = {"regions": {"render": {"egress": "direct", "exposures": ["none", "tailscale-relay"]}}}
        self.assertEqual(require_region_exposure(enabled, "render", "tailscale-relay")["egress"], "direct")

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
    def test_region_update_cli_and_client_only_send_requested_fields(self) -> None:
        from luma.cli import build_parser
        from luma.cli.nodes import cmd_region
        from luma.control.client import ControlClient
        args = build_parser().parse_args(["region", "update", "render", "--exposure", "tailscale-relay", "--format", "json"])
        with patch("luma.cli.nodes._control_context", return_value=("https://luma.example.com", "test-token", False, None)), patch.object(
            ControlClient, "request", return_value={"name": "render", "exposures": ["none", "tailscale-relay"]},
        ) as request, patch("builtins.print"):
            self.assertEqual(cmd_region(args), 0)
        request.assert_called_once_with("POST", "/v1/regions/update", {"name": "render", "exposures": ["tailscale-relay"]})

    def test_update_preserves_region_identity_and_egress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": tmp}):
            state = init_state(domain="luma.example.com", cluster_id="test", overwrite=True)
            token = state["deployToken"]
            original = handle_region_create(token, {"name": "render", "egress": "direct"})
            result = handle_region_update(token, {"name": "render", "exposures": ["tailscale-relay"]})
            self.assertEqual(result["exposures"], ["none", "tailscale-relay"])
            self.assertEqual(result["createdAt"], original["createdAt"])
            self.assertEqual(result["egress"], "direct")
            require_region_exposure(load_state(), "render", "tailscale-relay")
            result = handle_region_update(token, {"name": "render", "egress": "proxy"})
            self.assertEqual(result["exposures"], ["none", "tailscale-relay"])
            with self.assertRaises(LumaError):
                handle_region_update(state["joinToken"], {"name": "render", "exposures": ["none"]})
            self.assertEqual(load_state()["regions"]["render"]["exposures"], ["none", "tailscale-relay"])

    def test_capability_removal_rejects_native_and_compose_users(self) -> None:
        for kind in ("services", "compose"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": tmp}):
                state = init_state(domain="luma.example.com", cluster_id="test", overwrite=True)
                token = state["deployToken"]
                handle_region_create(token, {"name": "render", "exposures": ["tailscale-relay"]})
                state = load_state()
                manifest = {"region": "render", "exposure": "tailscale-relay"}
                if kind == "compose":
                    manifest = {"region": "render", "services": {"api": {"exposure": "tailscale-relay"}}}
                state["deployments"] = {kind: {"teachvideo": {"manifest": yaml.safe_dump(manifest), "status": "failed_partial"}}}
                save_state(state)
                with self.assertRaisesRegex(LumaError, "still used.*teachvideo"):
                    handle_region_update(token, {"name": "render", "exposures": ["none"]})
                self.assertIn("tailscale-relay", load_state()["regions"]["render"]["exposures"])
                state["deployments"] = {}
                save_state(state)
                self.assertEqual(handle_region_update(token, {"name": "render", "exposures": ["none"]})["exposures"], ["none"])

    def test_region_exposure_validation_and_update_http_route(self) -> None:
        from starlette.testclient import TestClient
        from luma.control.server import create_app
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": tmp}):
            state = init_state(domain="luma.example.com", cluster_id="test", overwrite=True)
            token = state["deployToken"]
            handle_region_create(token, {"name": "render"})
            for invalid in ([], "tailscale-relay", ["cn-edge"], ["typo"], [True]):
                with self.subTest(exposures=invalid), self.assertRaises(LumaError):
                    handle_region_update(token, {"name": "render", "exposures": invalid})
            with self.assertRaisesRegex(LumaError, "read-only"):
                handle_region_update(token, {"name": "home", "exposures": ["none"]})
            with self.assertRaisesRegex(LumaError, "requires egress or exposures"):
                handle_region_update(token, {"name": "render"})
            with TestClient(create_app()) as client:
                response = client.post("/v1/regions/update", headers={"Authorization": f"Bearer {token}"}, json={"name": "render", "exposures": ["tailscale-relay"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("tailscale-relay", load_state()["regions"]["render"]["exposures"])

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
