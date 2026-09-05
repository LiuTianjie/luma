import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from luma.compose import load_compose_deployment
from luma.config import LumaConfig
from luma.control.server import (
    _bind_compose_local_storage,
    _bind_service_local_storage,
    _register_service_deployment,
)
from luma.errors import LumaError
from luma.local_storage import choose_storage_node, guard_storage_sources, persistent_mounts, storage_owner_from_job
from luma.nomad_render import render_compose_job, render_nomad_job
from luma.service import ServiceSpec


class LocalStoragePlacementTests(unittest.TestCase):
    def test_replacing_existing_volume_with_empty_named_volume_is_blocked(self):
        with self.assertRaisesRegex(LumaError, "source changed"):
            guard_storage_sources(persistent_mounts(["existing:/data"]), persistent_mounts(["fresh:/data"]))

    def test_preparing_existing_database_directory_preserves_permissions(self):
        import subprocess
        from luma.agent import _volume_path_command
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "postgres"
            directory.mkdir(mode=0o700)
            subprocess.run(["bash", "-c", _volume_path_command(str(directory), preserve_existing=True)], check=True)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def setUp(self):
        self.config = LumaConfig({"defaults": {"engine": "nomad"}}, None)
        self.state = {"nodes": {
            "node-a": {"region": "cn", "nodeId": "id-a", "nomadStatus": "ready", "agent": {"status": "ready"}},
            "node-b": {"region": "cn", "nodeId": "id-b", "nomadStatus": "ready", "agent": {"status": "ready"}},
        }}

    def test_previous_owner_never_falls_back_to_a_different_ready_node(self):
        self.assertEqual(choose_storage_node(slug="db", requested=[], previous="offline-node", candidates=["node-b"]), "offline-node")
        with self.assertRaisesRegex(LumaError, "cannot move"):
            choose_storage_node(slug="db", requested=["node-b"], previous="node-a")

    def test_read_only_configuration_and_sockets_do_not_pin_stateless_services(self):
        self.assertEqual(persistent_mounts(["/config:/config:ro", "/var/run/docker.sock:/var/run/docker.sock", {"type": "tmpfs", "target": "/tmp"}]), [])

    def test_down_allocation_still_owns_its_data(self):
        allocation = {"NodeID": "id-a", "ClientStatus": "failed", "DesiredStatus": "stop", "CreateIndex": 12}
        self.assertEqual(storage_owner_from_job({}, [allocation], lambda _: "node-a"), "node-a")

    def test_ambiguous_legacy_allocations_require_data_inspection(self):
        allocations = [{"NodeID": name, "ClientStatus": "running"} for name in ("node-a", "node-b")]
        with self.assertRaisesRegex(LumaError, "ambiguous"):
            storage_owner_from_job({}, allocations, lambda node: node)

    def test_new_service_persists_deployment_node_and_keeps_volume_identity(self):
        service = ServiceSpec(source=Path("db.yaml"), name="db", image="postgres:17", region="cn", volumes=["existing-data:/var/lib/postgresql/data"])
        manifest = "name: db\nimage: postgres:17\nregion: cn\nvolumes: [existing-data:/var/lib/postgresql/data]\n"
        bound, normalized = _bind_service_local_storage(self.config, self.state, service, manifest, inspect_runtime=False)
        self.assertEqual(bound.node, "node-a")
        self.assertEqual(yaml.safe_load(normalized)["node"], "node-a")
        _register_service_deployment(self.state, bound, normalized, "db.yaml")
        self.state["nodes"]["node-a"]["nomadStatus"] = "down"
        again, _ = _bind_service_local_storage(self.config, self.state, service, manifest, inspect_runtime=False)
        self.assertEqual(again.node, "node-a")
        job = json.loads(render_nomad_job(self.config, again))["Job"]
        self.assertIn({"LTarget": "${meta.luma_node_name}", "RTarget": "node-a", "Operand": "="}, job["Constraints"])
        self.assertEqual(job["TaskGroups"][0]["Tasks"][0]["Config"]["mount"][0]["source"], "existing-data")
        self.assertEqual(self.state["deployments"]["services"]["db"]["localStorage"]["node"], "node-a")

    def test_runtime_node_takes_precedence_over_fresh_auto_placement(self):
        service = ServiceSpec(source=Path("db.yaml"), name="db", image="postgres:17", region="cn", volumes=["data:/data"])
        job = {"TaskGroups": [{"Tasks": [{"Config": {"mount": [{"type": "volume", "source": "data", "target": "/data"}]}}]}]}
        allocations = [{"NodeID": "id-b", "ClientStatus": "running"}]
        with patch("luma.control.server.NomadApi.request", side_effect=[job, allocations]):
            bound, _ = _bind_service_local_storage(self.config, self.state, service, "name: db\n", inspect_runtime=True)
        self.assertEqual(bound.node, "node-b")

    def test_persistent_single_service_never_uses_canary_or_shared_replicas(self):
        service = ServiceSpec(source=Path("db.yaml"), name="db", image="postgres:17", region="cn", node="node-a", exposure="cn-edge", domain="db.example.com", port=8080, volumes=["data:/data"])
        job = json.loads(render_nomad_job(self.config, service))["Job"]
        self.assertNotIn("Canary", job["Update"])
        from dataclasses import replace
        with self.assertRaisesRegex(LumaError, "replicas: 1"):
            _bind_service_local_storage(self.config, self.state, replace(service, replicas=2), "name: db\n", inspect_runtime=False)

    def test_compose_path_inherits_service_node_without_storage_node_picker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compose = "services:\n  db:\n    image: postgres:17\n    volumes: [data:/var/lib/postgresql/data]\nvolumes:\n  data: {}\n"
            manifest = "name: app\nregion: cn\ncompose: docker-compose.yml\nservices:\n  db:\n    node: node-b\nvolumes:\n  data:\n    local:\n      path: /srv/app/data\n"
            (root / "docker-compose.yml").write_text(compose)
            (root / "luma.compose.yml").write_text(manifest)
            deployment = load_compose_deployment(root / "luma.compose.yml")
            bound, body = _bind_compose_local_storage(self.config, self.state, deployment, {"manifest": manifest}, inspect_runtime=False)
            self.assertEqual(bound.volumes["data"].local_node, "node-b")
            from luma.storage import storage_migration_plan
            plan = storage_migration_plan(bound, volume="data", from_node="old-node", from_volume="old-data")
            self.assertEqual(plan["toNode"], "node-b")
            self.assertEqual(plan["toPath"], "/srv/app/data")
            self.assertEqual(plan["status"], "manual-required")
            self.assertEqual(yaml.safe_load(body["manifest"])["services"]["db"]["node"], "node-b")
            job = render_compose_job(self.config, bound, as_json=False)["Job"]
            mount = job["TaskGroups"][0]["Tasks"][0]["Config"]["mount"][0]
            self.assertEqual(mount, {"type": "bind", "source": "/srv/app/data", "target": "/var/lib/postgresql/data", "readonly": False})
            self.assertNotIn("Canary", job["Update"])
