"""Observe deployment regressions: saved credentials survive source upgrades."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from luma.control.observe_access import ACCESS_CREDENTIALS, guard_access_credentials
from luma.control import server
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError


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
