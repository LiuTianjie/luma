"""Real encrypted Variables, workload identities and legacy API cleanup.

Opt in only on an isolated Linux Docker test host. With the flag enabled, a
missing binary/daemon is a failure, never a skipped security assertion.
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from luma import nomad_acl, nomad_node, nomad_pki, nomad_secrets, nomad_security, secret_migration, secret_storage
from luma.control import database, state
from luma.errors import LumaError
from luma.nomad_api import NomadApi
from tests.test_nomad_security import SecurityFixture, nomad_test_ports


@unittest.skipUnless(os.environ.get("LUMA_TEST_SECRET_DELIVERY") == "1", "enable isolated real Nomad/Docker secret verification")
class NomadSecretDelivery(SecurityFixture):
    def setUp(self):
        super().setUp()
        self.assertEqual(os.geteuid(), 0, "real Docker secret verification requires an isolated Linux root runner")
        self.assertEqual(os.uname().sysname, "Linux")
        self.binary = os.environ.get("LUMA_TEST_NOMAD_BINARY", "")
        self.assertTrue(Path(self.binary).is_file(), "LUMA_TEST_NOMAD_BINARY must name the pinned binary")
        self.assertIsNotNone(shutil.which("docker"))
        subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20)
        self.image = os.environ.get("LUMA_TEST_SECRET_IMAGE", "python:3.12-alpine")
        subprocess.run(["docker", "image", "inspect", self.image], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20)
        self.jobs = []
        self.process = None
        self.log = open(self.root / "nomad.log", "w")
        self.addCleanup(self.close)
        self.prepare()
        nomad_pki.prepare_manager("100.64.0.1", admin_root=self.admin, service_root=self.runtime)
        self.tls = nomad_pki.tls_paths(self.runtime / "control.d")
        self.ports = nomad_test_ports()
        self.address = "https://127.0.0.1:" + str(self.ports[0])
        self.factory = lambda address, token="": NomadApi(address, token=token, tls=self.tls, managed_transport=False)
        self.start(client=False)
        nomad_acl.activate_acl(address=self.address, admin_root=self.admin, service_root=self.runtime, api_factory=self.factory)
        self.admin_api = self.factory(self.address, nomad_security.private_read(self.admin / "management.token").strip())
        self.control = self.factory(self.address, nomad_security.private_read(nomad_security.service_token_path(self.runtime, "control")).strip())
        self.reader = self.factory(self.address, nomad_security.private_read(nomad_security.service_token_path(self.runtime, "agent")).strip())
        # Leader election precedes asynchronous signing-key initialization.
        # Exercise real signing readiness before starting the Docker client.
        introduction = self.wait_for(lambda: self.control.request("PUT", "/v1/acl/identity/client-introduction-token", {
            "NodeName": "secret-fixture", "NodePool": "default", "TTL": 600_000_000_000,
        }), "Nomad node introduction signing key")
        nomad_security.private_write(self.root / "data" / "client" / "intro_token.jwt", introduction["JWT"])
        self.stop()
        self.start(client=True)
        self.wait_for(lambda: any(node.get("Status") == "ready" for node in self.control.request("GET", "/v1/nodes")), "ready Docker client")
        nomad_security.private_write(self.runtime / "transport.json", '{"http":true}')
        for patch in (
            mock.patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.root / "control")}),
            mock.patch.object(secret_storage, "KEY_ROOT", self.root / "keys"),
            mock.patch.object(secret_storage, "KEY_FILE", self.root / "keys" / "control-data.key"),
            mock.patch.object(secret_storage, "_client", side_effect=lambda administrator=False: self.admin_api if administrator else self.control),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        secret_storage._keys.clear()
        self.addCleanup(secret_storage._keys.clear)

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None

    def close(self):
        if hasattr(self, "admin_api"):
            for name in self.jobs:
                try:
                    self.admin_api.request("DELETE", "/v1/job/" + name + "?purge=true")
                except LumaError:
                    pass
                # Only exact fixture-owned job labels, even on a CI host with
                # other Docker workloads. Never use daemon-wide prune/kill.
                found = subprocess.run(["docker", "ps", "-aq", "--filter", "label=com.hashicorp.nomad.job_id=" + name],
                                       text=True, capture_output=True, timeout=20)
                for identifier in found.stdout.split():
                    subprocess.run(["docker", "rm", "-f", identifier], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        self.stop()
        # Nomad shutdown can leave its task tmpfs mounted, especially for an
        # intentionally denied allocation. Detach only this fixture's mounts
        # before TemporaryDirectory removes its private state.
        mounted = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            path = Path(line.split()[4])
            if self.root in path.parents:
                mounted.append(path)
        for path in sorted(mounted, key=lambda path: len(path.parts), reverse=True):
            subprocess.run(["umount", "-l", str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        self.log.close()

    def start(self, *, client, migration=False, source_limit="0"):
        config = nomad_node.render_agent_config(os_name="linux", role="server", tailscale_ip="100.64.0.1", region="cn",
                                               node_name="secret-fixture", client_enabled=client, secret_migration=migration,
                                               tls=nomad_pki.tls_paths(self.admin / "tls"))
        config = config.replace('"/opt/nomad/data"', json.dumps(str(self.root / "data")))
        config = config.replace('"127.0.0.1 100.64.0.1"', '"127.0.0.1"').replace('"100.64.0.1"', '"127.0.0.1"')
        config = config.replace('job_max_source_size = "0"', 'job_max_source_size = "' + source_limit + '"')
        config += f'\nports {{ http = {self.ports[0]} rpc = {self.ports[1]} serf = {self.ports[2]} }}\n'
        path = self.root / "nomad.hcl"
        path.write_text(config)
        # Use exactly the service's allowlist and deliberately poison its
        # parent environment. No host secret may become an application Env.
        self.process = subprocess.Popen([*nomad_node.clean_agent_environment("linux"), self.binary, "agent", "-config", str(path)],
                                        stdout=self.log, stderr=subprocess.STDOUT,
                                        env={**os.environ, "HOST_SECRET_MUST_NOT_REACH_TASK": "host-only-canary"})
        self.wait_for(lambda: self.factory(self.address).request("GET", "/v1/status/leader"), "Nomad leader")

    def wait_for(self, callback, description, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                break
            try:
                value = callback()
                if value:
                    return value
            except LumaError:
                pass
            time.sleep(0.3)
        self.fail("timed out waiting for " + description + ": " + json.dumps(getattr(self, "last_allocations", [])) + "\n" + (self.root / "nomad.log").read_text()[-5000:])

    def job(self, suffix, secret):
        name = "luma-secret-" + suffix + "-" + self.root.name[-8:]
        if name not in self.jobs:
            self.jobs.append(name)
        code = ("import hashlib,json,os,sys,time; "
                "digest=lambda s:hashlib.sha256(s.encode()).hexdigest(); "
                "print(json.dumps({'secret':digest(os.environ['API_KEY']),'literal':digest(os.environ['LITERAL']),"
                "'argument':digest(sys.argv[1]),'escaped':sys.argv[2],'runtime':sys.argv[3],"
                "'hostLeak':'HOST_SECRET_MUST_NOT_REACH_TASK' in os.environ,"
                "'identityLeak':any(k.startswith('NOMAD_TOKEN') for k in os.environ),"
                "'otherLeak':'OTHER_APP_ONLY' in os.environ}),flush=True); time.sleep(600)")
        return {"ID": name, "Type": "service", "Datacenters": ["dc1"], "TaskGroups": [{"Name": "group", "Count": 1,
            "Update": {"MaxParallel": 1, "MinHealthyTime": 100_000_000, "HealthyDeadline": 60_000_000_000, "ProgressDeadline": 90_000_000_000},
            "Tasks": [{"Name": "web", "Driver": "docker", "Env": {"API_KEY": secret, "LITERAL": "${secret.luma.literal}"},
                "Resources": {"CPU": 50, "MemoryMB": 64},
                "Config": {"image": self.image, "network_mode": "host", "command": "python", "args": ["-u", "-c", code, secret,
                            "$${API_KEY}", "prefix_${NOMAD_ALLOC_ID}_${NOMAD_TASK_NAME}"]}}]}]}

    def allocation(self, job):
        name, version = job["ID"], job["Version"]
        def running():
            allocations = self.control.request("GET", "/v1/job/" + name + "/allocations") or []
            self.last_allocations = [{key: a.get(key) for key in ("ID", "JobVersion", "ClientStatus", "DesiredStatus", "TaskStates")} for a in allocations]
            for allocation in allocations:
                if allocation["JobVersion"] == version and allocation["ClientStatus"] == "running":
                    return allocation
        return self.wait_for(running, "running allocation for " + name)

    def result(self, allocation):
        def read():
            containers = subprocess.check_output(["docker", "ps", "-q", "--filter", "label=com.hashicorp.nomad.alloc_id=" + allocation["ID"]], text=True).split()
            if not containers:
                return None
            output = subprocess.check_output(["docker", "logs", containers[0]], text=True)
            return json.loads(output.splitlines()[0]) if output.strip() else None
        return self.wait_for(read, "application secret digest")

    def raw_register(self, job, submission=None):
        # Deliberately bypass Luma admission only to test Nomad's independent
        # workload-identity boundary and to construct pre-upgrade fixtures.
        payload = {"Job": job}
        if submission is not None:
            payload["Submission"] = submission
        request = urllib.request.Request(self.address + "/v1/jobs", method="POST", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", "X-Nomad-Token": self.admin_api.token})
        with self.admin_api._opener().open(request, timeout=30) as response:
            return json.load(response)

    def test_fresh_encryption_real_delivery_and_cross_application_denial(self):
        secret_storage.provision()
        values = ['new-key-a-雪-"quoted"\\\n${secret.luma.literal}\n$(never-execute)\t', "new-key-b-only"]
        # Control has no host recovery-key mount. Exercise its real scoped ACL
        # read of the encrypted key Variable on a cold codec cache.
        secret_storage._keys.clear()
        with mock.patch.object(secret_storage, "KEY_FILE", self.root / "not-mounted-in-control"):
            state.save_state({"clusterId": "fixture", "deployToken": "test-admin", "secrets": {"API_KEY": values[0]}})
            self.assertEqual(state.load_state()["secrets"]["API_KEY"], values[0])
        jobs = []
        for suffix, value in zip(("a", "b"), values):
            job = self.job(suffix, value)
            if suffix == "a":
                job["TaskGroups"][0]["Tasks"][0]["Env"]["OTHER_APP_ONLY"] = "only-a"
            nomad_secrets.protect_job(job, self.control)
            self.control.request("POST", "/v1/jobs", {"Job": job})
            live = self.control.request("GET", "/v1/job/" + job["ID"])
            allocation = self.allocation(live)
            result = self.result(allocation)
            self.assertEqual(result["secret"], hashlib.sha256(value.encode()).hexdigest())
            self.assertEqual(result["argument"], result["secret"])
            self.assertEqual(result["literal"], hashlib.sha256(b"${secret.luma.literal}").hexdigest())
            self.assertEqual(result["escaped"], "${API_KEY}")
            self.assertEqual(result["runtime"], "prefix_" + allocation["ID"] + "_web")
            self.assertFalse(result["hostLeak"])
            self.assertFalse(result["identityLeak"])
            self.assertEqual(result["otherLeak"], suffix == "a")
            for route in ("/v1/job/" + job["ID"], "/v1/job/" + job["ID"] + "/versions", "/v1/allocation/" + allocation["ID"]):
                detail = self.reader.request("GET", route)
                serialized = json.dumps(detail, ensure_ascii=False)
                for prefix in ("new-key-a", "new-key-b-only"):
                    self.assertNotIn(prefix, serialized)
                self.assertFalse(detail.get("SignedIdentities"))
            jobs.append(live)
        for path in (secret_storage.KEY_VARIABLE, nomad_secrets.variable_path(jobs[0]["ID"], "group", "web")):
            with self.assertRaisesRegex(LumaError, "403"):
                nomad_secrets.read_variable(self.reader, path)
        backup = self.root / "backup.sqlite3"
        database.backup_database(backup)
        raft_files = list((self.root / "data" / "server").rglob("raft.db"))
        self.assertTrue(raft_files, "real Raft storage must be inspected")
        for file in [backup, database.database_path(), *raft_files]:
            self.assertNotIn(b"new-key-a", file.read_bytes())
            self.assertNotIn(b"new-key-b-only", file.read_bytes())
        intruder = copy.deepcopy(jobs[1])
        task = intruder["TaskGroups"][0]["Tasks"][0]
        victim = jobs[0]["TaskGroups"][0]["Tasks"][0]
        task["Secrets"] = copy.deepcopy(victim["Secrets"])
        task["Env"]["API_KEY"] = victim["Env"]["API_KEY"]
        self.raw_register(intruder)
        live = self.control.request("GET", "/v1/job/" + intruder["ID"])
        def denied():
            allocations = self.control.request("GET", "/v1/job/" + intruder["ID"] + "/allocations") or []
            current = [a for a in allocations if a["JobVersion"] == live["Version"]]
            self.assertFalse(any(a["ClientStatus"] == "running" for a in current), "cross-application secret binding started")
            text = json.dumps(current).lower() + (self.root / "nomad.log").read_text().lower()
            return current and "permission denied" in text
        self.wait_for(denied, "real workload identity permission denial", timeout=35)

    def test_migration_retires_tagged_history_sources_and_old_allocations(self):
        # Legacy cluster source retention and inline Env, constructed before
        # installing the encrypted-storage profile.
        self.stop()
        self.start(client=True, source_limit="1M")
        legacy = self.job("legacy", "old-key-must-leave-api")
        for index in range(3):
            legacy["TaskGroups"][0]["Tasks"][0]["Env"]["GENERATION"] = str(index)
            self.raw_register(legacy, {"Source": "old-key-must-leave-api", "Format": "json"})
            live = self.control.request("GET", "/v1/job/" + legacy["ID"])
            self.control.request("PUT", "/v1/job/" + live["ID"] + "/versions/legacy-" + str(index) + "/tag", {"Version": live["Version"]})
        old_allocation = self.allocation(live)
        secret_storage.provision()
        self.stop()
        self.start(client=True, migration=True)
        target = copy.deepcopy(live)
        target.pop("VersionTag", None)
        nomad_secrets.protect_job(target, self.control, preserve_runtime=True)
        self.control.request("POST", "/v1/jobs", {"Job": target, "Submission": {"Source": "", "Format": "json"}})
        protected = self.control.request("GET", "/v1/job/" + target["ID"])
        current_allocation = self.allocation(protected)
        self.assertNotEqual(old_allocation["ID"], current_allocation["ID"])
        secret_migration._retire_history(self.control, protected)
        versions = self.reader.request("GET", "/v1/job/" + target["ID"] + "/versions")["Versions"]
        self.assertEqual([item["Version"] for item in versions], [protected["Version"]])
        for version in range(protected["Version"]):
            with self.assertRaisesRegex(LumaError, "404"):
                self.reader.request("GET", "/v1/job/" + target["ID"] + "/submission?version=" + str(version))
        self.assertNotIn("old-key-must-leave-api", json.dumps(versions))
        def collected():
            self.admin_api.request("PUT", "/v1/system/gc")
            for node in self.admin_api.request("GET", "/v1/nodes"):
                self.admin_api.request("GET", "/v1/client/gc?node_id=" + node["ID"])
            allocations = self.reader.request("GET", "/v1/allocations") or []
            return all(a["JobVersion"] == protected["Version"] for a in allocations if a["JobID"] == protected["ID"])
        self.wait_for(collected, "legacy allocation garbage collection", timeout=60)
        self.assertEqual(self.result(current_allocation)["secret"], hashlib.sha256(b"old-key-must-leave-api").hexdigest())


if __name__ == "__main__":
    unittest.main()
