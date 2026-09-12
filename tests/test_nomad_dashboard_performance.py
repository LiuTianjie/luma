import copy
import unittest
from unittest import mock

from luma import nomad_api
from luma.config import LumaConfig
from luma.errors import LumaError


def job_stub(name="app", revision=20, namespace="default"):
    return {
        "ID": name, "Name": name, "Type": "service", "Namespace": namespace,
        "Status": "running", "JobModifyIndex": revision,
        "Meta": {"luma.region": "cn", "luma.compose": "true"},
        "JobSummary": {"Summary": {name: {"Running": 1}}},
    }


def job_spec(name="app", revision=20, image="app:one"):
    return {
        "ID": name, "JobModifyIndex": revision, "Meta": {"luma.region": "cn"},
        "TaskGroups": [{
            "Name": name, "Count": 1,
            "Tasks": [{"Name": "web", "Config": {"image": image}, "Env": {"SECRET": "not-cached"}}],
        }],
    }


class FakeApi:
    def __init__(self, *, address="http://nomad", token="token", jobs=None):
        self.api_url = address
        self.token = token
        self.jobs = jobs if jobs is not None else [job_stub()]
        self.specs = {job["ID"]: job_spec(job["ID"], job["JobModifyIndex"]) for job in self.jobs}
        self.allocations = [{
            "JobID": "app", "ID": "allocation", "TaskGroup": "app",
            "DesiredStatus": "run", "ClientStatus": "running", "NodeName": "node-one",
        }]
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append(path)
        base = path.split("?")[0]
        if base == "/v1/jobs":
            return copy.deepcopy(self.jobs)
        if base == "/v1/allocations":
            if isinstance(self.allocations, Exception):
                raise self.allocations
            return copy.deepcopy(self.allocations)
        name = base.split("/")[3]
        if base.endswith("/allocations"):
            return [] if isinstance(self.allocations, Exception) else copy.deepcopy(self.allocations)
        payload = self.specs[name]
        if isinstance(payload, Exception):
            raise payload
        return copy.deepcopy(payload)


class NomadDashboardPerformanceTests(unittest.TestCase):
    def setUp(self):
        nomad_api._job_spec_cache.clear()
        self.addCleanup(nomad_api._job_spec_cache.clear)
        self.config = LumaConfig({"defaults": {}}, None)

    def summary(self, fake, **kwargs):
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            return nomad_api.nomad_services_summary(self.config, {}, **kwargs)

    def test_unchanged_spec_is_reused_but_allocation_state_is_fresh(self):
        fake = FakeApi()
        first = self.summary(fake)
        fake.allocations[0].update(ClientStatus="failed", NodeName="node-two")
        second = self.summary(fake)
        self.assertEqual(first[0]["tasks"][0]["status"], "running")
        self.assertEqual(second[0]["tasks"][0]["status"], "failed")
        self.assertEqual(second[0]["tasks"][0]["nodes"], ["node-two"])
        self.assertEqual(fake.calls.count("/v1/job/app"), 1)
        self.assertEqual(fake.calls.count("/v1/jobs?meta=true"), 2)
        self.assertEqual(fake.calls.count("/v1/allocations"), 2)
        self.assertEqual(fake.READ_TIMEOUT, 5)
        cached_spec = next(iter(nomad_api._job_spec_cache.values()))[2]
        self.assertNotIn("Env", cached_spec["TaskGroups"][0]["Tasks"][0])

    def test_new_revision_refreshes_image_and_failure_never_uses_old_spec(self):
        fake = FakeApi()
        self.summary(fake)
        fake.jobs[0]["JobModifyIndex"] = 21
        fake.specs["app"] = job_spec(revision=21, image="app:two")
        self.assertEqual(self.summary(fake)[0]["tasks"][0]["image"], "app:two")
        fake.jobs[0]["JobModifyIndex"] = 22
        fake.specs["app"] = LumaError("unavailable")
        self.assertNotIn("tasks", self.summary(fake)[0])
        self.assertEqual(len(nomad_api._job_spec_cache), 0)
        fake.specs["app"] = job_spec(revision=22, image="app:three")
        self.assertEqual(self.summary(fake)[0]["tasks"][0]["image"], "app:three")

    def test_missing_or_racing_revision_is_not_cached(self):
        fake = FakeApi()
        fake.specs["app"]["JobModifyIndex"] = 21
        self.summary(fake)
        self.summary(fake)
        self.assertEqual(fake.calls.count("/v1/job/app"), 2)
        self.assertFalse(nomad_api._job_spec_cache)
        fake.jobs[0].pop("JobModifyIndex")
        self.summary(fake)
        self.summary(fake)
        self.assertEqual(fake.calls.count("/v1/job/app"), 4)

    def test_endpoint_token_and_namespace_are_isolated(self):
        for fake in (
            FakeApi(), FakeApi(address="http://other-nomad"),
            FakeApi(token="other-token"), FakeApi(jobs=[job_stub(namespace="private")]),
        ):
            nomad_api._job_details_by_id(fake, fake.jobs)
            self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(nomad_api._job_spec_cache), 4)
        self.assertEqual(fake.calls, ["/v1/job/app?namespace=private"])
        self.assertNotIn("other-token", str(list(nomad_api._job_spec_cache)))

    def test_expired_and_oversize_specs_are_refetched(self):
        fake = FakeApi()
        with mock.patch.object(nomad_api.time, "monotonic", return_value=10):
            self.summary(fake)
        with mock.patch.object(nomad_api.time, "monotonic", return_value=311):
            self.summary(fake)
        self.assertEqual(fake.calls.count("/v1/job/app"), 2)
        nomad_api._job_spec_cache.clear()
        with mock.patch.object(nomad_api, "NOMAD_JOB_SPEC_CACHE_MAX_ENTRY_BYTES", 1):
            self.summary(fake)
            self.summary(fake)
        self.assertEqual(fake.calls.count("/v1/job/app"), 4)
        self.assertFalse(nomad_api._job_spec_cache)

    def test_cache_is_lru_bounded_and_returned_details_cannot_mutate_it(self):
        fake = FakeApi(jobs=[job_stub("a"), job_stub("b"), job_stub("c")])
        with mock.patch.object(nomad_api, "NOMAD_JOB_SPEC_CACHE_MAX_ENTRIES", 2):
            for stub in fake.jobs:
                nomad_api._job_details_by_id(fake, [stub])
        self.assertEqual([key[-1] for key in nomad_api._job_spec_cache], ["b", "c"])
        first = nomad_api._job_details_by_id(fake, [fake.jobs[1]])
        first["b"]["TaskGroups"][0]["Tasks"][0]["Config"]["image"] = "mutated"
        second = nomad_api._job_details_by_id(fake, [fake.jobs[1]])
        self.assertEqual(second["b"]["TaskGroups"][0]["Tasks"][0]["Config"]["image"], "app:one")

    def test_deleted_jobs_invalidate_cached_spec(self):
        fake = FakeApi()
        self.summary(fake)
        fake.jobs = []
        self.assertEqual(self.summary(fake), [])
        self.assertFalse(nomad_api._job_spec_cache)
        self.assertEqual(fake.calls.count("/v1/allocations"), 1)

    def test_targeted_detail_never_scans_unrelated_jobs_or_allocations(self):
        fake = FakeApi(jobs=[job_stub("app"), job_stub("app-worker"), job_stub("other")])
        rows = self.summary(fake, job_ids={"app"})
        self.assertEqual([row["jobId"] for row in rows], ["app"])
        self.assertEqual(fake.calls, [
            "/v1/jobs?meta=true&prefix=app", "/v1/job/app/allocations", "/v1/job/app",
        ])
        fake.calls.clear()
        self.assertEqual(self.summary(fake, job_ids=set()), [])
        self.assertEqual(fake.calls, [])

    def test_targeted_request_does_not_evict_other_jobs(self):
        fake = FakeApi(jobs=[job_stub("app"), job_stub("other")])
        self.summary(fake)
        self.summary(fake, job_ids={"app"})
        self.assertEqual({key[-1] for key in nomad_api._job_spec_cache}, {"app", "other"})

    def test_directory_reads_only_jobs_list_and_preserves_selector_metadata(self):
        fake = FakeApi(jobs=[job_stub("z"), job_stub("a")])
        fake.jobs[0]["Meta"]["luma.lae"] = "true"
        with mock.patch.object(nomad_api, "NomadApi", return_value=fake):
            rows = nomad_api.nomad_service_directory(self.config, {})
        self.assertEqual(fake.calls, ["/v1/jobs?meta=true"])
        self.assertEqual(rows, [
            {"name": "a", "jobId": "a", "region": "cn", "compose": True, "managedBy": ""},
            {"name": "z", "jobId": "z", "region": "cn", "compose": True, "managedBy": "lae"},
        ])

    def test_expired_enrichment_deadline_stops_queued_requests_but_allows_cache(self):
        fake = FakeApi()
        nomad_api._job_details_by_id(fake, fake.jobs)
        fake.calls.clear()
        with mock.patch.object(nomad_api.time, "monotonic", return_value=100):
            self.assertEqual(nomad_api._job_allocations_by_id(fake, fake.jobs, deadline=99), {"app": []})
            self.assertIn("TaskGroups", nomad_api._job_details_by_id(fake, fake.jobs, deadline=99)["app"])
            fake.jobs[0]["JobModifyIndex"] += 1
            self.assertEqual(nomad_api._job_details_by_id(fake, fake.jobs, deadline=99), {"app": {}})
        self.assertEqual(fake.calls, [])

    def test_bulk_allocation_failure_falls_back_to_each_job(self):
        fake = FakeApi(jobs=[job_stub("app"), job_stub("other")])
        fake.allocations = LumaError("bulk unavailable")
        self.assertEqual(len(self.summary(fake)), 2)
        self.assertIn("/v1/job/app/allocations", fake.calls)
        self.assertIn("/v1/job/other/allocations", fake.calls)

    def test_slow_bulk_failure_does_not_start_more_enrichment_waves(self):
        fake = FakeApi(jobs=[job_stub(str(index)) for index in range(80)])
        clock = {"now": 0.0}
        original_request = fake.request

        def request(method, path, body=None):
            if path == "/v1/allocations":
                fake.calls.append(path)
                clock["now"] = nomad_api.NOMAD_SUMMARY_ENRICH_TIMEOUT_SECONDS
                raise LumaError("bulk read exhausted enrichment budget")
            return original_request(method, path, body)

        fake.request = request
        with mock.patch.object(nomad_api.time, "monotonic", side_effect=lambda: clock["now"]):
            rows = self.summary(fake)
        self.assertEqual(len(rows), 80)
        self.assertEqual(fake.calls, ["/v1/jobs?meta=true", "/v1/allocations"])
        self.assertTrue(all("tasks" not in row for row in rows))


if __name__ == "__main__":
    unittest.main()
