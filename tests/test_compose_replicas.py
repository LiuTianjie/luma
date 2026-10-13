"""Replica contracts through parsing, Control submission and Nomad status."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from luma.compose import load_compose_deployment
from luma.config import LumaConfig
from luma.control.server import handle_compose_deployment, handle_compose_deployment_preview
from luma.control.state import init_state, load_state
from luma.errors import LumaError
from luma.nomad_api import _allocation_health, _desired_group_counts_from_job, _job_task_summaries
from luma.nomad_render import render_compose_job


class ComposeReplicaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.compose = {"services": {"worker": {
            "image": "example.test/worker:1",
            "command": ["worker", "--workers", "3"],
            "stop_grace_period": "30m",
            "deploy": {"replicas": 6, "resources": {
                "reservations": {"cpus": "1.50", "memory": "2048M"},
                "limits": {"memory": "2048M"},
            }},
        }}}
        self.sidecar = {"name": "worker-stack", "region": "render", "services": {
            "worker": {"exposure": "none", "replicas": 6},
        }}
        self.config = LumaConfig({"defaults": {"engine": "nomad"}}, None)

    def load(self, *, import_mode=False):
        (self.root / "docker-compose.yml").write_text(yaml.safe_dump(self.compose))
        path = self.root / "luma.compose.yml"
        path.write_text(yaml.safe_dump(self.sidecar))
        return load_compose_deployment(path, allow_build_services=import_mode)

    def render(self):
        return render_compose_job(self.config, self.load(), as_json=False)["Job"]

    def test_sources_resolve_to_the_same_count_and_default_to_one(self):
        for compose_count, sidecar_count, scale, expected in (
            (6, 6, None, 6), (6, None, None, 6), (None, 6, None, 6),
            (None, None, 6, 6), (6, 6, 6, 6), (None, None, None, 1),
        ):
            with self.subTest(compose=compose_count, sidecar=sidecar_count, scale=scale):
                deploy = self.compose["services"]["worker"]["deploy"]
                override = self.sidecar["services"]["worker"]
                deploy.pop("replicas", None)
                override.pop("replicas", None)
                self.compose["services"]["worker"].pop("scale", None)
                if compose_count is not None:
                    deploy["replicas"] = compose_count
                if sidecar_count is not None:
                    override["replicas"] = sidecar_count
                if scale is not None:
                    self.compose["services"]["worker"]["scale"] = scale
                self.assertEqual(self.render()["TaskGroups"][0]["Count"], expected)

    def test_region_scheduling_resources_shutdown_and_update_are_per_replica(self):
        job = self.render()
        self.assertEqual(job["Constraints"], [{"LTarget": "${meta.region}", "RTarget": "render", "Operand": "="}])
        self.assertEqual(len(job["TaskGroups"]), 1)
        group = job["TaskGroups"][0]
        self.assertEqual(group["Count"], 6)
        self.assertEqual(len(group["Tasks"]), 1)
        task = group["Tasks"][0]
        self.assertEqual(task["Resources"], {"CPU": 1500, "MemoryMB": 2048, "MemoryMaxMB": 2048})
        self.assertEqual(task["Config"]["args"], ["worker", "--workers", "3"])
        self.assertEqual(task["KillTimeout"], 1_800_000_000_000)
        self.assertEqual(job["Update"]["MaxParallel"], 1)
        self.assertEqual(job["Update"]["HealthCheck"], "task_states")
        self.assertTrue(job["Update"]["AutoRevert"])
        self.assertNotIn("Canary", job["Update"])
        self.assertNotIn("ReservedPorts", group)

    def test_explicit_pin_is_preserved(self):
        self.sidecar["services"]["worker"]["node"] = "lab"
        job = self.render()
        self.assertIn({"LTarget": "${meta.luma_node_name}", "RTarget": "lab", "Operand": "="}, job["Constraints"])
        self.assertEqual(job["TaskGroups"][0]["Count"], 6)

    def test_dynamic_edge_ports_support_replicas_without_extra_canary(self):
        self.sidecar.update(region="cn")
        self.sidecar["services"]["worker"].update(exposure="cn-edge", domain="worker.example.test", port=8080)
        self.compose["services"]["worker"]["healthcheck"] = {"test": ["CMD", "curl", "http://localhost:8080/health"]}
        job = self.render()
        group = job["TaskGroups"][0]
        self.assertEqual(group["Count"], 6)
        self.assertEqual(group["Networks"][0]["DynamicPorts"][0]["To"], 8080)
        self.assertNotIn("ReservedPorts", group["Networks"][0])
        self.assertEqual(job["Update"]["HealthCheck"], "checks")
        self.assertNotIn("Canary", job["Update"])

    def test_multi_service_stack_stays_one_shared_group_and_cannot_scale(self):
        self.compose["services"]["db"] = {"image": "postgres:17"}
        with self.assertRaisesRegex(LumaError, "exactly one service"):
            self.load()
        self.compose["services"]["db"]["deploy"] = {"replicas": 6}
        with self.assertRaisesRegex(LumaError, "exactly one service"):
            self.load()
        for body in self.compose["services"].values():
            body["deploy"]["replicas"] = 1
        self.sidecar["services"]["worker"]["replicas"] = 1
        group = self.render()["TaskGroups"][0]
        self.assertEqual(group["Count"], 1)
        self.assertEqual(group["Networks"], [{"Mode": "bridge"}])
        for task in group["Tasks"]:
            self.assertIn("db:127.0.0.1", task["Config"]["extra_hosts"])

    def test_invalid_counts_and_conflicting_sources_fail_before_rendering(self):
        for key in ("compose", "sidecar", "scale"):
            for bad in (0, -1, True, False, 1.5, 6.0, "1.5", "invalid", None, [], {}):
                with self.subTest(key=key, value=bad):
                    original_compose, original_sidecar = copy.deepcopy(self.compose), copy.deepcopy(self.sidecar)
                    target = self.compose["services"]["worker"]["deploy"] if key == "compose" else self.sidecar["services"]["worker"]
                    if key == "scale":
                        target = self.compose["services"]["worker"]
                    target["scale" if key == "scale" else "replicas"] = bad
                    with self.assertRaisesRegex(LumaError, "positive integer"):
                        self.load()
                    self.compose, self.sidecar = original_compose, original_sidecar
        self.sidecar["services"]["worker"]["replicas"] = 2
        with self.assertRaisesRegex(LumaError, "conflicting replica counts"):
            self.load()
        self.sidecar["services"]["worker"]["replicas"] = 6
        self.compose["services"]["worker"]["scale"] = 2
        with self.assertRaisesRegex(LumaError, "conflicting replica counts"):
            self.load()

    def test_volume_network_and_ignored_scheduling_options_are_rejected(self):
        base = copy.deepcopy(self.compose)
        for changes, message in (
            ({"volumes": ["data:/data"]}, "volume mounts"),
            ({"volumes": ["/srv/data:/data:ro"]}, "volume mounts"),
            ({"volumes": [{"type": "bind", "source": "/srv/data", "target": "/data"}]}, "volume mounts"),
            ({"volumes": ["/data"]}, "volume mounts"),
            ({"network_mode": "host"}, "bridge networking"),
            ({"ports": ["8080:8080"]}, "Compose ports"),
            ({"container_name": "worker"}, "container_name"),
            ({"deploy": {"replicas": 6, "mode": "global"}}, "deploy.mode"),
            ({"deploy": {"replicas": 6, "mode": "replicated-job"}}, "deploy.mode"),
            ({"deploy": {"replicas": 6, "placement": {"constraints": ["node.role == worker"]}}}, "deploy.placement"),
            ({"deploy": {"replicas": 6, "update_config": {"parallelism": 3}}}, "deploy.update_config"),
            ({"deploy": {"replicas": 6, "rollback_config": {"parallelism": 3}}}, "deploy.rollback_config"),
        ):
            with self.subTest(changes=changes):
                self.compose = copy.deepcopy(base)
                self.compose["services"]["worker"].update(changes)
                with self.assertRaisesRegex(LumaError, message):
                    self.load()

    def test_sidecar_storage_and_static_routes_cannot_scale(self):
        self.sidecar["volumes"] = {"data": {"local": {"path": "/srv/data", "node": "lab"}}}
        with self.assertRaisesRegex(LumaError, "sidecar volumes"):
            self.load()
        del self.sidecar["volumes"]
        for exposure, port in (("none", 8080), ("cn-edge", 8080), ("tailscale-relay", None), ("tcp-relay", None)):
            self.sidecar["services"]["worker"].update(exposure=exposure, publishPort=port)
            with self.assertRaisesRegex(LumaError, "dynamic edge port"):
                self.load()

    def test_import_mode_validates_before_build_and_preserves_count_after_image_injection(self):
        worker = self.compose["services"]["worker"]
        del worker["image"]
        worker["build"] = {"context": "."}
        deployment = self.load(import_mode=True)
        deployment.compose["services"]["worker"].pop("build")
        deployment.compose["services"]["worker"]["image"] = "registry.test/worker@sha256:" + "a" * 64
        self.assertEqual(render_compose_job(self.config, deployment, as_json=False)["Job"]["TaskGroups"][0]["Count"], 6)
        self.sidecar["services"]["worker"]["replicas"] = 2
        with self.assertRaisesRegex(LumaError, "conflicting replica counts"):
            self.load(import_mode=True)

    def test_control_preview_submit_and_dashboard_desired_count_agree(self):
        # Local Control state only; replace the external Nomad submit boundary.
        with patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.root / "state"), "LUMA_CONTROL_CONFIG": str(self.root / "config.yml")}):
            state = init_state(domain="luma.example.test", cluster_id="test", overwrite=True)
            (self.root / "config.yml").write_text(yaml.safe_dump({"defaults": {
                "engine": "nomad", "stackRoot": str(self.root / "stacks"), "routesRoot": str(self.root / "routes"),
            }}))
            # Compose-only replicas must also be visible without a sidecar service entry.
            self.sidecar.update(region="cn", services={})
            body = {"manifest": yaml.safe_dump(self.sidecar), "composeContent": yaml.safe_dump(self.compose), "skipDns": True}
            with patch("luma.control.server.deploy_to_nomad", return_value="registered") as submit:
                preview = handle_compose_deployment_preview(state["deployToken"], body)
                submit.assert_not_called()
                self.assertFalse((self.root / "stacks").exists())
                self.assertEqual(preview["summary"]["services"][0]["replicas"], 6)
                expected = json.loads(preview["artifacts"][0]["content"])["Job"]
                handle_compose_deployment(state["deployToken"], body)
                submit.assert_called_once()
                job = json.loads(submit.call_args.args[1])["Job"]
                self.assertEqual(job, expected)
                self.assertEqual(job["TaskGroups"][0]["Count"], 6)
                self.assertFalse(load_state()["deployments"]["compose"]["worker-stack"].get("localStorage"))
                summary = _job_task_summaries("worker-stack", job, {}, [], {"running": 1})
                self.assertEqual(summary[0]["desired"], 6)
                self.assertEqual(summary[0]["running"], 1)

    def test_control_rejects_conflict_before_storage_or_nomad_side_effects(self):
        with patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.root / "state"), "LUMA_CONTROL_CONFIG": str(self.root / "config.yml")}):
            state = init_state(domain="luma.example.test", cluster_id="test", overwrite=True)
            (self.root / "config.yml").write_text("defaults: {engine: nomad}\n")
            self.sidecar["services"]["worker"]["replicas"] = 2
            body = {"manifest": yaml.safe_dump(self.sidecar), "composeContent": yaml.safe_dump(self.compose)}
            with patch("luma.control.server.deploy_to_nomad") as submit, patch("luma.control.server._bind_compose_local_storage") as bind:
                for handler in (handle_compose_deployment_preview, handle_compose_deployment):
                    with self.assertRaisesRegex(LumaError, "conflicting replica counts"):
                        handler(state["deployToken"], body)
                submit.assert_not_called()
                bind.assert_not_called()

    def test_rollout_needs_all_six_current_version_allocations(self):
        job = self.render()
        groups = _desired_group_counts_from_job(job, slug="worker-stack")
        allocations = [{
            "ID": f"worker-{index}", "TaskGroup": "worker-stack", "JobVersion": 2,
            "DesiredStatus": "run", "ClientStatus": "running",
            "TaskStates": {"worker": {"State": "running", "Failed": False}},
            "DeploymentStatus": {"Healthy": True},
        } for index in range(6)]
        self.assertFalse(_allocation_health(allocations[:1], target_version=2, expected_groups=groups)[0])
        self.assertTrue(_allocation_health(allocations, target_version=2, expected_groups=groups)[0])
        allocations[-1]["JobVersion"] = 1
        self.assertFalse(_allocation_health(allocations, target_version=2, expected_groups=groups)[0])
