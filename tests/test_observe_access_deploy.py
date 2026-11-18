"""Observe deployment regressions: saved credentials survive source upgrades."""
import json
import os
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from luma.control.observe_access import ACCESS_CREDENTIALS, guard_access_credentials
from luma.control import server
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError
from luma.compose import ComposeDeploymentSpec
from luma import nomad_security


def job(environment):
    return json.dumps({"Job": {"TaskGroups": [{"Tasks": [
        {"Name": "observe-access", "Env": environment},
    ]}]}})


class ObserveAccessDeployTests(unittest.TestCase):
    def test_saved_roles_cannot_be_cleared_or_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "job.json"
            for name in ACCESS_CREDENTIALS:
                target.write_text(job({name: "private-credential"}))
                for after in ({name: ""}, {}):
                    for state in ({"secrets": {name: "private-credential"}},
                                  {"scopedSecrets": {"luma-observe": {name: "private-credential"}}}):
                        with self.subTest(name=name, after=after, scoped="scopedSecrets" in state):
                            with self.assertRaises(LumaError) as error:
                                guard_access_credentials(target, job(after), state)
                            self.assertIn("${" + name + "}", str(error.exception))
                            self.assertNotIn("private-credential", str(error.exception))

    def test_first_install_rotation_and_explicit_disable(self):
        name = "LUMA_OBSERVE_READ_TOKEN"
        state = {"scopedSecrets": {"luma-observe": {name: "new-credential"}}}
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "job.json"
            guard_access_credentials(target, job({name: ""}), {})
            target.write_text(job({name: ""}))
            guard_access_credentials(target, job({name: ""}), state)
            target.write_text(job({name: "old-credential"}))
            guard_access_credentials(target, job({name: "new-credential"}), state)
            guard_access_credentials(target, job({name: ""}), {"scopedSecrets": {"luma-observe": {}}})

    def test_unreadable_job_does_not_disclose_credential_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "job.json"
            target.write_text('broken-private-credential')
            with self.assertRaisesRegex(LumaError, "cannot verify Observe access credentials") as error:
                guard_access_credentials(target, job({}), {})
            self.assertNotIn("private-credential", str(error.exception))

    def test_template_reset_is_rejected_before_job_write_and_recovers_with_reference(self):
        name = "LUMA_OBSERVE_READ_TOKEN"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {
                "LUMA_CONTROL_STATE_DIR": str(root / "state"),
                "LUMA_CONTROL_CONFIG": str(root / "luma.yaml"),
            }):
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"manager": {"name": "manager", "region": "cn", "status": "manager"}}
                state["scopedSecrets"] = {"luma-observe": {name: "r" * 40}}
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({
                    "defaults": {"stackRoot": str(root / "stacks")},
                }))
                target = root / "stacks/compose/luma-observe/luma-observe.nomad.json"
                target.parent.mkdir(parents=True)
                previous = job({name: "r" * 40})
                target.write_text(previous)
                manifest = yaml.safe_dump({
                    "name": "luma-observe", "compose": "docker-compose.yml", "region": "cn",
                    "services": {"observe-access": {"node": "manager", "exposure": "none"}},
                })

                def deploy(value):
                    return server.handle_compose_deployment(state["deployToken"], {
                        "manifest": manifest, "composeContent": yaml.safe_dump({"services": {
                            "observe-access": {"image": "example/access:1", "network_mode": "host",
                                               "environment": {name: value}},
                        }}), "skipDns": True, "skipOrchestrator": True,
                    })

                with patch.object(server, "_cache_compose_images_on_builder",
                                  side_effect=lambda _config, _state, deployment, **_kw: (deployment, {})), \
                     patch.object(server, "deploy_to_nomad") as submit:
                    for _ in range(2):
                        with self.assertRaisesRegex(LumaError, "would disable configured access"):
                            deploy("")
                        self.assertEqual(target.read_text(), previous)
                        self.assertEqual(load_state()["scopedSecrets"]["luma-observe"][name], "r" * 40)
                    result = deploy("${" + name + "}")
                    self.assertTrue(any(step["name"] == "Check Observe access credentials"
                                        and step["status"] == "ok" for step in result["steps"]))
                    self.assertEqual(json.loads(target.read_text())["Job"]["TaskGroups"][0]["Tasks"][0]["Env"][name], "r" * 40)
                    submit.assert_not_called()


class ObserveRecoveryTests(unittest.TestCase):
    def test_managed_readers_are_not_sent_to_application_storage_validation(self):
        data = {"type": "bind", "source": "/srv/luma/data/luma-observe/log-shipper",
                "target": "/var/lib/log-shipper", "readonly": False}
        reader = {"type": "bind", "source": "/opt/luma/security/nomad/logs.d",
                  "target": "/run/luma-nomad-credential", "readonly": True}
        group = {"Name": "luma-observe", "Tasks": [
            {"Name": "log-shipper", "Driver": "docker", "Config": {"mount": [data, reader]}},
        ]}
        document = {"Job": {"ID": "luma-observe", "TaskGroups": [group]}}
        node = {"ID": "node-manager", "Meta": {"luma_node_name": "manager"}}
        state = {"nodes": {"manager": {"name": "manager"}}}
        with patch.object(nomad_security, "security_root", return_value=Path("/opt/luma/security/nomad")), \
             patch.object(nomad_security, "read_private_json", return_value={"mode": "enforce"}), \
             patch.object(nomad_security, "authorize_application") as authorize, \
             patch.object(server, "_protect_nomad_secrets", side_effect=lambda _c, _s, value: value), \
             patch.object(server, "nomad_addr", return_value="https://nomad"), \
             patch.object(server, "nomad_token", return_value="test-token"), \
             patch.object(server, "NomadApi") as api, \
             patch("luma.control.registry_targets.registry_candidate_nodes", return_value=[node]), \
             patch.object(server, "load_runtime_state", return_value=state), \
             patch.object(server, "_node_agent_is_ready", return_value=True), \
             patch.object(server, "_run_node_agent_task", return_value={"verified": True, "nodeId": node["ID"]}) as task:
            api.return_value.request.side_effect = [[{"ID": node["ID"], "Status": "ready"}], node]
            result = json.loads(server._prepare_nomad_workload_security(None, state, json.dumps(document)))
            self.assertEqual(task.call_args.args[3]["mounts"], [data])
            self.assertEqual(result, document)
            authorize.assert_called_once_with(document["Job"])

            # A writable, renamed or broader credential mount is still rejected
            # before the node is asked to change application storage permissions.
            for change in ({"readonly": False}, {"source": "/opt/luma/security/nomad/control.d"},
                           {"target": "/data"}):
                candidate = copy.deepcopy(document)
                candidate["Job"]["TaskGroups"][0]["Tasks"][0]["Config"]["mount"][1].update(change)
                task.reset_mock()
                with self.subTest(change=change), self.assertRaisesRegex(LumaError, "protected host directory"):
                    server._prepare_nomad_workload_security(None, state, json.dumps(candidate))
                task.assert_not_called()

    def test_failed_observe_update_keeps_application_otlp_wiring(self):
        deployment = ComposeDeploymentSpec(
            source=Path("luma.compose.yml"), compose_path=Path("docker-compose.yml"),
            compose={"services": {}}, name="luma-observe", region="cn",
            storage_classes={}, volumes={}, services={},
        )
        state = {"observeOtlpToken": "r" * 40}
        with patch.object(server, "mutate_state", side_effect=lambda fn: fn(state)), \
             patch.object(server, "manager_tailscale_ip", return_value="100.64.0.1"):
            # A failed first install must not enable application exporters.
            server._mark_compose_deployment(deployment, {}, "test", status="failed_partial")
            self.assertIsNone(server._observe_otlp_settings(state, "worker"))
            server._mark_compose_deployment(deployment, {}, "test", status="active")
            expected = server._observe_otlp_settings(state, "worker")
            self.assertIsNotNone(expected)
            for status in ("pending", "failed_partial", "pending", "failed_partial"):
                server._mark_compose_deployment(deployment, {}, "test", status=status)
                self.assertEqual(server._observe_otlp_settings(copy.deepcopy(state), "worker"), expected)
            state["deployments"]["compose"].pop("luma-observe")
            self.assertIsNone(server._observe_otlp_settings(state, "worker"))

    def test_legacy_active_observe_keeps_wiring_during_first_failed_update(self):
        deployment = ComposeDeploymentSpec(
            source=Path("luma.compose.yml"), compose_path=Path("docker-compose.yml"),
            compose={"services": {}}, name="luma-observe", region="cn",
            storage_classes={}, volumes={}, services={},
        )
        state = {"observeOtlpToken": "r" * 40, "deployments": {"compose": {
            "luma-observe": {"status": "active", "updatedAt": 123},
        }}}
        with patch.object(server, "mutate_state", side_effect=lambda fn: fn(state)), \
             patch.object(server, "manager_tailscale_ip", return_value="100.64.0.1"):
            server._mark_compose_deployment(deployment, {}, "test", status="pending")
            server._mark_compose_deployment(deployment, {}, "test", status="failed_partial")
            self.assertIsNotNone(server._observe_otlp_settings(state, "worker"))
