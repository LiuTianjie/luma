"""Fresh-install, rolling-upgrade and credential-boundary regression coverage."""
import copy
import hashlib
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from luma import agent, nomad_acl, nomad_enrollment, nomad_migration_node as host, nomad_node, nomad_pki as pki, nomad_security as security, nomad_upgrade as upgrade, secret_migration
from luma.local import LocalResult
from luma.errors import LumaError
from luma.nomad_api import NomadApi
from tests.test_nomad_security import SecurityFixture, nomad_test_ports, workload


class AuthorityAndPKI(SecurityFixture):
    def test_nomad_defaults_preserve_saved_application_authority(self):
        self.enforce()
        expected = workload(mounts=[{"type": "bind", "source": "/srv/luma/data/example", "target": "/data"}])
        group = expected["TaskGroups"][0]
        group["Networks"] = [{"Mode": "bridge"}]
        group["Tasks"][0]["Services"] = [{"Provider": "nomad", "Checks": [{"Type": "http"}]}]
        group["Tasks"][0]["Secrets"] = [{"Name": "luma", "Provider": "nomad", "Path": "nomad/jobs/example/app/app"}]
        security.authorize_application(expected)
        live = copy.deepcopy(expected)
        live["TaskGroups"][0]["Constraints"] = [
            *({"LTarget": "${attr.plugins.cni.version." + name + "}", "RTarget": ">= 0.4.0", "Operand": "semver"}
              for name in ("bridge", "firewall", "host-local", "loopback", "portmap")),
            {"LTarget": "${attr.nomad.service_discovery}", "RTarget": "true", "Operand": "="},
            {"LTarget": "${attr.nomad.version}", "RTarget": ">= 1.4.0", "Operand": "semver"},
            {"LTarget": "${attr.plugins.secrets.nomad.version}", "RTarget": "", "Operand": "is_set"},
        ]
        self.assertTrue(upgrade._matches_expected_job(live, expected))
        security.admit_job(live)
        for field, value in (("image", "unreviewed/image"), ("auth", {"username": "different"})):
            changed = copy.deepcopy(live)
            changed["TaskGroups"][0]["Tasks"][0]["Config"][field] = value
            with self.subTest(field=field):
                self.assertFalse(upgrade._matches_expected_job(changed, expected))
        changed = copy.deepcopy(live)
        changed["TaskGroups"][0]["Tasks"][0]["Secrets"][0]["Path"] = "nomad/jobs/another/app/app"
        self.assertFalse(upgrade._matches_expected_job(changed, expected))

    def test_explicit_constraints_remain_pinned_to_their_scope_and_bounds(self):
        for scope, constraint in (
            ("job", {"LTarget": "${meta.luma_region}", "RTarget": "cn", "Operand": "="}),
            ("group", {"LTarget": "${meta.luma_node}", "RTarget": "owner", "Operand": "="}),
            ("group", {"LTarget": "${attr.plugins.cni.version.bridge}", "RTarget": ">= 1.0.0", "Operand": "semver"}),
            ("group", {"LTarget": "${attr.nomad.version}", "RTarget": ">= 2.1.1", "Operand": "semver"}),
            ("group", {"LTarget": "${attr.nomad.service_discovery}", "RTarget": "true", "Operand": "!="}),
            ("job", {"LTarget": "${attr.nomad.version}", "RTarget": ">= 1.4.0", "Operand": "semver"}),
            ("task", {"LTarget": "${attr.plugins.cni.version.bridge}", "RTarget": ">= 0.4.0", "Operand": "semver"}),
        ):
            expected = workload()
            group = expected["TaskGroups"][0]
            group["Networks"] = [{"Mode": "bridge"}]
            group["Services"] = [{"Provider": "nomad", "Checks": [{"Type": "http"}]}]
            target = {"job": expected, "group": group, "task": group["Tasks"][0]}[scope]
            target["Constraints"] = [constraint]
            removed = copy.deepcopy(expected)
            removed_group = removed["TaskGroups"][0]
            {"job": removed, "group": removed_group, "task": removed_group["Tasks"][0]}[scope].pop("Constraints")
            with self.subTest(scope=scope, constraint=constraint):
                self.assertFalse(upgrade._matches_expected_job(removed, expected))
                self.assertFalse(upgrade._matches_expected_job(expected, removed))

    def test_default_looking_constraints_require_the_corresponding_feature(self):
        expected = workload(network="host")
        for constraint in (
            {"LTarget": "${attr.plugins.cni.version.bridge}", "RTarget": ">= 0.4.0", "Operand": "semver"},
            {"LTarget": "${attr.nomad.service_discovery}", "RTarget": "true", "Operand": "="},
            {"LTarget": "${attr.nomad.version}", "RTarget": ">= 1.4.0", "Operand": "semver"},
            {"LTarget": "${attr.plugins.secrets.nomad.version}", "RTarget": "", "Operand": "is_set"},
        ):
            changed = copy.deepcopy(expected)
            changed["TaskGroups"][0]["Constraints"] = [constraint]
            with self.subTest(constraint=constraint):
                self.assertFalse(upgrade._matches_expected_job(changed, expected))

    def test_unary_constraint_empty_target_is_a_nomad_default(self):
        expected = workload()
        expected["Constraints"] = [{"LTarget": "${meta.luma_region}", "Operand": "is_set"}]
        live = copy.deepcopy(expected)
        live["Constraints"][0]["RTarget"] = ""
        self.assertTrue(upgrade._matches_expected_job(live, expected))
        live["Constraints"][0]["Operand"] = "is_not_set"
        self.assertFalse(upgrade._matches_expected_job(live, expected))

    def test_egress_legacy_bind_is_verified_against_installed_configuration(self):
        from luma.nomad_render import render_egress_job

        expected = render_egress_job(image="example/mihomo:1", as_json=False)["Job"]
        legacy = copy.deepcopy(expected)
        cfg = legacy["TaskGroups"][0]["Tasks"][0]["Config"]
        cfg.pop("mount")
        cfg["volumes"] = ["/opt/luma/egress-gateway:/root/.config/mihomo"]
        config = mock.Mock(defaults={"images": {"egressGateway": "example/mihomo:1"}})
        with mock.patch.object(upgrade, "ADMIN_ROOT", self.admin), mock.patch("luma.config.load_config", return_value=config):
            self.assertTrue(upgrade._system_job(expected))
            self.assertTrue(upgrade._system_job(legacy))
            for key, value in (("volumes", ["/:/host"]), ("image", "unreviewed/mihomo"), ("args", ["sh", "-c", "sleep infinity"])):
                changed = copy.deepcopy(legacy)
                changed["TaskGroups"][0]["Tasks"][0]["Config"][key] = value
                with self.subTest(field=key):
                    self.assertFalse(upgrade._system_job(changed))

    def test_application_updates_preserve_authority_but_cannot_gain_host_root(self):
        self.enforce()
        job = workload(network="host", mounts=[{"type": "bind", "source": "/srv/luma/data/app", "target": "/data"}])
        security.authorize_application(job)
        updated = copy.deepcopy(job)
        updated["TaskGroups"][0]["Tasks"][0]["Config"].update(image="example/app:2", args=["app", "serve"])
        updated["TaskGroups"][0]["Tasks"][0]["Env"] = {"MODE": "new"}
        security.admit_job(updated)
        for mutate in (
            lambda j: j["TaskGroups"][0]["Tasks"][0]["Config"]["mount"][0].update(source="/"),
            lambda j: j["TaskGroups"][0]["Tasks"][0]["Config"].update(privileged=True),
            lambda j: j["TaskGroups"][0]["Tasks"][0]["Config"].update(cap_add=["SYS_ADMIN"]),
            lambda j: j.update(Constraints=[]),
        ):
            candidate = copy.deepcopy(updated)
            mutate(candidate)
            if candidate.get("Constraints") == []:
                # Removing the extra trusted-host constraint cannot be used to
                # schedule a mount on an isolated node. Admission reapplies it.
                security.prepare_job(candidate)
                self.assertIn("trusted", str(candidate["Constraints"]))
            else:
                with self.assertRaises(LumaError):
                    security.admit_job(candidate)

    def test_protected_subdirectories_and_resolved_symlinks_are_rejected(self):
        for source in ("/etc/luma/nomad", "/opt/luma/security/nomad/control.d", "/var/lib/docker/volumes", "/Users/example/.ssh"):
            with self.subTest(source=source), self.assertRaises(LumaError):
                security.validate_application(workload(mounts=[{"type": "bind", "source": source, "target": "/data"}]))
        link = self.root / "data"
        link.symlink_to("/etc")
        with self.assertRaises(LumaError):
            host.validate_mounts([{"type": "bind", "source": str(link), "target": "/data"}])

    def test_pki_is_idempotent_and_rejects_another_nodes_key_or_ca(self):
        pki.prepare_manager("100.64.0.1", admin_root=self.admin, service_root=self.runtime)
        before = (self.admin / "ca" / "key.pem").read_bytes()
        pki.prepare_manager("100.64.0.1", admin_root=self.admin, service_root=self.runtime)
        self.assertEqual(before, (self.admin / "ca" / "key.pem").read_bytes())
        node = self.root / "node"
        bundle = pki.sign_csr(pki.create_csr(node), role="client", address="100.64.0.2", admin_root=self.admin)
        pki.install_certificate(bundle, root=node)
        other = self.root / "other"
        pki.create_csr(other)
        with self.assertRaises(LumaError):
            pki.install_certificate(bundle, root=other)
        alien = pki.sign_csr(pki.create_csr(node), role="client", address="100.64.0.2", admin_root=self.root / "alien-ca")
        with self.assertRaises(LumaError):
            pki.install_certificate(alien, root=node)
        for role in security.ROLES:
            self.assertNotEqual(before, (self.runtime / (role + ".d") / "key.pem").read_bytes())
        self.assertFalse((self.admin / "ca" / "key.pem").stat().st_mode & 0o077)

    def test_tls_endpoint_cannot_fall_back_to_plaintext_when_active(self):
        security.private_write(self.runtime / "cluster.json", '{"tlsRequired":true}')
        self.assertEqual(pki.endpoint("http://100.64.0.2:4646"), "https://100.64.0.2:4646")
        self.assertEqual(pki.endpoint("http://127.0.0.1:8080"), "http://127.0.0.1:8080")

    def test_expired_node_can_request_renewal_without_contacting_nomad(self):
        node_tls = self.admin / "tls"
        pki.create_csr(node_tls)
        security.private_write(self.runtime / "node.json", json.dumps({"nodeId": "node-id", "address": "100.64.0.2"}))
        with mock.patch.object(host.os, "geteuid", side_effect=[0, os.getuid()]), mock.patch.object(host, "snapshot", side_effect=AssertionError("must not require expired Nomad TLS")), mock.patch.object(host, "create_csr", return_value="public-csr"), mock.patch.object(host, "expires_soon", return_value=True):
            result = host.execute({"schema": 1, "op": "certificate-request"})
        self.assertTrue(result["certificateRenewalRequired"])
        self.assertEqual(result["csr"], "public-csr")
        self.assertNotIn("key", result)

    def test_mac_vm_bind_paths_are_not_approved_using_the_mac_filesystem(self):
        with mock.patch.object(host, "detect_os", return_value="darwin"), mock.patch.object(host.os.path, "realpath") as resolved:
            with self.assertRaisesRegex(LumaError, "Docker VM"):
                host.validate_mounts([{"type": "bind", "source": "/srv/data", "target": "/data"}])
            resolved.assert_not_called()
        remote = mock.Mock()
        with mock.patch.object(host, "detect_os", return_value="darwin"), mock.patch.object(host, "LocalExecutor", return_value=remote):
            remote.run_result.return_value = LocalResult(0, json.dumps([{"Driver": "local", "Options": {}}]))
            host.validate_mounts([{"type": "volume", "source": "app_data", "target": "/data"}])
            remote.run_result.return_value = LocalResult(0, json.dumps([{"Driver": "local", "Options": {"type": "none", "device": "/etc", "o": "bind"}}]))
            with self.assertRaisesRegex(LumaError, "macOS VM"):
                host.validate_mounts([{"type": "volume", "source": "app_data", "target": "/data"}])


class AgentControlTrust(SecurityFixture):
    def test_legacy_agent_config_is_migrated_only_after_verified_https_succeeds(self):
        config = {"endpoint": "https://control.example", "token": "agent-only", "nodeName": "worker", "insecure": True, "resolveIp": "192.0.2.4"}
        path = self.root / "agent.json"
        security.private_write(path, json.dumps(config))
        client = mock.Mock()
        with mock.patch("luma.control.client.ControlClient", return_value=client) as factory:
            client.health.side_effect = LumaError("certificate verification failed")
            with self.assertRaisesRegex(LumaError, "verified Control HTTPS"):
                agent._verified_agent_control_client(config, path)
            self.assertTrue(security.read_private_json(path)["insecure"])
            client.lease_agent_task.assert_not_called()
            client.health.side_effect = None
            agent._verified_agent_control_client(config, path)
            self.assertFalse(security.read_private_json(path)["insecure"])
            self.assertEqual(security.read_private_json(path)["resolveIp"], "")
            for call in factory.call_args_list:
                self.assertIs(call.kwargs["insecure"], False)
                self.assertIsNone(call.kwargs["resolve_ip"])

    def test_old_agent_does_not_receive_staged_nomad_credentials(self):
        self.prepare()
        self.assertEqual(nomad_enrollment.agent_credential(capabilities=["docker-build"]), {})
        self.assertTrue(nomad_enrollment.agent_credential(capabilities=[host.CAPABILITY])["agentToken"])


class SimulatedCrash(BaseException):
    pass


class HostOperationRecovery(SecurityFixture):
    def setUp(self):
        super().setUp()
        self.config, self.binary = self.root / "nomad.hcl", self.root / "nomad"
        self.config.write_text('client { enabled = true }\n')
        self.binary.write_bytes(b"old-verified-binary")
        self.facts = {"nodeId": "node-client", "node": "worker", "address": "100.64.0.2", "os": "linux", "role": "client",
                      "region": "cn", "extraMeta": {}, "cpu": None, "version": security.NOMAD_VERSION,
                      "baselineSHA256": hashlib.sha256(self.config.read_bytes()).hexdigest(), "acl": False, "tlsHTTP": False,
                      "tlsRPC": False, "nodeStatus": "ready"}
        self.payload = {"schema": 1, "op": "configure", "nodeId": self.facts["nodeId"], "address": self.facts["address"],
                        "managerAddress": "100.64.0.1", "version": security.NOMAD_VERSION,
                        "baselineSHA256": self.facts["baselineSHA256"], "acl": True, "tlsHTTP": True,
                        "trustedHost": False, "certificate": {"ca": "public", "cert": "public"}}
        security.private_write(self.admin / "agent.token", "reader-only")
        host_os = mock.Mock(wraps=os)
        host_os.geteuid.return_value = 0
        patches = [mock.patch.object(host, "ADMIN_ROOT", self.admin), mock.patch.object(host, "CONFIG", self.config),
                   mock.patch.object(host, "BINARY", self.binary), mock.patch.object(host, "os", host_os),
                   mock.patch.object(host, "install_guard"), mock.patch.object(host, "install_certificate"),
                   mock.patch.object(host, "LocalExecutor", return_value=mock.Mock()),
                   mock.patch.object(host, "snapshot", return_value=self.facts),
                   mock.patch.object(host, "_wait", side_effect=lambda version, node_id: {**self.facts, "version": version, "nodeId": node_id, "acl": True, "tlsHTTP": True}),
                   mock.patch.object(host, "_restart")]
        self.mocks = {}
        for patch in patches:
            value = patch.start()
            self.addCleanup(patch.stop)
            self.mocks[patch.attribute] = value

    def test_config_and_transport_written_before_crash_resume_without_old_api(self):
        self.mocks["_restart"].side_effect = SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            host.execute(self.payload)
        self.assertTrue(security.read_private_json(self.runtime / "transport.json")["http"])
        self.mocks["snapshot"].side_effect = AssertionError("recovery must not query the unavailable old API")
        self.mocks["_restart"].side_effect = None
        result = host.execute(self.payload)
        self.assertTrue(result["tlsHTTP"])
        self.assertEqual(security.read_private_json(self.admin / "operation.json")["phase"], "completed")
        restart_count = self.mocks["_restart"].call_count
        self.assertEqual(host.execute(self.payload), result)
        self.assertEqual(self.mocks["_restart"].call_count, restart_count)

    def test_crash_after_intent_before_config_write_recovers_and_preserves_drift_guard(self):
        write = host.private_write
        def crash_before_config(path, content, **kwargs):
            if path == self.config:
                raise SimulatedCrash()
            return write(path, content, **kwargs)
        with mock.patch.object(host, "private_write", side_effect=crash_before_config), self.assertRaises(SimulatedCrash):
            host.execute(self.payload)
        self.assertEqual(hashlib.sha256(self.config.read_bytes()).hexdigest(), self.facts["baselineSHA256"])
        self.config.write_text("operator changed this config\n")
        self.mocks["snapshot"].side_effect = AssertionError("saved operation must check local state first")
        with self.assertRaisesRegex(LumaError, "configuration changed"):
            host.execute(self.payload)
        self.mocks["_restart"].assert_not_called()
        self.config.write_text('client { enabled = true }\n')
        host.execute(self.payload)
        self.mocks["_restart"].assert_called_once()

    def test_binary_replacement_before_crash_resumes_without_redownload_or_snapshot(self):
        self.facts["version"] = "1.9.7"
        payload = {**self.payload, "op": "version", "version": "1.10.5"}
        def download(version, directory, **kwargs):
            binary = directory / "nomad"
            binary.write_bytes(b"pinned-next-minor")
            return binary
        self.mocks["_restart"].side_effect = SimulatedCrash()
        with mock.patch.object(host, "_download", side_effect=download) as fetch:
            with self.assertRaises(SimulatedCrash):
                host.execute(payload)
            self.assertEqual(self.binary.read_bytes(), b"pinned-next-minor")
            self.mocks["snapshot"].side_effect = AssertionError("new process must recover before API inspection")
            self.mocks["_restart"].side_effect = None
            self.assertEqual(host.execute(payload)["version"], "1.10.5")
            fetch.assert_called_once()


class UpgradeWorkflow(SecurityFixture):
    def setUp(self):
        super().setUp()
        self.models = {}
        self.state = {"deployToken": "management", "nodes": {}}
        for index, name in enumerate(("manager", "linux", "mac"), 1):
            self.models[name] = {"schema": 1, "nodeId": f"node-{index}", "node": name, "nomadName": name, "address": f"100.64.0.{index}",
                                 "os": "darwin" if name == "mac" else "linux", "role": "server" if name == "manager" else "client",
                                 "region": "home" if name == "mac" else "cn", "extraMeta": {}, "cpu": 30000 if name == "mac" else None,
                                 "version": "1.9.7", "baselineSHA256": "initial", "acl": False, "tlsHTTP": False, "tlsRPC": False,
                                 "rpcUpgradeMode": False, "nodeStatus": "ready", "csr": "public-csr"}
            self.state["nodes"][name] = {"nodeId": f"node-{index}", "tailscaleIP": f"100.64.0.{index}", "agent": {"capabilities": [host.CAPABILITY], "lastSeen": int(time.time())}}
        self.journal = self.admin / "upgrade.json"
        self.calls = []
        self.tasks = {}
        self.lost_once = False
        self.fail_node = ""
        self.original_inventory = upgrade.inventory
        security.private_write(self.runtime / "cluster.json", '{"schema":1,"aclRequired":true}')
        security.private_write(self.journal, json.dumps({"schema": 1, "id": "nomad-security-test", "manager": "manager", "managerId": "node-1", "attempt": 0, "nodes": {"manager": self.models["manager"]}, "next": 0, "status": "pending"}))
        patches = [
            mock.patch.object(upgrade, "JOURNAL", self.journal), mock.patch.object(upgrade, "ADMIN_ROOT", self.admin),
            mock.patch.object(upgrade, "_require_single_server"), mock.patch.object(upgrade, "resume_operation", return_value=None),
            mock.patch.object(upgrade, "_state", side_effect=lambda: copy.deepcopy(self.state)),
            mock.patch.object(upgrade, "snapshot", side_effect=lambda: copy.deepcopy(self.models["manager"])),
            mock.patch.object(upgrade, "inventory", side_effect=lambda _state, nodes: ([], {name: {"mounts": [], "systemMounts": [], "trustedHost": name == "manager"} for name in nodes})),
            mock.patch.object(upgrade, "execute", side_effect=lambda payload, **kwargs: self.execute("manager", payload)),
            mock.patch.object(upgrade, "control_task", side_effect=self.task),
            mock.patch.object(upgrade, "sign_csr", return_value={"ca": "public-ca", "cert": "public-cert"}),
            mock.patch.object(upgrade, "_raft_snapshot", side_effect=lambda label: self.calls.append(("snapshot", label))),
            mock.patch.object(upgrade, "activate_acl", side_effect=lambda: self.calls.append(("acl", "activated"))),
            mock.patch.object(upgrade, "_consumers", side_effect=lambda journal: self.calls.append(("consumers", "verified"))),
            mock.patch.object(secret_migration, "migrate", side_effect=self.secrets),
            mock.patch.object(upgrade, "verify_acl", return_value={"ok": True}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def secrets(self, journal, state):
        self.assertTrue(all(node["acl"] and node["tlsHTTP"] for node in self.models.values()))
        self.assertEqual(self.models["manager"]["jobTrackedVersions"], 1)
        journal["secretMigration"] = {"verifiedAt": int(time.time()), "databaseEncrypted": True}
        self.calls.append(("secrets", "verified"))
        return {"encryptedStorage": True, "taskIdentitySecrets": True}

    def execute(self, name, payload):
        operation = payload["op"]
        node = self.models[name]
        self.calls.append((name, operation, payload.get("version"), payload.get("acl")))
        if operation == "version":
            self.assertEqual(host.version_path(node["version"])[0], payload["version"])
            node["version"] = payload["version"]
        if operation == "configure":
            self.assertEqual(payload["baselineSHA256"], node["baselineSHA256"])
            node.update(acl=payload["acl"], tlsHTTP=payload["tlsHTTP"], tlsRPC=True, rpcUpgradeMode=bool(payload.get("rpcUpgradeMode")))
            node.update(isolatedHostEnvironment=True, jobTrackedVersions=1 if payload.get("secretMigration") else 6)
            node["baselineSHA256"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            if name == "manager":
                security.private_write(self.runtime / "transport.json", json.dumps({"http": payload["tlsHTTP"], "address": node["address"]}))
        if operation == "verify":
            self.assertTrue(node["acl"] and node["tlsHTTP"] and node["tlsRPC"])
            self.assertFalse(node["rpcUpgradeMode"])
            return {"nodeId": node["nodeId"], "version": node["version"], "checks": {"ok": True}, "verifiedAt": int(time.time())}
        return copy.deepcopy(node)

    def task(self, state, name, key, payload):
        if key not in self.tasks:
            self.tasks[key] = {"payload": copy.deepcopy(payload), "status": "queued"}
            return {"status": "queued"}
        task = self.tasks[key]
        self.assertEqual(task["payload"], payload, "a lost response must reuse the persisted payload")
        if task["status"] == "queued":
            if self.fail_node == name and payload["op"] == "configure":
                task.update(status="failed", message="unsafe host configuration")
            else:
                task.update(status="succeeded", result=self.execute(name, payload))
            if payload["op"] == "version" and name == "linux" and not self.lost_once:
                self.lost_once = True
                raise LumaError("Control is unavailable during the security upgrade; the saved step will resume")
        return copy.deepcopy(task)

    def complete(self):
        # Each iteration reloads the journal, like a process restart after
        # every step, including the deliberately lost task response.
        for _ in range(140):
            status = upgrade.reconcile_once()
            if status["status"] in {"completed", "failed"}:
                return status
        self.fail("automatic security migration did not terminate")

    def test_manager_then_all_nodes_upgrade_automatically_and_resume_lost_responses(self):
        result = self.complete()
        self.assertEqual(result["status"], "completed", result.get("message"))
        self.assertTrue(self.lost_once)
        for version in security.NOMAD_RELEASES:
            migrated = [call[0] for call in self.calls if len(call) == 4 and call[1:3] == ("version", version)]
            self.assertEqual(migrated, ["manager", "linux", "mac"])
        activated = self.calls.index(("acl", "activated"))
        for index, call in enumerate(self.calls):
            if len(call) == 4 and call[0] != "manager" and call[1] == "configure" and call[3]:
                self.assertGreater(index, activated)
        self.assertTrue(security.read_private_json(self.runtime / "cluster.json")["tlsRequired"])
        self.assertTrue(all(node["verified"] for node in result["nodes"]))
        self.assertTrue(result["secrets"]["taskIdentitiesVerified"])
        self.assertEqual(self.models["manager"]["jobTrackedVersions"], 6)

    def test_old_or_offline_agent_keeps_upgrade_pending_without_changing_nomad(self):
        self.state["nodes"]["mac"]["agent"]["capabilities"] = []
        result = upgrade.reconcile_once()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["waitingNodes"], ["mac"])
        self.assertEqual(self.calls, [])

    def test_failed_node_never_reports_all_security_checks_passed(self):
        self.fail_node = "mac"
        result = self.complete()
        self.assertEqual(result["status"], "failed")
        self.assertIn("unsafe host configuration", result["message"])
        self.assertNotIn("verifiedAt", result)
        self.assertNotIn(("acl", "activated"), self.calls)

    def test_new_install_needs_verification_without_a_legacy_migration(self):
        nodes = {name: {**value, "version": security.NOMAD_VERSION, "acl": True, "tlsHTTP": True, "tlsRPC": True} for name, value in self.models.items()}
        steps = upgrade.migration_steps(nodes, "manager")
        self.assertFalse(any(step["kind"] in {"version", "rpc", "activate", "secure"} for step in steps))
        self.assertEqual(sum(step["kind"] == "verify" for step in steps), len(nodes))

    def test_unknown_live_workload_is_not_approved_by_name_or_metadata(self):
        bad = workload(name="hostshell", mounts=[{"type": "bind", "source": "/", "target": "/host"}])
        bad["Meta"] = {"luma.managed": "true"}
        client = mock.Mock()
        client.request.side_effect = lambda method, path: [] if path == "/v1/allocations" else [{"ID": "hostshell", "Status": "running"}] if path == "/v1/jobs?namespace=*" else bad
        with mock.patch.object(upgrade, "_source_jobs", return_value={}), mock.patch.object(upgrade, "NomadApi", return_value=client), mock.patch.object(upgrade, "nomad_token", return_value="reader"):
            with self.assertRaisesRegex(LumaError, "does not match a saved Luma deployment"):
                self.original_inventory(self.state, self.models)
        with self.assertRaises(LumaError):
            host.version_path("2.2.0")


class ConsumerRecovery(SecurityFixture):
    def setUp(self):
        super().setUp()
        self.prepare()
        pki.prepare_manager("100.64.0.1", admin_root=self.admin, service_root=self.runtime)
        security.private_write(self.runtime / "transport.json", '{"http":true}')
        job = workload(name="traefik", image="traefik:v3.6", network="host",
                       mounts=[{"type": "bind", "source": "/opt/luma/routes", "target": "/dynamic", "readonly": True}])
        task = job["TaskGroups"][0]["Tasks"][0]
        task["Name"] = "traefik"
        task["Config"]["args"] = ["--providers.nomad.endpoint.address=http://127.0.0.1:4646"]
        job.update(JobModifyIndex=10, Version=1)
        self.live = copy.deepcopy(job)
        self.path = self.admin / "upgrade.json"
        self.journal = {"jobs": [{"Job": job, "system": True, "authoritySHA256": security.authority_digest(job)}]}
        security.private_write(self.path, json.dumps(self.journal))
        security.private_write(self.admin / "system-jobs" / "traefik.json", json.dumps({"Job": job}))
        self.submissions = 0
        self.lose_response = False
        self.healthy = True
        client = mock.Mock()
        client.request.side_effect = self.request
        for patch in (mock.patch.object(upgrade, "JOURNAL", self.path), mock.patch.object(upgrade, "ADMIN_ROOT", self.admin),
                      mock.patch.object(upgrade, "NomadApi", return_value=client)):
            patch.start()
            self.addCleanup(patch.stop)

    def request(self, method, path, body=None):
        if method == "POST":
            self.assertTrue(body["EnforceIndex"])
            self.assertEqual(body["JobModifyIndex"], self.live["JobModifyIndex"])
            self.submissions += 1
            self.live = {**copy.deepcopy(body["Job"]), "JobModifyIndex": self.live["JobModifyIndex"] + 1, "Version": self.live["Version"] + 1}
            if self.lose_response:
                self.lose_response = False
                raise LumaError("Nomad API unavailable: response lost after commit")
            return {"JobModifyIndex": self.live["JobModifyIndex"]}
        if path.endswith("/allocations"):
            return [{"JobVersion": self.live["Version"], "ClientStatus": "running", "TaskStates": {"traefik": {"Failed": False}}}] if self.healthy else []
        return copy.deepcopy(self.live)

    def test_lost_submission_response_recovers_from_persisted_prior_and_target(self):
        self.lose_response = True
        with self.assertRaisesRegex(LumaError, "response lost"):
            upgrade._consumers(self.journal)
        saved = security.read_private_json(self.path)
        self.assertIn("consumerIntent", saved["jobs"][0])
        self.assertFalse(upgrade._system_job(self.live), "trusted installed spec must not move before verifying the new revision")
        upgrade._consumers(saved)
        self.assertEqual(self.submissions, 1)
        self.assertTrue(upgrade._system_job(self.live))
        self.assertNotIn("consumerIntent", security.read_private_json(self.path)["jobs"][0])

    def test_pending_health_does_not_repeatedly_restart_a_secured_consumer(self):
        self.healthy = False
        with self.assertRaises(upgrade.Pending):
            upgrade._consumers(self.journal)
        self.healthy = True
        upgrade._consumers(security.read_private_json(self.path))
        self.assertEqual(self.submissions, 1)

    def test_resume_rejects_live_command_changes_outside_both_saved_revisions(self):
        self.lose_response = True
        with self.assertRaises(LumaError):
            upgrade._consumers(self.journal)
        self.live["TaskGroups"][0]["Tasks"][0]["Config"]["image"] = "unexpected/image"
        with self.assertRaisesRegex(LumaError, "outside its saved upgrade intent"):
            upgrade._consumers(security.read_private_json(self.path))
        self.assertEqual(self.submissions, 1)

    def test_empty_cluster_still_activates_job_admission(self):
        candidate = workload(network="host")
        security.admit_job(candidate)
        upgrade._consumers({"jobs": []})
        with self.assertRaisesRegex(LumaError, "requires a reviewed security grant"):
            security.admit_job(candidate)


class UpgradeTopology(SecurityFixture):
    def test_single_server_requirement_is_verified_from_raft_membership(self):
        client = mock.Mock()
        with mock.patch.object(upgrade, "NomadApi", return_value=client), mock.patch.object(upgrade, "nomad_token", return_value="read-only"):
            for servers in ([], [{"Leader": True, "Voter": True}, {"Leader": False, "Voter": True}], [{"Leader": False, "Voter": True}]):
                client.request.return_value = {"Servers": servers}
                with self.assertRaisesRegex(LumaError, "exactly one healthy Raft server"):
                    upgrade._require_single_server()
            client.request.return_value = {"Servers": [{"Leader": True, "Voter": True}]}
            upgrade._require_single_server()

    def test_certificate_refresh_keeps_consumer_validation_pending_after_new_files_exist(self):
        journal = {"nodes": {"manager": {"address": "100.64.0.1"}}, "manager": "manager", "jobs": []}
        path = self.admin / "upgrade.json"
        with mock.patch.object(upgrade, "JOURNAL", path), mock.patch.object(upgrade, "expires_soon", return_value=True) as expiry, \
                mock.patch.object(upgrade, "prepare_manager"), mock.patch.object(upgrade, "LocalExecutor"), \
                mock.patch.object(upgrade, "inventory", return_value=([], {})), \
                mock.patch.object(upgrade, "_consumers", side_effect=[upgrade.Pending("waiting for new allocations"), None]) as consumers:
            with self.assertRaises(upgrade.Pending):
                upgrade._maintain_certificates(journal, {})
            self.assertTrue(security.read_private_json(path)["consumerRenewalPending"])
            expiry.return_value = False
            upgrade._maintain_certificates(security.read_private_json(path), {})
            self.assertEqual(consumers.call_count, 2)
            self.assertNotIn("consumerRenewalPending", security.read_private_json(path))


@unittest.skipUnless(os.environ.get("LUMA_TEST_NOMAD_BINARY"), "set LUMA_TEST_NOMAD_BINARY for the isolated real Nomad TLS protocol test")
class NomadTLSProtocol(SecurityFixture):
    def setUp(self):
        super().setUp()
        self.prepare()
        pki.prepare_manager("100.64.0.1", admin_root=self.admin, service_root=self.runtime)
        self.tls = pki.tls_paths(self.runtime / "control.d")
        ports = nomad_test_ports()
        config = self.root / "nomad.hcl"
        paths = pki.tls_paths(self.admin / "tls")
        rendered = nomad_node.render_agent_config(os_name="linux", role="server", tailscale_ip="100.64.0.1", region="cn", node_name="isolated-test-server", client_enabled=False, tls=paths)
        rendered = rendered.replace('"/opt/nomad/data"', json.dumps(str(self.root / "data"))).replace('"127.0.0.1 100.64.0.1"', '"127.0.0.1"').replace('"100.64.0.1"', '"127.0.0.1"')
        config.write_text(rendered + f'\nports {{ http = {ports[0]} rpc = {ports[1]} serf = {ports[2]} }}\n')
        log = open(self.root / "nomad.log", "w")
        process = subprocess.Popen([os.environ["LUMA_TEST_NOMAD_BINARY"], "agent", "-config", str(config)], stdout=log, stderr=subprocess.STDOUT,
                                   env={key: value for key, value in os.environ.items() if not key.startswith("NOMAD_")})
        def close():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()
        self.addCleanup(close)
        self.address = f"https://127.0.0.1:{ports[0]}"
        self.factory = lambda address, token="": NomadApi(address, token=token, tls=self.tls, managed_transport=False)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                if self.factory(self.address).request("GET", "/v1/status/leader"):
                    break
            except LumaError:
                pass
            time.sleep(0.2)
        if process.poll() is not None:
            self.fail("isolated TLS Nomad server failed: " + (self.root / "nomad.log").read_text()[-2000:])

    def test_first_install_requires_both_client_certificate_and_scoped_acl_token(self):
        result = nomad_acl.activate_acl(address=self.address, admin_root=self.admin, service_root=self.runtime, api_factory=self.factory)
        self.assertTrue(result["verification"]["ok"])
        token = security.private_read(security.service_token_path(self.runtime, "control")).strip()
        self.factory(self.address, token).request("GET", "/v1/jobs")
        live = self.factory(self.address, token).request("GET", "/v1/agent/self")["config"]
        self.assertEqual(live["Addresses"]["HTTP"], "127.0.0.1")
        self.assertEqual(live["Client"]["Options"]["driver.allowlist"], "docker")
        self.assertEqual(live["Server"]["ClientIntroduction"]["Enforcement"], "strict")
        self.assertTrue(live["TLSConfig"]["VerifyHTTPSClient"])
        self.assertTrue(live["TLSConfig"]["VerifyServerHostname"])
        plugin = next(item["Config"] for item in live["Plugins"] if item["Name"] == "docker")
        self.assertIs(plugin["allow_privileged"], False)
        volumes = plugin["volumes"]
        self.assertFalse((volumes[0] if isinstance(volumes, list) else volumes)["enabled"])
        with self.assertRaises(LumaError):
            NomadApi(self.address, token=token, tls={"ca_file": self.tls["ca_file"]}, managed_transport=False).request("GET", "/v1/jobs")
        with self.assertRaisesRegex(LumaError, "403"):
            self.factory(self.address).request("GET", "/v1/jobs")
        # Count zero stores a real job revision without running any allocation.
        # Verify anonymous denial of both current and historical runtime Env.
        job = workload(name="private-environment")
        job["TaskGroups"][0]["Count"] = 0
        job["TaskGroups"][0]["Tasks"][0]["Env"] = {"LUMA_PROTOCOL_TEST_SECRET": "nonproduction-test-value"}
        self.factory(self.address, token).request("POST", "/v1/jobs", {"Job": job})
        detail = self.factory(self.address, token).request("GET", "/v1/job/private-environment")
        self.assertEqual(detail["TaskGroups"][0]["Tasks"][0]["Env"], job["TaskGroups"][0]["Tasks"][0]["Env"])
        for path in ("/v1/job/private-environment", "/v1/job/private-environment/versions"):
            with self.subTest(path=path), self.assertRaisesRegex(LumaError, "403"):
                self.factory(self.address).request("GET", path)
        identity = self.factory(self.address, token).request("PUT", "/v1/acl/identity/client-introduction-token", {"NodeName": "new-worker", "NodePool": "default", "TTL": 600_000_000_000})
        self.assertTrue(identity.get("JWT"))
        readonly = security.private_read(security.service_token_path(self.runtime, "agent")).strip()
        with self.assertRaisesRegex(LumaError, "403"):
            self.factory(self.address, readonly).request("PUT", "/v1/acl/identity/client-introduction-token", {"NodeName": "other-worker", "NodePool": "default", "TTL": 600_000_000_000})
        self.assertTrue(nomad_acl.activate_acl(address=self.address, admin_root=self.admin, service_root=self.runtime, api_factory=self.factory)["verification"]["ok"])


if __name__ == "__main__":
    unittest.main()
