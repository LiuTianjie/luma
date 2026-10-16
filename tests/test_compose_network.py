from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from luma.compose import load_compose_deployment
from luma.compose_network import NETWORK_CONTAINER, NETWORK_TASKS, uses_macos_compose_network
from luma.config import LumaConfig
from luma.errors import LumaError
from luma.nomad_render import render_compose_job


class MacComposeNetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.nodes = {"air1": {"name": "air1", "region": "render", "tailscaleIP": "100.64.0.2", "agent": {"os": "darwin", "arch": "arm64"}}}
        self.config = LumaConfig({}, None)

    def deployment(self, *, public=True, pin=True):
        (self.root / "compose.yml").write_text(yaml.safe_dump({"services": {
            "api": {"image": "api:1", "environment": {"DATABASE_HOST": "database"}},
            "database": {"image": "postgres:16", "volumes": ["data:/var/lib/postgresql/data"]},
        }, "volumes": {"data": {}}}))
        sidecar = {"name": "video", "region": "render", "compose": "compose.yml", "services": {
            "api": {"exposure": "tailscale-relay", "domain": "video.example.com", "port": 8080, "publishPort": 18080} if public else {"exposure": "none"},
            "database": {"exposure": "none", **({"node": "air1"} if pin else {})},
        }}
        (self.root / "luma.compose.yml").write_text(yaml.safe_dump(sidecar))
        return load_compose_deployment(self.root / "luma.compose.yml")

    def test_published_port_uses_private_docker_namespace_and_lifecycle(self):
        deployment = self.deployment()
        group = render_compose_job(self.config, deployment, node_records=self.nodes, as_json=False)["Job"]["TaskGroups"][0]
        self.assertEqual(group["Networks"], [{"Mode": "host", "ReservedPorts": [{"Label": "api", "Value": 18080, "To": 8080, "HostNetwork": "default"}]}])
        tasks = {task["Name"]: task for task in group["Tasks"]}
        for name in ("api", "database"):
            config = tasks[name]["Config"]
            self.assertEqual(config["network_mode"], f"container:{NETWORK_CONTAINER}")
            self.assertNotIn("ports", config)
            self.assertNotIn("extra_hosts", config)
            self.assertNotIn("/var/run/docker.sock", json.dumps(config))
        self.assertEqual(tasks["api"]["Env"]["DATABASE_HOST"], "database")
        init = tasks[NETWORK_TASKS[0]]
        self.assertEqual(init["Lifecycle"], {"Hook": "prestart", "Sidecar": False})
        args = init["Config"]["args"]
        self.assertEqual(json.loads(args[args.index("--ports") + 1]), [{"port": 8080, "published": 18080}])
        self.assertEqual(tasks[NETWORK_TASKS[2]]["Lifecycle"], {"Hook": "poststop", "Sidecar": False})
        self.assertEqual(tasks[NETWORK_TASKS[1]]["RestartPolicy"]["Mode"], "fail")

    def test_internal_siblings_have_no_published_port(self):
        group = render_compose_job(self.config, self.deployment(public=False), node_records=self.nodes, as_json=False)["Job"]["TaskGroups"][0]
        self.assertEqual(group["Networks"], [{"Mode": "host"}])
        init = next(t for t in group["Tasks"] if t["Name"] == NETWORK_TASKS[0])
        args = init["Config"]["args"]
        self.assertEqual(json.loads(args[args.index("--ports") + 1]), [])

    def test_single_internal_worker_can_still_scale_across_mac_region(self):
        deployment = self.deployment(public=False, pin=False)
        deployment.compose["services"].pop("database")
        deployment.compose.pop("volumes")
        deployment = replace(deployment, services={"api": replace(deployment.services["api"], replicas=3)}, volumes={})
        group = render_compose_job(self.config, deployment, node_records=self.nodes, as_json=False)["Job"]["TaskGroups"][0]
        self.assertEqual(group["Count"], 3)
        self.assertEqual(len(group["Tasks"]), 1)
        self.assertNotIn("Networks", group)

    def test_linux_keeps_existing_cni_and_service_loopback(self):
        nodes = {"air1": {**self.nodes["air1"], "agent": {"os": "linux", "arch": "arm64"}}}
        group = render_compose_job(self.config, self.deployment(), node_records=nodes, as_json=False)["Job"]["TaskGroups"][0]
        self.assertEqual(group["Networks"][0]["Mode"], "bridge")
        self.assertEqual(len(group["Tasks"]), 2)
        self.assertIn("database:127.0.0.1", group["Tasks"][0]["Config"]["extra_hosts"])

    def test_unpinned_mac_region_fails_before_submitting_cni_job(self):
        with self.assertRaisesRegex(LumaError, "explicit node pin"):
            render_compose_job(self.config, self.deployment(pin=False), node_records=self.nodes)

    def test_cached_helper_image_and_registry_auth_used_by_all_hooks(self):
        deployment = replace(self.deployment(), network_image="registry.local/helper@sha256:" + "a" * 64)
        auth = {"username": "runtime", "password": "test", "serveraddress": "registry.local"}
        group = render_compose_job(self.config, deployment, node_records=self.nodes, registry_auth_resolver=lambda image: auth if image == deployment.network_image else None, as_json=False)["Job"]["TaskGroups"][0]
        for task in group["Tasks"]:
            if task["Name"] in NETWORK_TASKS:
                self.assertEqual(task["Config"]["image"], deployment.network_image)
                self.assertEqual(task["Config"]["auth"]["server_address"], "registry.local")
            else:
                self.assertNotIn("auth", task["Config"])

    def test_helper_cached_without_overwriting_application_services(self):
        from luma.control.server import _cache_compose_images_on_builder
        deployment = self.deployment()
        state = {"nodes": self.nodes, "build": {"registryHost": "registry.local:5000", "pushHost": "registry.local:5000"}}
        def cached(config, state, image, **kwargs):
            return {"deployed": "registry.local:5000/" + image, "cached": True}
        with patch("luma.control.server._cache_runtime_image_on_builder", side_effect=cached):
            resolved, result = _cache_compose_images_on_builder(self.config, state, deployment)
        self.assertEqual(set(resolved.compose["services"]), {"api", "database"})
        self.assertTrue(resolved.network_image.startswith("registry.local:5000/python:"))
        self.assertIn("networkImage", result)
        self.assertEqual(deployment.compose["services"]["api"]["image"], "api:1")

    def test_explicit_internal_host_network_is_not_rewritten(self):
        deployment = self.deployment(public=False)
        for body in deployment.compose["services"].values():
            body["network_mode"] = "host"
        self.assertFalse(uses_macos_compose_network(deployment, self.nodes))

    def test_control_preview_with_local_storage_and_sibling_node_pin(self):
        from luma.control.server import handle_compose_deployment_preview
        from luma.control.state import init_state, save_state
        deployment = self.deployment()
        config_path = self.root / "luma.yaml"
        config_path.write_text(yaml.safe_dump({"defaults": {"stackRoot": str(self.root / "stacks")}}))
        with patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.root / "state"), "LUMA_CONTROL_CONFIG": str(config_path)}):
            state = init_state(domain="luma.example.com", cluster_id="test", overwrite=True)
            state["nodes"] = self.nodes
            state["regions"] = {"render": {"egress": "direct", "exposures": ["none", "tailscale-relay"]}}
            save_state(state)
            result = handle_compose_deployment_preview(state["deployToken"], {
                "manifest": deployment.source.read_text(), "composeContent": deployment.compose_path.read_text(),
            })
        job = json.loads(next(item["content"] for item in result["artifacts"] if item["kind"] == "job"))
        self.assertEqual(job["Job"]["TaskGroups"][0]["Networks"][0]["Mode"], "host")
        route = next(item["content"] for item in result["artifacts"] if item["kind"] == "route")
        self.assertIn("http://100.64.0.2:18080", route)

    def test_relay_inherits_sibling_pin_without_local_storage(self):
        from luma.control.server import _compose_service_as_service_spec, resolve_nomad_static_route_target
        deployment = self.deployment()
        deployment.compose["services"]["database"].pop("volumes")
        service = _compose_service_as_service_spec(deployment, deployment.services["api"])
        resolved = resolve_nomad_static_route_target(service, {"nodes": self.nodes})
        self.assertEqual(resolved.node, "air1")
        self.assertEqual(resolved.relay, {"host": "100.64.0.2", "port": 18080})

    def test_mac_ports_never_dispatch_host_cni_repair(self):
        from luma.control.server import _refresh_nomad_cni_hostports_for_nodes
        with patch("luma.control.server._queue_node_agent_task") as queue:
            result = _refresh_nomad_cni_hostports_for_nodes({"nodes": self.nodes}, {"air1"}, ports=[18080])
        queue.assert_not_called()
        self.assertIn("Docker manages", result["skipped"][0]["reason"])

    def test_runtime_cache_normalizes_mac_platform_to_linux_container(self):
        from luma.control.server import _cache_runtime_image_on_builder
        state = {"build": {"registryHost": "registry.local:5000", "pushHost": "registry.local:5000"}}
        with patch("luma.control.server._require_build_node", return_value="builder"), patch(
            "luma.control.server._run_node_agent_task", return_value={"digest": "sha256:" + "a" * 64},
        ) as task:
            _cache_runtime_image_on_builder(self.config, state, "python:3.13-alpine", platform="darwin/arm64")
        self.assertEqual(task.call_args.args[3]["platform"], "linux/arm64")

    def test_dashboard_hides_network_hooks_and_keeps_published_port(self):
        from luma.nomad_api import _job_task_summaries
        job = render_compose_job(self.config, self.deployment(), node_records=self.nodes, as_json=False)["Job"]
        tasks = _job_task_summaries("video", job, {}, [], {})
        self.assertEqual({task["name"] for task in tasks}, {"api", "database"})
        api = next(task for task in tasks if task["name"] == "api")
        self.assertEqual(api["publishPort"], "18080")
        self.assertEqual(api["targetPort"], "8080")
