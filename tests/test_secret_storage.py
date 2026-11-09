"""Persistent encryption, private input and task-bound secret regressions."""
import base64
import copy
import io
import json
import os
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from luma import nomad_secrets, nomad_security, nomad_upgrade, secret_migration, secret_storage
from luma.control import database, state
from luma.errors import LumaError
from luma.local import LocalExecutor, LocalResult
from luma.secret_redaction import redact_document, restore_configuration

CANARY = 'nonproduction-key-雪-"quoted"\n${secret.luma.literal}\n$(never-execute)'


class VariableStore:
    """An API-shaped store for renderer tests; protocol tests use real Nomad."""
    def __init__(self):
        self.variables = {}
        self.index = 0
        self.writes = 0

    def error(self, code):
        cause = urllib.error.HTTPError("http://fixture", code, "fixture", {}, io.BytesIO())
        raise LumaError(f"Nomad API error {code}: fixture") from cause

    def request(self, method, route, body=None):
        if route == "/v1/acl/policies":
            return []
        parsed = urllib.parse.urlsplit(route)
        query = urllib.parse.parse_qs(parsed.query)
        namespace = query.get("namespace", ["default"])[0]
        if parsed.path.startswith("/v1/var/"):
            path = urllib.parse.unquote(parsed.path.removeprefix("/v1/var/"))
            key = namespace, path
            previous = self.variables.get(key)
            if method == "GET":
                if previous is None:
                    self.error(404)
                return copy.deepcopy(previous)
            if int(query.get("cas", ["0"])[0]) != int((previous or {}).get("ModifyIndex") or 0):
                self.error(409)
            self.index += 1
            self.writes += 1
            self.variables[key] = {"Namespace": namespace, "Path": path, "Items": copy.deepcopy(body["Items"]),
                                   "ModifyIndex": self.index, "ModifyTime": time.time_ns()}
            return copy.deepcopy(self.variables[key])
        raise AssertionError("unexpected fixture request: " + route)


class KeyFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="luma-secrets-unit-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.runtime = self.root / "runtime"
        self.fake = VariableStore()
        self.key = {"schema": 1, "id": "fixture-key", "key": base64.b64encode(bytes(range(32))).decode()}
        patches = [
            mock.patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.root / "control"), "LUMA_NOMAD_SECURITY_DIR": str(self.runtime), "LUMA_SECURITY_AUDIT_FILE": ""}),
            mock.patch.object(secret_storage, "KEY_ROOT", self.root / "keys"),
            mock.patch.object(secret_storage, "KEY_FILE", self.root / "keys" / "control-data.key"),
            mock.patch.object(secret_storage, "_client", return_value=self.fake),
        ]
        secret_storage._keys.clear()
        self.addCleanup(secret_storage._keys.clear)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def enable(self):
        nomad_security.private_write(secret_storage.KEY_FILE, json.dumps(self.key))
        self.fake.variables["default", secret_storage.KEY_VARIABLE] = {
            "Namespace": "default", "Path": secret_storage.KEY_VARIABLE,
            "Items": {"id": self.key["id"], "key": self.key["key"]}, "ModifyIndex": 1,
        }
        nomad_security.private_write(secret_storage.profile_path(), json.dumps({
            "schema": 1, "format": secret_storage.FORMAT, "keyId": self.key["id"], "keyVariable": secret_storage.KEY_VARIABLE,
        }))


class EncryptedPersistence(KeyFixture):
    def data(self):
        return {"clusterId": "fixture", "deployToken": "nonproduction-admin", "secrets": {"API_KEY": CANARY},
                "scopedSecrets": {"app-a": {"API_KEY": CANARY + "-a"}},
                "buildRuns": {"run-a": {"id": "run-a", "status": "queued", "request": {"envSecrets": {"API_KEY": CANARY}},
                                             "events": [{"message": CANARY}]}}, "nodes": {}}

    def test_fresh_database_and_backup_contain_no_plaintext_or_recovery_key(self):
        self.enable()
        data = self.data()
        state.save_state(data)
        self.assertEqual(state.load_state(), data)
        backup = self.root / "backup.sqlite3"
        self.assertTrue(database.backup_database(backup)["encrypted"])
        for path in (database.database_path(), backup):
            raw = path.read_bytes()
            self.assertNotIn(b"nonproduction-key", raw)
            self.assertNotIn(self.key["key"].encode(), raw)
            self.assertNotIn(b"nonproduction-admin", raw)
        with database.transaction(immediate=False) as conn:
            self.assertEqual(database.read_entity(conn, "buildRuns", "run-a")["request"]["envSecrets"]["API_KEY"], CANARY)
        with sqlite3.connect(database.database_path()) as conn:
            self.assertEqual(conn.execute("SELECT value FROM database_meta WHERE key='payload_cipher'").fetchone()[0], "aes256gcm-v1")

    def test_legacy_migration_survives_interruption_after_ciphertext_commit(self):
        original = self.data()
        state.save_state(original)
        self.assertIn(b"nonproduction-key", database.database_path().read_bytes())
        self.enable()
        connect = database.connect

        class Interrupted:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def execute(self, sql, *args):
                if sql == "VACUUM":
                    raise RuntimeError("simulated host interruption")
                return self.connection.execute(sql, *args)

        with mock.patch.object(database, "connect", side_effect=lambda *args, **kwargs: Interrupted(connect(*args, **kwargs))):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                database.migrate_encryption()
        self.assertEqual(state.load_state(), original)
        self.assertTrue(database.migrate_encryption()["encrypted"])
        self.assertEqual(state.load_state(), original)
        for path in (self.root / "control").glob("control.sqlite3*"):
            self.assertNotIn(b"nonproduction-key", path.read_bytes())

    def test_encryption_cleanup_keeps_late_writers_out_until_journals_are_cleared(self):
        original = self.data()
        state.save_state(original)
        self.enable()
        connect = database.connect
        probes = []
        class ConcurrentWriter:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def execute(self, sql, *args):
                if sql == "VACUUM":
                    script = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); "
                              "c.execute(\"INSERT INTO database_meta VALUES('late-writer','unexpected')\"); c.commit()")
                    probes.append(subprocess.run([sys.executable, "-c", script, str(database.database_path())], capture_output=True, text=True, timeout=10))
                return self.connection.execute(sql, *args)
        with mock.patch.object(database, "connect", side_effect=lambda *args, **kwargs: ConcurrentWriter(connect(*args, **kwargs))):
            self.assertTrue(database.migrate_encryption()["encrypted"])
        self.assertEqual(len(probes), 1)
        self.assertNotEqual(probes[0].returncode, 0)
        self.assertIn("locked", probes[0].stderr)
        self.assertEqual(state.load_state(), original)

    def test_old_reader_keeps_cleanup_pending_without_undoing_encryption(self):
        original = self.data()
        state.save_state(original)
        reader = sqlite3.connect(database.database_path(), isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT payload FROM control_config").fetchall()
        self.enable()
        connect = database.connect
        try:
            with mock.patch.object(database, "connect", side_effect=lambda *args, **kwargs: connect(*args, **{**kwargs, "timeout": 0})):
                with self.assertRaises(database.EncryptionCleanupPending):
                    database.migrate_encryption()
            self.assertEqual(state.load_state(), original)
        finally:
            reader.close()
        self.assertTrue(database.migrate_encryption()["encrypted"])
        for path in (self.root / "control").glob("control.sqlite3*"):
            self.assertNotIn(b"nonproduction-key", path.read_bytes())

    def test_pending_sqlite_cleanup_keeps_the_migration_unverified(self):
        self.enable()
        journal = {"secretMigration": {"policiesApplied": True, "backup": "fixture"}, "jobs": []}
        with mock.patch.object(secret_migration, "_save"), mock.patch.object(secret_migration, "_client"), mock.patch.object(database, "migrate_encryption", side_effect=database.EncryptionCleanupPending("readers still active")):
            with self.assertRaises(nomad_upgrade.Pending):
                secret_migration.migrate(journal, {})
            self.assertFalse(journal["secretMigration"].get("databaseEncrypted"))
            journal["secretMigration"]["databaseCleanupStartedAt"] = time.time() - 601
            with self.assertRaisesRegex(LumaError, "10 minutes"):
                secret_migration.migrate(journal, {})
            self.assertFalse(journal["secretMigration"].get("databaseEncrypted"))

    def test_secret_wait_is_caught_when_the_supervisor_uses_another_module_name(self):
        entry = runpy.run_path(str(Path(nomad_upgrade.__file__)), run_name="luma.migration_entry_test")
        with self.assertRaises(entry["Pending"]):
            secret_migration._pending("allocation is still starting")

    def test_tampering_record_swaps_and_missing_keys_fail_closed(self):
        self.enable()
        purpose = secret_storage.context("control_config", "secrets")
        first = secret_storage.dumps({"KEY": CANARY}, purpose)
        second = secret_storage.dumps({"KEY": CANARY}, purpose)
        self.assertNotEqual(first, second)
        self.assertEqual(secret_storage.dumps({"KEY": CANARY}, purpose, previous=first), first)
        with self.assertRaises(LumaError):
            secret_storage.loads(first, secret_storage.context("control_config", "other"))
        corrupt = json.loads(first)
        raw = bytearray(base64.b64decode(corrupt["ciphertext"]))
        raw[0] ^= 1
        corrupt["ciphertext"] = base64.b64encode(raw).decode()
        with self.assertRaisesRegex(LumaError, "authentication"):
            secret_storage.loads(json.dumps(corrupt), purpose)
        state.save_state(self.data())
        secret_storage.profile_path().unlink()
        with self.assertRaises(LumaError):
            state.load_state()
        with self.assertRaises(LumaError):
            state.save_state({"secrets": {"KEY": "must-not-be-written"}})
        self.assertNotIn(b"must-not-be-written", database.database_path().read_bytes())

    def test_archives_restore_only_after_authentication_and_keep_the_original(self):
        self.enable()
        original = self.root / "old-job.json"
        original.write_text(CANARY)
        archive = secret_storage.encrypt_file(original)
        self.assertTrue(original.exists())
        self.assertNotIn(b"nonproduction-key", archive.read_bytes())
        restored = self.root / "restored.json"
        secret_storage.decrypt_file(archive, restored)
        self.assertEqual(restored.read_text(), CANARY)
        raw = bytearray(archive.read_bytes())
        raw[-1] ^= 1
        archive.write_bytes(raw)
        rejected = self.root / "rejected.json"
        with self.assertRaises(LumaError):
            secret_storage.decrypt_file(archive, rejected)
        self.assertFalse(rejected.exists())
        self.assertFalse(list(self.root.glob(".luma-decrypt-*")))

    def test_alert_credentials_and_callbacks_join_the_same_migration(self):
        state.save_state(self.data())
        from luma.control import alerting
        with database.transaction() as conn:
            alerting._schema(conn)
            conn.execute("INSERT INTO alert_channels(id,name,type,app_id,app_secret,chat_id,enabled,created_at,updated_at) VALUES('one','one','feishu','app',?,'chat',1,1,1)", (CANARY,))
            conn.execute("INSERT INTO alert_feishu_callbacks VALUES('app',?,?)", (CANARY, CANARY))
        self.enable()
        database.migrate_encryption()
        with database.transaction(immediate=False) as conn:
            raw = conn.execute("SELECT app_secret FROM alert_channels WHERE id='one'").fetchone()[0]
            self.assertEqual(database.decode_secret(conn, raw, "alert_channels", "one", "app_secret"), CANARY)
            self.assertNotIn("nonproduction-key", raw)
            values = conn.execute("SELECT verification_token,encrypt_key FROM alert_feishu_callbacks WHERE app_id='app'").fetchone()
            for field, value in zip(("verification_token", "encrypt_key"), values):
                self.assertEqual(database.decode_secret(conn, value, "alert_feishu_callbacks", "app", field), CANARY)


class SecretRendering(KeyFixture):
    def setUp(self):
        super().setUp()
        self.enable()

    def job(self, name="app-a"):
        return {"ID": name, "Type": "service", "TaskGroups": [{"Name": "group", "Count": 1, "Tasks": [{
            "Name": "web", "Driver": "docker", "Env": {"API_KEY": CANARY, "LITERAL": "${secret.luma.not-an-expression}"},
            "Config": {"image": "example:fixture", "command": "/bin/sh", "args": ["-c", "printf '%s' $${API_KEY}", "${NOMAD_ALLOC_ID}"],
                       "auth": {"username": "fixture", "password": CANARY, "server_address": "registry.fixture"}},
        }]}]}

    def test_env_arguments_and_registry_auth_are_task_bound_references(self):
        job = nomad_secrets.protect_job(self.job(), self.fake)
        nomad_secrets.assert_protected_job(job)
        nomad_secrets.verify_bindings(self.fake, job)
        serialized = json.dumps(job)
        self.assertNotIn("nonproduction-key", serialized)
        self.assertNotIn("registry.fixture", serialized)
        group, task = job["TaskGroups"][0], job["TaskGroups"][0]["Tasks"][0]
        values = nomad_secrets.task_environment(self.fake, job, group, task)
        self.assertEqual(values["API_KEY"], CANARY)
        self.assertEqual(values["LITERAL"], "${secret.luma.not-an-expression}")
        self.assertEqual(task["Secrets"][0]["Path"], "nomad/jobs/app-a/group/web")
        self.assertFalse(task["Identity"]["Env"])
        self.assertFalse(task["Identity"]["File"])
        written = self.fake.writes
        self.assertEqual(nomad_secrets.protect_job(copy.deepcopy(job), self.fake), job)
        self.assertEqual(self.fake.writes, written)

    def test_cross_application_binding_and_missing_generation_are_rejected(self):
        job = nomad_secrets.protect_job(self.job(), self.fake)
        other = copy.deepcopy(job)
        other["ID"] = "app-b"
        with self.assertRaisesRegex(LumaError, "application identity"):
            nomad_secrets.assert_protected_job(other)
        self.fake.variables.pop(("default", "nomad/jobs/app-a/group/web"))
        with self.assertRaisesRegex(LumaError, "generation"):
            nomad_secrets.verify_bindings(self.fake, job)
        inline = self.job()
        with self.assertRaises(LumaError):
            nomad_secrets.assert_protected_job(inline)

    def test_render_context_and_control_subprocesses_exclude_other_application_keys(self):
        from luma.control import secrets as control_secrets
        data = {"secrets": {"APP_ONLY": CANARY, "UNRELATED": "other-app", "CUSTOM_DNS_TOKEN": "dns-token"},
                "scopedSecrets": {"app-a": {"APP_ONLY": "scoped-a"}}}
        config = self.root / "luma.yaml"
        config.write_text("providers:\n  dns:\n    provider: cloudflare\n    apiTokenEnv: CUSTOM_DNS_TOKEN\n")
        with mock.patch.dict(os.environ, {"LUMA_CONTROL_CONFIG": str(config), "APP_ONLY": "stale-before-rotation"}):
            control_secrets._apply_state_secrets(data)
            self.assertNotIn("APP_ONLY", os.environ)
            self.assertNotIn("UNRELATED", os.environ)
            self.assertEqual(os.environ["CUSTOM_DNS_TOKEN"], "dns-token")
            values, _ = control_secrets._render_secrets(data, scope="app-a", body={}, texts=["env: ${APP_ONLY}"])
            self.assertEqual(values, {"APP_ONLY": "scoped-a"})
            with self.assertRaisesRegex(LumaError, "missing scoped"):
                control_secrets._render_secrets(data, scope="app-a", body={}, texts=["env: ${UNRELATED}"])


class SecretUpgradeRecovery(KeyFixture):
    def setUp(self):
        super().setUp()
        self.enable()
        self.current = {"ID": "app", "Type": "service", "Version": 2, "JobModifyIndex": 10, "TaskGroups": [{"Name": "app", "Count": 1,
            "Tasks": [{"Name": "web", "Driver": "docker", "Env": {"API_KEY": CANARY}, "Config": {"image": "fixture", "args": ["sleep", "60"]}}]}]}
        self.item = {"Job": copy.deepcopy(self.current), "system": False, "authoritySHA256": nomad_security.authority_digest(self.current)}
        self.journal = {"id": "fixture", "jobs": [self.item]}
        self.path = self.root / "private-upgrade.json"
        self.submitted = 0
        self.lost = True
        self.healthy = True
        self.history = [copy.deepcopy(self.current)]
        self.disruptive_plan = False
        self.variables = self.fake.request
        self.fake.request = self.request
        for patch in (mock.patch.object(nomad_upgrade, "JOURNAL", self.path), mock.patch.object(secret_migration, "_grant"),
                      mock.patch.object(secret_migration, "_write_artifacts")):
            patch.start()
            self.addCleanup(patch.stop)

    def request(self, method, route, body=None):
        if route.startswith("/v1/var/"):
            return self.variables(method, route, body)
        if route == "/v1/job/app":
            return copy.deepcopy(self.current)
        if route == "/v1/job/app/versions":
            return {"Versions": copy.deepcopy(self.history)}
        if route == "/v1/job/app/plan":
            kind = "DestructiveUpdate" if self.disruptive_plan else "InPlaceUpdate"
            return {"Annotations": {"DesiredTGUpdates": {"app": {kind: 1}}}}
        if route == "/v1/job/app/allocations":
            return [{"JobVersion": self.current["Version"], "TaskGroup": "app", "ClientStatus": "running",
                     "TaskStates": {"web": {"State": "running", "Failed": False}}}] if self.healthy else []
        if method == "POST" and route == "/v1/jobs":
            self.assertEqual(body["JobModifyIndex"], self.current["JobModifyIndex"])
            self.submitted += 1
            self.current = {**copy.deepcopy(body["Job"]), "Version": self.current["Version"] + 1, "JobModifyIndex": self.current["JobModifyIndex"] + 1}
            self.history.insert(0, copy.deepcopy(self.current))
            if self.lost:
                self.lost = False
                raise LumaError("response lost after commit")
            return {"JobModifyIndex": self.current["JobModifyIndex"]}
        raise AssertionError(route)

    def test_response_loss_resumes_the_encrypted_intent_without_restarting_again(self):
        with self.assertRaisesRegex(LumaError, "response lost"):
            secret_migration._upgrade_job(self.journal, self.item, {}, self.fake)
        self.assertNotIn("nonproduction-key", self.path.read_text())
        saved = nomad_upgrade._read_journal()
        self.healthy = False
        with self.assertRaises(nomad_upgrade.Pending):
            secret_migration._upgrade_job(saved, saved["jobs"][0], {}, self.fake)
        self.healthy = True
        secret_migration._upgrade_job(saved, saved["jobs"][0], {}, self.fake)
        self.assertEqual(self.submitted, 1)
        self.assertTrue(nomad_upgrade._read_journal()["jobs"][0]["secretsMigrated"])

    def test_changed_live_job_is_rejected_before_migration_can_overwrite_it(self):
        with self.assertRaises(LumaError):
            secret_migration._upgrade_job(self.journal, self.item, {}, self.fake)
        saved = nomad_upgrade._read_journal()
        self.current["TaskGroups"][0]["Tasks"][0]["Config"]["auth"] = {"password": "unexpected"}
        with self.assertRaisesRegex(LumaError, "outside its saved"):
            secret_migration._upgrade_job(saved, saved["jobs"][0], {}, self.fake)
        self.assertEqual(self.submitted, 1)


    def test_evicted_history_recovery_resumes_after_a_lost_registration_reply(self):
        self.lost = False
        secret_migration._upgrade_job(self.journal, self.item, {}, self.fake)
        # An older migration could leave the live protected job absent from
        # the historical table while retaining its plaintext stable parent.
        self.history = []
        self.lost = True
        with self.assertRaisesRegex(LumaError, "response lost"):
            secret_migration._restore_history(self.journal, self.item, self.fake)
        saved = nomad_upgrade._read_journal()
        self.assertIn("secretHistoryIntent", saved["jobs"][0])
        self.healthy = False
        with self.assertRaises(nomad_upgrade.Pending):
            secret_migration._restore_history(saved, saved["jobs"][0], self.fake)
        self.healthy = True
        secret_migration._restore_history(saved, saved["jobs"][0], self.fake)
        self.assertEqual(self.submitted, 2)
        self.assertEqual(saved["jobs"][0]["Job"]["Version"], 4)
        self.assertNotIn("secretHistoryIntent", nomad_upgrade._read_journal()["jobs"][0])
        # A crash while removing the temporary tag must also resume; only
        # history metadata changed, not the recorded task definition.
        self.current.pop("VersionTag", None)
        secret_migration._restore_history(saved, saved["jobs"][0], self.fake)
        self.assertEqual(self.submitted, 2)

    def test_evicted_history_recovery_refuses_replacement_or_external_edits(self):
        self.lost = False
        secret_migration._upgrade_job(self.journal, self.item, {}, self.fake)
        self.history = []
        self.disruptive_plan = True
        with self.assertRaisesRegex(LumaError, "disrupt allocations"):
            secret_migration._restore_history(self.journal, self.item, self.fake)
        self.assertEqual(self.submitted, 1)
        saved = nomad_upgrade._read_journal()
        self.disruptive_plan = False
        self.current["Priority"] = 80
        with self.assertRaisesRegex(LumaError, "outside its saved"):
            secret_migration._restore_history(saved, saved["jobs"][0], self.fake)
        self.assertEqual(self.submitted, 1)


class PrivateInterfaces(unittest.TestCase):
    def test_installer_input_uses_stdin_and_redacts_error_values(self):
        executor = LocalExecutor()
        document = json.dumps({"secrets": {"API_KEY": CANARY}})
        with mock.patch("luma.local.os.geteuid", return_value=0), mock.patch.object(executor, "run_result", return_value=LocalResult(0, "ok")) as run:
            self.assertEqual(executor.sudo_input("trusted-installer", document), "ok")
        self.assertEqual(run.call_args.args, ("trusted-installer",))
        self.assertEqual(run.call_args.kwargs["input_data"], document)
        with mock.patch("luma.local.os.geteuid", return_value=0), mock.patch.object(executor, "run_result", return_value=LocalResult(1, "unsupported tmpfs noswap: " + CANARY)):
            with self.assertRaisesRegex(LumaError, "noswap") as caught:
                executor.sudo_input("trusted-installer", document)
        self.assertNotIn("nonproduction-key", str(caught.exception))

    def test_read_only_history_and_config_views_never_return_secret_values(self):
        raw = {"request": {"envSecrets": {"API_KEY": CANARY}, "registryAuth": {"password": CANARY}},
               "queuePayload": {"body": {"token": CANARY}},
               "manifest": "name: app\nenv:\n  API_KEY: nonproduction-key\ncommand: [run, --password=nonproduction-key]\n"}
        result = redact_document(raw)
        self.assertNotIn("nonproduction-key", json.dumps(result))
        self.assertNotIn("queuePayload", result)
        self.assertEqual(result["request"]["envSecretNames"], ["API_KEY"])
        self.assertIn("name: app", result["manifest"])
        self.assertIn("queuePayload", raw)
        import yaml
        self.assertEqual(yaml.safe_load(restore_configuration(result["manifest"], raw["manifest"])), yaml.safe_load(raw["manifest"]))
        with self.assertRaises(LumaError):
            restore_configuration(result["manifest"], "name: a-different-app")


if __name__ == "__main__":
    unittest.main()
