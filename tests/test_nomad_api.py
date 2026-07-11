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
        if callable(resp):
            resp = resp()
        if isinstance(resp, Exception):
            raise resp
        return resp


def _sequence(*values):
    remaining = list(values)

    def next_value():
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0]

    return next_value


def _job(*, version=2, modify_index=20, count=1):
    return {
        "ID": "app",
        "Version": version,
        "JobModifyIndex": modify_index,
        "TaskGroups": [{"Name": "app", "Count": count}],
    }


def _deployment(*, status="successful", version=2, modify_index=20, description=""):
    return {
        "ID": "deployment-123",
        "JobID": "app",
        "JobVersion": version,
        "JobModifyIndex": modify_index,
        "Status": status,
        "StatusDescription": description,
        "TaskGroups": {
            "app": {
                "DesiredTotal": 1,
                "PlacedAllocs": 1,
                "HealthyAllocs": 1 if status == "successful" else 0,
                "UnhealthyAllocs": 0,
            }
        },
        "CreateIndex": 21,
    }


def _allocation(*, alloc_id, version, healthy=True, status="running"):
    return {
        "ID": alloc_id,
        "JobID": "app",
        "JobVersion": version,
        "TaskGroup": "app",
        "DesiredStatus": "run",
        "ClientStatus": status,
        "DeploymentStatus": {"Healthy": healthy},
        "TaskStates": {
            "app": {
                "State": status,
                "Failed": status not in {"running", "pending"},
            }
        },
    }


class NomadApiTests(unittest.TestCase):
    def test_nomad_addr_defaults_to_local_agent(self):
        self.assertEqual(nomad_api.nomad_addr(cfg(), {}), "http://127.0.0.1:4646")

    def test_nomad_addr_state_override(self):
        self.assertEqual(
            nomad_api.nomad_addr(cfg(), {"nomadAddr": "http://100.1.1.1:4646/"}),
            "http://100.1.1.1:4646",
        )

    def test_deploy_waits_for_exact_deployment_and_new_allocations(self):
        old = _allocation(alloc_id="alloc-old", version=1)
        new = _allocation(alloc_id="alloc-new", version=2)
        fake = _FakeApi(
            {
                "POST /v1/jobs": {
                    "EvalID": "eval-123",
                    "JobModifyIndex": 20,
                },
                "GET /v1/evaluation/eval-123": _sequence(
                    {
                        "ID": "eval-123",
                        "JobID": "app",
                        "JobModifyIndex": 20,
                        "Status": "pending",
                    },
                    {
                        "ID": "eval-123",
                        "JobID": "app",
                        "JobModifyIndex": 20,
                        "DeploymentID": "deployment-123",
                        "Status": "complete",
                    },
                ),
                "GET /v1/job/app": _job(),
                "GET /v1/deployment/deployment-123": _sequence(
                    _deployment(status="running"),
                    _deployment(),
                ),
                # A previous-version allocation remains running throughout
                # cutover. It must not satisfy the v2 barrier.
                "GET /v1/job/app/allocations": _sequence(
                    [old],
                    [old, new],
                ),
            }
        )
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake), mock.patch.object(
            nomad_api.time, "sleep", return_value=None
        ):
            job_json = json.dumps({"Job": {"ID": "app", "Name": "app"}})
            msg = nomad_api.deploy_to_nomad(cfg(), job_json, {}, slug="app")
        self.assertIn("rollout healthy", msg)
        self.assertIn("v2", msg)
        self.assertIn("eval eval-123", msg)
        self.assertIn("deployment deployment-123", msg)
        method, path, body = fake.calls[0]
        self.assertEqual((method, path), ("POST", "/v1/jobs"))
        self.assertEqual(body["Job"]["ID"], "app")
        allocation_calls = [
            call for call in fake.calls if call[:2] == ("GET", "/v1/job/app/allocations")
        ]
        self.assertEqual(len(allocation_calls), 2)

    def test_deploy_noop_verifies_current_version_and_allocations(self):
        fake = _FakeApi(
            {
                "POST /v1/jobs": {"EvalID": "", "JobModifyIndex": 20},
                "GET /v1/job/app": _job(),
                "GET /v1/job/app/deployments": [_deployment()],
                "GET /v1/job/app/allocations": [
                    _allocation(alloc_id="alloc-current", version=2)
                ],
            }
        )
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            msg = nomad_api.deploy_to_nomad(
                cfg(),
                json.dumps({"Job": {"ID": "app", "Name": "app"}}),
                {},
                slug="app",
            )
        self.assertIn("already healthy", msg)
        self.assertIn("no-op", msg)
        self.assertIn("v2", msg)
        self.assertFalse(
            any(path.startswith("/v1/evaluation/") for _, path, _ in fake.calls)
        )

    def test_deploy_ignores_terminal_predecessor_after_reschedule(self):
        predecessor = _allocation(
            alloc_id="alloc-failed",
            version=2,
            healthy=False,
            status="failed",
        )
        predecessor["NextAllocation"] = "alloc-current"
        fake = _FakeApi(
            {
                "POST /v1/jobs": {"EvalID": "", "JobModifyIndex": 20},
                "GET /v1/job/app": _job(),
                "GET /v1/job/app/deployments": [_deployment()],
                "GET /v1/job/app/allocations": [
                    predecessor,
                    _allocation(alloc_id="alloc-current", version=2),
                ],
            }
        )
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            msg = nomad_api.deploy_to_nomad(
                cfg(),
                json.dumps({"Job": {"ID": "app"}}),
                {},
                slug="app",
            )
        self.assertIn("1 allocations", msg)

    def test_healthy_deployment_marker_does_not_hide_dead_task_state(self):
        allocation = _allocation(alloc_id="alloc-restarting", version=2)
        allocation["TaskStates"]["app"] = {"State": "dead", "Failed": True}
        ready, healthy, detail = nomad_api._allocation_health(
            [allocation],
            target_version=2,
            expected_groups={"app": 1},
        )
        self.assertFalse(ready)
        self.assertEqual(healthy, 0)
        self.assertIn("healthy=0", detail)

    def test_deploy_rejects_blocked_evaluation_with_placement_context(self):
        fake = _FakeApi(
            {
                "POST /v1/jobs": {
                    "EvalID": "eval-blocked",
                    "JobModifyIndex": 20,
                },
                "GET /v1/evaluation/eval-blocked": {
                    "ID": "eval-blocked",
                    "JobID": "app",
                    "JobModifyIndex": 20,
                    "Status": "complete",
                    "BlockedEval": "eval-placement",
                    "FailedTGAllocs": {"app": {"NodesEvaluated": 2}},
                },
            }
        )
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            with self.assertRaisesRegex(
                LumaError,
                r"rollout blocked.*eval-placement.*task groups: app",
            ):
                nomad_api.deploy_to_nomad(
                    cfg(),
                    json.dumps({"Job": {"ID": "app"}}),
                    {},
                    slug="app",
                )

    def test_deploy_rejects_failed_cancelled_or_blocked_deployment(self):
        for status in ("failed", "cancelled", "blocked"):
            with self.subTest(status=status):
                fake = _FakeApi(
                    {
                        "POST /v1/jobs": {
                            "EvalID": "eval-123",
                            "JobModifyIndex": 20,
                        },
                        "GET /v1/evaluation/eval-123": {
                            "ID": "eval-123",
                            "JobID": "app",
                            "JobModifyIndex": 20,
                            "DeploymentID": "deployment-123",
                            "Status": "complete",
                        },
                        "GET /v1/job/app": _job(),
                        "GET /v1/deployment/deployment-123": _deployment(
                            status=status,
                            description="scheduler stopped rollout",
                        ),
                    }
                )
                with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
                    with self.assertRaisesRegex(
                        LumaError,
                        rf"rollout {status}.*deployment-123.*scheduler stopped rollout",
                    ):
                        nomad_api.deploy_to_nomad(
                            cfg(),
                            json.dumps({"Job": {"ID": "app"}}),
                            {},
                            slug="app",
                        )

    def test_deploy_times_out_while_evaluation_is_pending(self):
        fake = _FakeApi(
            {
                "POST /v1/jobs": {
                    "EvalID": "eval-pending",
                    "JobModifyIndex": 20,
                },
                "GET /v1/evaluation/eval-pending": {
                    "ID": "eval-pending",
                    "JobID": "app",
                    "JobModifyIndex": 20,
                    "Status": "pending",
                },
            }
        )
        clock = {"value": 0.0}

        def monotonic():
            return clock["value"]

        def sleep(seconds):
            clock["value"] += max(float(seconds), 0.1)

        with mock.patch.object(nomad_api, "NomadApi", return_value=fake), mock.patch.object(
            nomad_api.time,
            "monotonic",
            side_effect=monotonic,
        ), mock.patch.object(nomad_api.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(
                LumaError,
                r"timed out.*evaluation eval-pending.*last status: pending",
            ):
                nomad_api.deploy_to_nomad(
                    cfg(),
                    json.dumps({"Job": {"ID": "app"}}),
                    {},
                    slug="app",
                    rollout_timeout_seconds=0.25,
                )

    def test_deploy_rejects_superseded_job_before_allocation_success(self):
        fake = _FakeApi(
            {
                "POST /v1/jobs": {"EvalID": "", "JobModifyIndex": 20},
                "GET /v1/job/app": _sequence(
                    _job(),
                    _job(modify_index=21),
                ),
                "GET /v1/job/app/deployments": [],
            }
        )
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            with self.assertRaisesRegex(LumaError, r"rollout superseded.*21.*20"):
                nomad_api.deploy_to_nomad(
                    cfg(),
                    json.dumps({"Job": {"ID": "app"}}),
                    {},
                    slug="app",
                )

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
                    "JobSummary": {"Summary": {"granary": {"Running": 1, "Failed": 13}}},
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
                },
                {
                    "ID": "alloc-old",
                    "JobID": "granary",
                    "TaskGroup": "granary",
                    "DesiredStatus": "stop",
                    "ClientStatus": "failed",
                    "NodeName": "lab",
                    "TaskStates": {"mysql": {"State": "dead", "Failed": True}, "granary": {"State": "dead", "Failed": True}},
                },
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
        self.assertEqual(tasks["mysql"]["status"], "running")
        self.assertEqual(tasks["mysql"]["failed"], 0)
        self.assertEqual([row["id"] for row in tasks["mysql"]["tasks"]], ["alloc-1"])
        self.assertEqual(tasks["granary"]["fullName"], "granary_granary")

    def test_rescheduled_recovered_service_reports_running_not_failed(self):
        # A failed-then-rescheduled alloc keeps DesiredStatus="run" but carries
        # a NextAllocation pointer and a terminal ClientStatus. It must NOT be
        # counted as failed once a healthy replacement is running, otherwise a
        # recovered service shows "failed" until Nomad GCs the dead alloc (~1h).
        responses = {
            "GET /v1/jobs": [
                {
                    "ID": "app",
                    "Name": "app",
                    "Type": "service",
                    "Status": "running",
                    "Meta": {"luma.region": "home"},
                    "JobSummary": {"Summary": {"app": {"Running": 1}}},
                }
            ],
            "GET /v1/job/app": {
                "ID": "app",
                "Meta": {"luma.region": "home"},
                "TaskGroups": [
                    {
                        "Name": "app",
                        "Count": 1,
                        "Tasks": [
                            {"Name": "app", "Config": {"image": "app:latest"}, "Resources": {"CPU": 100, "MemoryMB": 256}},
                        ],
                    }
                ],
            },
            "GET /v1/job/app/allocations": [
                {
                    "ID": "alloc-dead",
                    "JobID": "app",
                    "TaskGroup": "app",
                    "DesiredStatus": "run",
                    "ClientStatus": "failed",
                    "NextAllocation": "alloc-new",
                    "NodeName": "lab",
                    "TaskStates": {"app": {"State": "dead", "Failed": True}},
                },
                {
                    "ID": "alloc-new",
                    "JobID": "app",
                    "TaskGroup": "app",
                    "DesiredStatus": "run",
                    "ClientStatus": "running",
                    "NodeName": "lab",
                    "TaskStates": {"app": {"State": "running"}},
                },
            ],
        }
        fake = _FakeApi(responses)
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            services = nomad_api.nomad_services_summary(cfg(), {})
        task = services[0]["tasks"][0]
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["failed"], 0)
        # only the live replacement survives the stale-reschedule filter
        self.assertEqual([row["id"] for row in task["tasks"]], ["alloc-new"])

    def test_old_failed_allocation_does_not_pollute_new_running_allocation(self):
        responses = {
            "GET /v1/jobs": [
                {
                    "ID": "app",
                    "Name": "app",
                    "Type": "service",
                    "Status": "running",
                    "Meta": {"luma.region": "home"},
                    "JobSummary": {"Summary": {"app": {"Running": 1, "Failed": 1}}},
                }
            ],
            "GET /v1/job/app": {
                "ID": "app",
                "Meta": {"luma.region": "home"},
                "TaskGroups": [
                    {
                        "Name": "app",
                        "Count": 1,
                        "Tasks": [
                            {"Name": "app", "Config": {"image": "app:latest"}, "Resources": {"CPU": 100, "MemoryMB": 256}},
                        ],
                    }
                ],
            },
            "GET /v1/job/app/allocations": [
                {
                    "ID": "alloc-old",
                    "JobID": "app",
                    "TaskGroup": "app",
                    "DesiredStatus": "run",
                    "ClientStatus": "failed",
                    "NodeName": "lab",
                    "TaskStates": {"app": {"State": "dead", "Failed": True}},
                },
                {
                    "ID": "alloc-new",
                    "JobID": "app",
                    "TaskGroup": "app",
                    "DesiredStatus": "run",
                    "ClientStatus": "running",
                    "NodeName": "lab",
                    "TaskStates": {"app": {"State": "running"}},
                },
            ],
        }
        fake = _FakeApi(responses)
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            services = nomad_api.nomad_services_summary(cfg(), {})
        task = services[0]["tasks"][0]
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["failed"], 0)
        self.assertEqual([row["id"] for row in task["tasks"]], ["alloc-new"])

    def test_partial_replica_failure_is_not_hidden_by_one_running_allocation(self):
        responses = {
            "GET /v1/jobs": [
                {
                    "ID": "app",
                    "Name": "app",
                    "Type": "service",
                    "Status": "running",
                    "Meta": {"luma.region": "home"},
                    "JobSummary": {"Summary": {"app": {"Running": 1, "Failed": 1}}},
                }
            ],
            "GET /v1/job/app": {
                "ID": "app",
                "Meta": {"luma.region": "home"},
                "TaskGroups": [
                    {
                        "Name": "app",
                        "Count": 2,
                        "Tasks": [
                            {"Name": "app", "Config": {"image": "app:latest"}, "Resources": {"CPU": 100, "MemoryMB": 256}},
                        ],
                    }
                ],
            },
            "GET /v1/job/app/allocations": [
                {
                    "ID": "alloc-ok",
                    "JobID": "app",
                    "TaskGroup": "app",
                    "DesiredStatus": "run",
                    "ClientStatus": "running",
                    "NodeName": "lab",
                    "TaskStates": {"app": {"State": "running"}},
                },
                {
                    "ID": "alloc-bad",
                    "JobID": "app",
                    "TaskGroup": "app",
                    "DesiredStatus": "run",
                    "ClientStatus": "failed",
                    "NodeName": "lab",
                    "TaskStates": {"app": {"State": "dead", "Failed": True}},
                },
            ],
        }
        fake = _FakeApi(responses)
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            services = nomad_api.nomad_services_summary(cfg(), {})
        task = services[0]["tasks"][0]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["running"], 1)
        self.assertEqual(task["failed"], 1)
        self.assertEqual([row["id"] for row in task["tasks"]], ["alloc-bad", "alloc-ok"])


if __name__ == "__main__":
    unittest.main()
