from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from luma.cli import build_parser
from luma.control import server as control_server
from luma.errors import LumaError
from luma.registry_management import (
    RegistryResponse,
    apply_protection,
    collect_state_image_references,
    managed_image_reference,
    normalize_policy,
    scan_registry,
    validate_digest,
    validate_repository,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
CONFIG_A = "sha256:" + "c" * 64
CONFIG_B = "sha256:" + "d" * 64
LAYER_A = "sha256:" + "e" * 64
LAYER_B = "sha256:" + "f" * 64


class FakeRegistryClient:
    def catalog(self) -> list[str]:
        return ["acme/api"]

    def tags(self, repository: str) -> list[str]:
        self._require_repository(repository)
        return ["current", "latest", "old"]

    def manifest_head(self, repository: str, tag: str) -> dict[str, object]:
        self._require_repository(repository)
        digest = DIGEST_A if tag == "old" else DIGEST_B
        return {
            "digest": digest,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "contentLength": 512,
            "lastModified": 1_700_000_000 if digest == DIGEST_A else 1_710_000_000,
        }

    def manifest(self, repository: str, digest: str):
        self._require_repository(repository)
        config = CONFIG_A if digest == DIGEST_A else CONFIG_B
        layer = LAYER_A if digest == DIGEST_A else LAYER_B
        payload = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": config, "size": 100},
            "layers": [{"digest": layer, "size": 900}],
        }
        return payload, RegistryResponse(
            status=200,
            headers={"Content-Type": payload["mediaType"]},
            body=json.dumps(payload).encode(),
        )

    def blob_json(self, repository: str, digest: str) -> dict[str, str]:
        self._require_repository(repository)
        return {
            "created": "2023-01-01T00:00:00Z" if digest == CONFIG_A else "2024-03-01T00:00:00Z",
            "os": "linux",
            "architecture": "amd64",
        }

    @staticmethod
    def _require_repository(repository: str) -> None:
        if repository != "acme/api":
            raise AssertionError(repository)


class RegistryInventoryTests(unittest.TestCase):
    def test_validators_and_managed_references_are_strict(self) -> None:
        self.assertEqual(validate_repository("Acme/API"), "acme/api")
        self.assertEqual(validate_digest(DIGEST_A), DIGEST_A)
        self.assertEqual(
            managed_image_reference(f"registry.internal/acme/api@{DIGEST_A}", "registry.internal")["digest"],
            DIGEST_A,
        )
        self.assertEqual(
            managed_image_reference("registry.internal/acme/api:release", "registry.internal")["tag"],
            "release",
        )
        self.assertIsNone(managed_image_reference("docker.io/acme/api:release", "registry.internal"))
        with self.assertRaises(LumaError):
            validate_repository("../escape")
        with self.assertRaises(LumaError):
            validate_digest("sha256:not-a-digest")

    def test_scan_groups_alias_tags_by_manifest_digest(self) -> None:
        inventory = scan_registry(FakeRegistryClient(), workers=2)

        self.assertEqual(inventory["summary"]["repositoryCount"], 1)
        self.assertEqual(inventory["summary"]["tagCount"], 3)
        self.assertEqual(inventory["summary"]["manifestCount"], 2)
        by_digest = {entry["digest"]: entry for entry in inventory["entries"]}
        self.assertEqual(by_digest[DIGEST_B]["tags"], ["current", "latest"])
        self.assertEqual(by_digest[DIGEST_A]["logicalBytes"], 1000)
        self.assertEqual(by_digest[DIGEST_B]["platforms"], ["linux/amd64"])

    def test_protection_resolves_tags_and_fails_closed(self) -> None:
        inventory = scan_registry(FakeRegistryClient(), workers=2)
        references = [
            {
                "repository": "acme/api",
                "tag": "current",
                "digest": "",
                "kind": "nomad-version",
                "source": "nomad:api:v4",
                "reference": "registry.internal/acme/api:current",
            }
        ]
        policy = {"keepLast": 1, "maxAgeDays": 1}
        protected = apply_protection(inventory, references, complete=True, policy=policy, now=1_800_000_000)
        by_digest = {entry["digest"]: entry for entry in protected["entries"]}
        self.assertEqual(by_digest[DIGEST_B]["protectionStatus"], "protected")
        self.assertEqual(by_digest[DIGEST_A]["protectionStatus"], "candidate")
        self.assertTrue(by_digest[DIGEST_A]["deletable"])

        incomplete = apply_protection(inventory, references, complete=False, policy=policy, now=1_800_000_000)
        self.assertTrue(all(entry["protectionStatus"] == "unknown" for entry in incomplete["entries"]))
        self.assertTrue(all(not entry["deletable"] for entry in incomplete["entries"]))

    def test_platform_digest_reference_protects_its_parent_index(self) -> None:
        inventory = {
            "summary": {},
            "entries": [
                {
                    "repository": "acme/api",
                    "digest": DIGEST_A,
                    "tags": ["multiarch"],
                    "createdAt": 1,
                    "childManifestDigests": [DIGEST_B],
                }
            ],
        }
        references = [
            {
                "repository": "acme/api",
                "digest": DIGEST_B,
                "tag": "",
                "kind": "nomad-version",
                "source": "nomad:api:v2",
            }
        ]
        result = apply_protection(
            inventory,
            references,
            complete=True,
            policy={"keepLast": 1, "maxAgeDays": 1},
            now=1_800_000_000,
        )
        self.assertEqual(result["entries"][0]["protectionStatus"], "protected")

    def test_state_references_cover_deployments_builds_runtime_and_tasks(self) -> None:
        state = {
            "deployments": {
                "services": {
                    "api": {"manifest": f"image: registry.internal/acme/api@{DIGEST_A}\n"},
                },
                "compose": {},
            },
            "buildRuns": {
                "run-1": {"status": "succeeded", "result": {"image": "registry.internal/acme/api:build"}},
            },
            "laeRuntime": {
                "deployments": {"runtime-1": {"status": "ready", "images": ["registry.internal/acme/api:runtime"]}},
            },
            "agentTasks": {
                "task-1": {"status": "running", "payload": {"image": "registry.internal/acme/api:task"}},
            },
        }
        references = collect_state_image_references(state, "registry.internal")
        self.assertEqual({item["kind"] for item in references}, {"deployment", "build", "lae", "agent-task"})

    def test_policy_bounds_and_threshold_order(self) -> None:
        self.assertEqual(normalize_policy({})["mode"], "recommend")
        with self.assertRaises(LumaError):
            normalize_policy({"keepLast": 0})

    def test_off_policy_never_marks_automatic_candidates(self) -> None:
        inventory = scan_registry(FakeRegistryClient(), workers=2)
        result = apply_protection(
            inventory,
            [],
            complete=True,
            policy={"mode": "off", "keepLast": 1, "maxAgeDays": 1},
            now=1_800_000_000,
        )
        self.assertEqual(result["summary"]["candidateCount"], 0)
        self.assertTrue(all(not entry["deletable"] for entry in result["entries"]))


class RegistryControlTests(unittest.TestCase):
    def test_delete_preview_blocks_any_protected_alias(self) -> None:
        inventory = {
            "protectionComplete": True,
            "entries": [
                {
                    "repository": "acme/api",
                    "digest": DIGEST_A,
                    "tags": ["latest", "stable"],
                    "logicalBytes": 1000,
                    "protectionStatus": "protected",
                    "protectionReasons": [{"kind": "deployment", "source": "service:api"}],
                }
            ],
        }
        preview = control_server._registry_deletion_preview_from_inventory(
            inventory,
            [{"repository": "acme/api", "digest": DIGEST_A}],
        )
        self.assertFalse(preview["allowed"])
        self.assertIn("referenced", preview["blocked"][0]["reason"])

        manual = control_server._registry_deletion_preview_from_inventory(
            inventory,
            [{"repository": "acme/api", "digest": DIGEST_A}],
            manual_override=True,
        )
        self.assertTrue(manual["allowed"])
        self.assertEqual(manual["blocked"], [])
        self.assertIn("referenced", manual["risks"][0]["reason"])

    def test_manual_delete_remains_available_when_reference_scan_is_incomplete(self) -> None:
        inventory = {
            "protectionComplete": False,
            "entries": [
                {
                    "repository": "acme/api",
                    "digest": DIGEST_A,
                    "tags": ["old"],
                    "logicalBytes": 1000,
                    "protectionStatus": "unknown",
                    "protectionReasons": [],
                }
            ],
        }
        automatic = control_server._registry_deletion_preview_from_inventory(
            inventory, [{"repository": "acme/api", "digest": DIGEST_A}]
        )
        manual = control_server._registry_deletion_preview_from_inventory(
            inventory,
            [{"repository": "acme/api", "digest": DIGEST_A}],
            manual_override=True,
        )
        self.assertFalse(automatic["allowed"])
        self.assertTrue(manual["allowed"])
        self.assertEqual(manual["risks"][0]["reason"], "reference scan is incomplete")

    def test_delete_preview_includes_only_exclusive_platform_manifests(self) -> None:
        inventory = {
            "protectionComplete": True,
            "entries": [
                {
                    "repository": "acme/api",
                    "digest": DIGEST_A,
                    "tags": ["old"],
                    "logicalBytes": 1000,
                    "protectionStatus": "candidate",
                    "protectionReasons": [],
                    "childManifestDigests": [CONFIG_A, CONFIG_B],
                },
                {
                    "repository": "acme/api",
                    "digest": DIGEST_B,
                    "tags": ["current"],
                    "logicalBytes": 1000,
                    "protectionStatus": "retained",
                    "protectionReasons": [],
                    "childManifestDigests": [CONFIG_B],
                },
            ],
        }
        preview = control_server._registry_deletion_preview_from_inventory(
            inventory,
            [{"repository": "acme/api", "digest": DIGEST_A}],
        )
        self.assertTrue(preview["allowed"])
        self.assertEqual(
            [(item["repository"], item["digest"]) for item in preview["dependentManifests"]],
            [("acme/api", CONFIG_A)],
        )

    def test_nomad_job_restore_drops_server_owned_fields(self) -> None:
        raw = {
            "ID": "luma-registry",
            "Stop": True,
            "Status": "dead",
            "Version": 8,
            "CreateIndex": 1,
            "ModifyIndex": 2,
            "JobModifyIndex": 3,
            "TaskGroups": [],
        }
        restored = control_server._registry_restorable_nomad_job(raw)
        self.assertFalse(restored["Stop"])
        self.assertNotIn("Status", restored)
        self.assertNotIn("Version", restored)
        self.assertEqual(restored["TaskGroups"], [])
        self.assertTrue(raw["Stop"])

    def test_dashboard_registry_alert_uses_saved_scan_and_policy(self) -> None:
        state = {
            "registryManagement": {
                "policy": {"warningPercent": 70, "criticalPercent": 85, "emergencyPercent": 95},
                "lastScan": {"usage": {"filesystemUsePercent": 88, "filesystemAvailableBytes": 123}},
            }
        }
        issues = control_server._registry_dashboard_issues(state)
        self.assertEqual(issues[0]["severity"], "critical")
        self.assertEqual(issues[0]["kind"], "registry-storage")

    @patch.object(control_server, "_record_registry_automation")
    @patch.object(control_server, "_registry_inventory_for_state")
    @patch.object(control_server, "load_state")
    def test_recommend_automation_refreshes_but_never_deletes(self, load_state, inventory, record) -> None:
        state = {"deployToken": "token", "registryManagement": {"policy": {"mode": "recommend"}}}
        load_state.return_value = copy.deepcopy(state)
        inventory.return_value = {"protectionComplete": True, "entries": []}
        with patch.object(control_server, "handle_registry_deletion_create") as create:
            control_server._registry_automation_tick()
        create.assert_not_called()
        record.assert_called_once()

    @patch.object(control_server, "_record_registry_automation")
    @patch.object(control_server, "handle_registry_deletion_create")
    @patch.object(control_server, "_registry_inventory_for_state")
    @patch.object(control_server, "load_state")
    def test_enforce_automation_queues_only_candidates(self, load_state, inventory, create, record) -> None:
        state = {
            "deployToken": "token",
            "registryManagement": {"policy": {"mode": "enforce"}, "deletions": [], "audit": []},
        }
        load_state.return_value = copy.deepcopy(state)
        inventory.return_value = {
            "protectionComplete": True,
            "deletions": [],
            "entries": [
                {"repository": "acme/api", "digest": DIGEST_A, "protectionStatus": "candidate"},
                {"repository": "acme/api", "digest": DIGEST_B, "protectionStatus": "protected"},
            ],
        }
        control_server._registry_automation_tick()
        request = create.call_args.args[1]
        self.assertEqual(request["manifests"], [{"repository": "acme/api", "digest": DIGEST_A}])
        self.assertEqual(request["confirm"], "delete")
        record.assert_called_once()

    def test_cli_exposes_registry_management_commands_without_overloading_credentials(self) -> None:
        parser = build_parser()
        images = parser.parse_args(["registry", "images", "--refresh"])
        deletion = parser.parse_args(["registry", "deletion", "delete-1", "restore"])
        self.assertEqual(images.registry_command, "images")
        self.assertTrue(images.refresh)
        self.assertEqual(deletion.registry_command, "deletion")
        self.assertEqual(deletion.action, "restore")


if __name__ == "__main__":
    unittest.main()
