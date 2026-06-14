import json
import unittest
from unittest import mock

from luma.config import LumaConfig
from luma.errors import LumaError
from luma import nomad_api


def cfg():
    return LumaConfig({"defaults": {}}, None)


class _FakeApi:
    """Stand-in for NomadApi that records calls and returns scripted responses."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        key = f"{method} {path.split('?')[0]}"
        resp = self.responses.get(key, self.responses.get(method))
        if isinstance(resp, Exception):
            raise resp
        return resp


class NomadApiTests(unittest.TestCase):
    def test_nomad_addr_defaults_to_local_agent(self):
        self.assertEqual(nomad_api.nomad_addr(cfg(), {}), "http://127.0.0.1:4646")

    def test_nomad_addr_state_override(self):
        self.assertEqual(
            nomad_api.nomad_addr(cfg(), {"nomadAddr": "http://100.1.1.1:4646/"}),
            "http://100.1.1.1:4646",
        )

    def test_deploy_unwraps_job_and_posts_to_jobs(self):
        fake = _FakeApi({"POST /v1/jobs": {"EvalID": "eval-123"}})
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            job_json = json.dumps({"Job": {"ID": "app", "Name": "app"}})
            msg = nomad_api.deploy_to_nomad(cfg(), job_json, {}, slug="app")
        self.assertIn("eval-123", msg)
        method, path, body = fake.calls[0]
        self.assertEqual((method, path), ("POST", "/v1/jobs"))
        self.assertEqual(body["Job"]["ID"], "app")

    def test_deploy_rejects_missing_job_object(self):
        fake = _FakeApi({})
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            with self.assertRaises(LumaError):
                nomad_api.deploy_to_nomad(cfg(), json.dumps({"NotJob": {}}), {}, slug="app")

    def test_remove_purges_job(self):
        fake = _FakeApi({"DELETE": None})
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            nomad_api.remove_from_nomad(cfg(), {}, slug="app")
        method, path, _ = fake.calls[0]
        self.assertEqual(method, "DELETE")
        self.assertIn("/v1/job/app", path)
        self.assertIn("purge=true", path)

    def test_job_versions_parses_real_api_shape(self):
        # Shape confirmed against a live Nomad 1.9.7 /v1/job/{id}/versions response.
        fake = _FakeApi(
            {
                "GET /v1/job/app/versions": {
                    "Versions": [
                        {"Version": 2, "Stable": True, "SubmitTime": 1700000002,
                         "TaskGroups": [{"Tasks": [{"Config": {"image": "app:v2"}}]}]},
                        {"Version": 1, "Stable": True, "SubmitTime": 1700000001,
                         "TaskGroups": [{"Tasks": [{"Config": {"image": "app:v1"}}]}]},
                    ]
                }
            }
        )
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            versions = nomad_api.job_versions(cfg(), {}, slug="app")
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["version"], 2)
        self.assertEqual(versions[0]["image"], "app:v2")
        self.assertTrue(versions[0]["stable"])

    def test_revert_defaults_to_previous_version(self):
        responses = {
            "GET /v1/job/app/versions": {
                "Versions": [
                    {"Version": 5, "Stable": True, "SubmitTime": 5, "TaskGroups": []},
                    {"Version": 4, "Stable": True, "SubmitTime": 4, "TaskGroups": []},
                ]
            },
            "POST /v1/job/app/revert": {"EvalID": "eval-rev"},
        }
        fake = _FakeApi(responses)
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            msg = nomad_api.revert_job(cfg(), {}, slug="app")
        self.assertIn("v4", msg)
        revert_call = [c for c in fake.calls if c[0] == "POST"][0]
        self.assertEqual(revert_call[2], {"JobID": "app", "JobVersion": 4})

    def test_revert_explicit_version(self):
        fake = _FakeApi({"POST /v1/job/app/revert": {"EvalID": "e"}})
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            nomad_api.revert_job(cfg(), {}, slug="app", version=2)
        revert_call = [c for c in fake.calls if c[0] == "POST"][0]
        self.assertEqual(revert_call[2]["JobVersion"], 2)

    def test_revert_refuses_when_no_previous(self):
        fake = _FakeApi({"GET /v1/job/app/versions": {"Versions": [{"Version": 0, "TaskGroups": []}]}})
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            with self.assertRaises(LumaError):
                nomad_api.revert_job(cfg(), {}, slug="app")

    def test_services_summary_expands_compose_job_tasks(self):
        responses = {
            "GET /v1/jobs": [
                {
                    "ID": "granary",
                    "Name": "granary",
                    "Type": "service",
                    "Status": "running",
                    "Meta": {"luma.region": "home", "luma.compose": "true"},
                    "JobSummary": {"Summary": {"granary": {"Running": 1}}},
                }
            ],
            "GET /v1/job/granary": {
                "ID": "granary",
                "Meta": {"luma.region": "home", "luma.compose": "true"},
                "TaskGroups": [
                    {
                        "Name": "granary",
                        "Count": 1,
                        "Networks": [{"ReservedPorts": [{"Label": "mysql", "Value": 3306, "To": 3306}]}],
                        "Tasks": [
                            {"Name": "mysql", "Config": {"image": "mysql:8", "ports": ["mysql"]}, "Resources": {"CPU": 100, "MemoryMB": 256}},
                            {"Name": "granary", "Config": {"image": "granary:latest"}, "Resources": {"CPU": 100, "MemoryMB": 256}},
                        ],
                    }
                ],
            },
            "GET /v1/job/granary/allocations": [
                {
                    "ID": "alloc-1",
                    "JobID": "granary",
                    "TaskGroup": "granary",
                    "DesiredStatus": "run",
                    "ClientStatus": "running",
                    "NodeName": "lab",
                    "TaskStates": {"mysql": {"State": "running"}, "granary": {"State": "running"}},
                }
            ],
        }
        fake = _FakeApi(responses)
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            services = nomad_api.nomad_services_summary(cfg(), {})
        self.assertEqual(len(services), 1)
        self.assertTrue(services[0]["compose"])
        tasks = {item["name"]: item for item in services[0]["tasks"]}
        self.assertEqual(tasks["mysql"]["fullName"], "granary_mysql")
        self.assertEqual(tasks["mysql"]["targetPort"], "3306")
        self.assertEqual(tasks["mysql"]["nodes"], ["lab"])
        self.assertEqual(tasks["granary"]["fullName"], "granary_granary")


if __name__ == "__main__":
    unittest.main()
