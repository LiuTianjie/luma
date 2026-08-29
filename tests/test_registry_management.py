from __future__ import annotations

import copy
import gzip
import json
import unittest
from types import SimpleNamespace
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
        policy = normalize_policy({})
        self.assertEqual(policy["mode"], "recommend")
        self.assertEqual(policy["queueGraceHours"], 0)
        self.assertEqual(normalize_policy({"queueGraceHours": 0})["queueGraceHours"], 0)
        with self.assertRaises(LumaError):
            normalize_policy({"keepLast": 0})
        with self.assertRaises(LumaError):
            normalize_policy({"queueGraceHours": -1})

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
    def tearDown(self) -> None:
        control_server._REGISTRY_SCAN_CACHE.clear()
        control_server._REGISTRY_BACKGROUND_SCAN_THREADS.clear()

    @patch.object(control_server.time, "time", return_value=1_800_000_000)
    def test_zero_grace_policy_unlocks_existing_queued_deletions(self, _time) -> None:
        state = {
            "deployToken": "token",
            "registryManagement": {
                "policy": {"mode": "recommend", "queueGraceHours": 24},
                "deletions": [
                    {"id": "delete-1", "status": "queued", "notBefore": 1_800_086_400, "updatedAt": 1},
                    {"id": "delete-2", "status": "deleted_pending_gc", "notBefore": 1_800_086_400, "updatedAt": 1},
                ],
                "audit": [],
            },
        }

        def mutate(callback):
            callback(state)

        with patch.object(control_server, "_mutate_control_state", side_effect=mutate):
            result = control_server.handle_registry_policy_set(
                "token", {"mode": "recommend", "queueGraceHours": 0}
            )

        self.assertEqual(result["policy"]["queueGraceHours"], 0)
        self.assertEqual(state["registryManagement"]["deletions"][0]["notBefore"], 1_800_000_000)
        self.assertEqual(state["registryManagement"]["deletions"][0]["updatedAt"], 1_800_000_000)
        self.assertEqual(state["registryManagement"]["deletions"][1]["notBefore"], 1_800_086_400)
        self.assertEqual(state["registryManagement"]["audit"][-1]["rescheduledDeletions"], 1)

    @patch.object(control_server, "_load_registry_scan_snapshot")
    @patch.object(control_server, "_refresh_registry_inventory")
    @patch.object(control_server, "_managed_registry_spec")
    def test_page_inventory_uses_durable_snapshot_without_rescanning(
        self, managed_spec, refresh_inventory, load_snapshot
    ) -> None:
        managed_spec.return_value = {
            "host": "registry.internal",
            "node": "builder",
            "volumeName": "registry-data",
            "jobId": "registry",
        }
        load_snapshot.return_value = {
            "summary": {"repositoryCount": 12},
            "entries": [],
            "protectionComplete": True,
            "scanPending": False,
        }
        state = {"registryManagement": {"policy": {"mode": "recommend"}, "deletions": [], "audit": []}}
        result = control_server._registry_inventory_for_state(state, refresh=False)
        self.assertEqual(result["summary"]["repositoryCount"], 12)
        self.assertFalse(result["scanPending"])
        refresh_inventory.assert_not_called()

    def test_dashboard_inventory_drops_blob_lists_and_uses_gzip(self) -> None:
        result = {
            "entries": [
                {
                    "repository": "acme/api",
                    "digest": DIGEST_A,
                    "blobDigests": [LAYER_A, LAYER_B],
                }
            ],
            "padding": "x" * 2000,
        }
        compacted = control_server._compact_registry_inventory_result(result)
        self.assertNotIn("blobDigests", compacted["entries"][0])
        response = control_server._compact_json_response(
            SimpleNamespace(headers={"Accept-Encoding": "gzip"}),
            compacted,
        )
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertEqual(json.loads(gzip.decompress(response.body)), compacted)

    def test_dashboard_inventory_paginates_and_filters_server_side(self) -> None:
        result = {
            "entries": [
                {"repository": "acme/api", "digest": DIGEST_A, "tags": ["latest"], "protectionStatus": "protected"},
                {"repository": "acme/worker", "digest": DIGEST_B, "tags": ["old"], "protectionStatus": "candidate"},
                {"repository": "docs/site", "digest": CONFIG_A, "tags": ["old"], "protectionStatus": "candidate"},
            ]
        }
        page = control_server._registry_inventory_page(
            result, offset=0, limit=1, query="old", status="candidate"
        )
        self.assertEqual(page["page"], {"offset": 0, "limit": 1, "total": 2, "hasMore": True})
        self.assertEqual(page["entries"][0]["repository"], "acme/worker")
        self.assertEqual(len(result["entries"]), 3)

    @patch.object(control_server, "_start_registry_background_scan")
    @patch.object(control_server, "_load_registry_scan_snapshot", return_value=None)
    @patch.object(control_server, "_managed_registry_spec")
    def test_first_page_inventory_starts_background_scan(
        self, managed_spec, _load_snapshot, start_scan
    ) -> None:
        managed_spec.return_value = {
            "host": "registry.internal",
            "node": "builder",
            "volumeName": "registry-data",
            "jobId": "registry",
        }
        state = {"registryManagement": {"policy": {"mode": "recommend"}, "deletions": [], "audit": []}}
        result = control_server._registry_inventory_for_state(state, refresh=False)
        self.assertTrue(result["scanPending"])
        start_scan.assert_called_once_with("registry.internal")

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

    @patch.object(control_server, "_managed_registry_spec")
    @patch.object(control_server, "_registry_inventory_for_state")
    @patch.object(control_server, "_apply_state_secrets")
    @patch.object(control_server, "require_token")
    @patch.object(control_server, "load_state")
    def test_purge_deletes_and_collects_in_one_offline_window(
        self, load_state, _require, _secrets, inventory, managed_spec
    ) -> None:
        state = {"deployToken": "token", "registryManagement": {"deletions": [], "audit": []}}
        load_state.return_value = state
        managed_spec.return_value = {
            "host": "registry.internal",
            "node": "builder",
            "volumeName": "registry-data",
            "jobId": "registry",
        }
        inventory.return_value = {
            "protectionComplete": True,
            "entries": [
                {
                    "repository": "acme/api",
                    "digest": DIGEST_A,
                    "tags": ["old"],
                    "logicalBytes": 4096,
                    "protectionStatus": "candidate",
                    "protectionReasons": [],
                }
            ],
        }

        def mutate(callback):
            return callback(state)

        with patch.object(control_server, "_run_registry_offline_task") as offline, patch.object(
            control_server, "_mutate_control_state", side_effect=mutate
        ):
            offline.return_value = {
                "operation": "gc",
                "steps": [
                    {"operation": "delete", "deleted": [{"repository": "acme/api", "digest": DIGEST_A}], "beforeBytes": 10240},
                    {"operation": "gc", "eligibleBlobs": 3, "beforeBytes": 10240, "afterBytes": 6144},
                ],
            }
            result = control_server.handle_registry_purge(
                "token", {"confirm": "delete", "manifests": [{"repository": "acme/api", "digest": DIGEST_A}]}
            )

        # One offline window, two ordered steps: delete then gc.
        self.assertEqual(offline.call_count, 1)
        operations = offline.call_args.kwargs["operations"]
        self.assertEqual([step["operation"] for step in operations], ["delete", "gc"])
        self.assertEqual(operations[0]["manifests"], [{"repository": "acme/api", "digest": DIGEST_A}])
        # Measured across the whole window: delete's before, gc's after.
        self.assertEqual(result["reclaimedBytes"], 4096)
        self.assertEqual(result["collectedBlobs"], 3)
        self.assertFalse(result["sharedLayersOnly"])
        self.assertEqual(result["manifestCount"], 1)
        # The cached snapshot is reused; a purge must not pay for a full rescan.
        self.assertFalse(inventory.call_args.kwargs["refresh"])
        self.assertEqual(state["registryManagement"]["audit"][-1]["action"], "manifests-purged")

    @patch.object(control_server, "_managed_registry_spec")
    @patch.object(control_server, "_registry_inventory_for_state")
    @patch.object(control_server, "_apply_state_secrets")
    @patch.object(control_server, "require_token")
    @patch.object(control_server, "load_state")
    def test_purge_includes_child_manifests_and_reports_but_ignores_protection(
        self, load_state, _require, _secrets, inventory, managed_spec
    ) -> None:
        state = {"deployToken": "token", "registryManagement": {"deletions": [], "audit": []}}
        load_state.return_value = state
        managed_spec.return_value = {
            "host": "registry.internal",
            "node": "builder",
            "volumeName": "registry-data",
            "jobId": "registry",
        }
        # A protected multi-arch index whose platform child is untagged.
        inventory.return_value = {
            "protectionComplete": True,
            "entries": [
                {
                    "repository": "acme/api",
                    "digest": DIGEST_A,
                    "tags": ["latest"],
                    "logicalBytes": 8192,
                    "childManifestDigests": [DIGEST_B],
                    "protectionStatus": "protected",
                    "protectionReasons": [{"kind": "deployment", "source": "service:api"}],
                }
            ],
        }

        def mutate(callback):
            return callback(state)

        with patch.object(control_server, "_run_registry_offline_task") as offline, patch.object(
            control_server, "_mutate_control_state", side_effect=mutate
        ):
            offline.return_value = {
                "steps": [
                    {"operation": "delete", "beforeBytes": 16384},
                    {"operation": "gc", "eligibleBlobs": 5, "beforeBytes": 16384, "afterBytes": 8192},
                ]
            }
            result = control_server.handle_registry_purge(
                "token", {"confirm": "delete", "manifests": [{"repository": "acme/api", "digest": DIGEST_A}]}
            )

        targets = offline.call_args.kwargs["operations"][0]["manifests"]
        # The untagged platform child goes with its index, otherwise its layers leak.
        self.assertEqual(
            [item["digest"] for item in targets],
            [DIGEST_A, DIGEST_B],
        )
        # Protection is surfaced as a risk, never as a block.
        self.assertIn("referenced", result["risks"][0]["reason"])

    @patch.object(control_server, "_managed_registry_spec")
    @patch.object(control_server, "_registry_inventory_for_state")
    @patch.object(control_server, "_apply_state_secrets")
    @patch.object(control_server, "require_token")
    @patch.object(control_server, "load_state")
    def test_purge_fails_loudly_when_no_blobs_were_collected(
        self, load_state, _require, _secrets, inventory, managed_spec
    ) -> None:
        """Deleting a manifest must never report success without reclaiming."""
        state = {"deployToken": "token", "registryManagement": {"deletions": [], "audit": []}}
        load_state.return_value = state
        managed_spec.return_value = {
            "host": "registry.internal",
            "node": "builder",
            "volumeName": "registry-data",
            "jobId": "registry",
        }
        inventory.return_value = {
            "protectionComplete": True,
            "entries": [
                {
                    "repository": "acme/api",
                    "digest": DIGEST_A,
                    "tags": ["old"],
                    "logicalBytes": 4096,
                    "protectionStatus": "candidate",
                    "protectionReasons": [],
                }
            ],
        }

        def mutate(callback):
            return callback(state)

        with patch.object(control_server, "_run_registry_offline_task") as offline, patch.object(
            control_server, "_mutate_control_state", side_effect=mutate
        ):
            # The collector ran but found nothing: a Registry that silently
            # refuses blob deletion looks exactly like this.
            offline.return_value = {
                "steps": [
                    {"operation": "delete", "deleted": [{"repository": "acme/api", "digest": DIGEST_A}], "beforeBytes": 10240},
                    {"operation": "gc", "eligibleBlobs": 0, "beforeBytes": 10240, "afterBytes": 10240},
                ]
            }
            with self.assertRaises(LumaError) as caught:
                control_server.handle_registry_purge(
                    "token", {"confirm": "delete", "manifests": [{"repository": "acme/api", "digest": DIGEST_A}]}
                )
        self.assertIn("not reclaimed", str(caught.exception))

    @patch.object(control_server, "_managed_registry_spec")
    @patch.object(control_server, "_registry_inventory_for_state")
    @patch.object(control_server, "_apply_state_secrets")
    @patch.object(control_server, "require_token")
    @patch.object(control_server, "load_state")
    def test_purge_reports_shared_layers_instead_of_failing(
        self, load_state, _require, _secrets, inventory, managed_spec
    ) -> None:
        """Blobs collected but no bytes freed is legitimate layer sharing."""
        state = {"deployToken": "token", "registryManagement": {"deletions": [], "audit": []}}
        load_state.return_value = state
        managed_spec.return_value = {
            "host": "registry.internal",
            "node": "builder",
            "volumeName": "registry-data",
            "jobId": "registry",
        }
        inventory.return_value = {
            "protectionComplete": True,
            "entries": [
                {
                    "repository": "acme/api",
                    "digest": DIGEST_A,
                    "tags": ["old"],
                    "logicalBytes": 4096,
                    "protectionStatus": "candidate",
                    "protectionReasons": [],
                }
            ],
        }

        def mutate(callback):
            return callback(state)

        with patch.object(control_server, "_run_registry_offline_task") as offline, patch.object(
            control_server, "_mutate_control_state", side_effect=mutate
        ):
            offline.return_value = {
                "steps": [
                    {"operation": "delete", "beforeBytes": 10240},
                    {"operation": "gc", "eligibleBlobs": 1, "beforeBytes": 10240, "afterBytes": 10240},
                ]
            }
            result = control_server.handle_registry_purge(
                "token", {"confirm": "delete", "manifests": [{"repository": "acme/api", "digest": DIGEST_A}]}
            )
        self.assertEqual(result["reclaimedBytes"], 0)
        self.assertTrue(result["sharedLayersOnly"])

    @patch.object(control_server, "load_state")
    def test_purge_requires_explicit_confirmation(self, load_state) -> None:
        load_state.return_value = {"deployToken": "token"}
        with self.assertRaises(LumaError):
            control_server.handle_registry_purge("token", {"manifests": []})

    @patch.object(control_server, "_managed_registry_client")
    @patch.object(control_server, "_wait_registry_healthy")
    @patch.object(control_server, "_wait_registry_job_stopped")
    @patch.object(control_server, "_run_node_agent_task")
    @patch.object(control_server, "_invalidate_registry_scan")
    @patch.object(control_server, "NomadApi")
    @patch.object(control_server, "load_config")
    def test_offline_task_runs_every_step_then_restarts_registry(
        self, _config, nomad_api, _invalidate, run_task, _stopped, _healthy, _client
    ) -> None:
        nomad = nomad_api.return_value
        nomad.request.return_value = {"ID": "registry", "TaskGroups": []}
        run_task.side_effect = [
            {"operation": "delete", "reclaimedBytes": 0},
            {"operation": "gc", "reclaimedBytes": 512},
        ]
        marker_state = {"registryManagement": {"deletions": [], "audit": []}}
        marker_patch = patch.object(
            control_server,
            "_mutate_control_state",
            side_effect=lambda callback: callback(marker_state),
        )
        marker_patch.start()
        self.addCleanup(marker_patch.stop)
        result = control_server._run_registry_offline_task(
            {"nomadToken": "t"},
            {
                "host": "registry.internal",
                "baseUrl": "https://registry.internal",
                "node": "builder",
                "volumeName": "vol",
                "image": "registry:2",
                "jobId": "registry",
            },
            operations=[
                {"operation": "delete", "manifests": [{"repository": "acme/api", "digest": DIGEST_A}]},
                {"operation": "gc"},
            ],
        )
        self.assertEqual(run_task.call_count, 2)
        self.assertEqual(
            [call.args[3]["operation"] for call in run_task.call_args_list],
            ["delete", "gc"],
        )
        # The last step carries the reclaimed total; every step is still reported.
        self.assertEqual(result["reclaimedBytes"], 512)
        self.assertEqual(len(result["steps"]), 2)
        # The Registry job is recreated after the final step.
        self.assertTrue(any(call.args[0] == "POST" for call in nomad.request.call_args_list))

    @patch.object(control_server, "_managed_registry_client")
    @patch.object(control_server, "_wait_registry_healthy")
    @patch.object(control_server, "_wait_registry_job_stopped")
    @patch.object(control_server, "_run_node_agent_task")
    @patch.object(control_server, "_invalidate_registry_scan")
    @patch.object(control_server, "NomadApi")
    @patch.object(control_server, "load_config")
    def test_offline_task_records_then_clears_maintenance_marker(
        self, _config, nomad_api, _invalidate, run_task, _stopped, _healthy, _client
    ) -> None:
        """The marker is what lets a crashed Control restart the Registry."""
        state = {"nomadToken": "t", "registryManagement": {"deletions": [], "audit": []}}
        nomad = nomad_api.return_value
        nomad.request.return_value = {"ID": "registry", "TaskGroups": []}
        run_task.return_value = {"operation": "gc", "reclaimedBytes": 1}
        seen_during_window: list[Any] = []

        def mutate(callback):
            result = callback(state)
            marker = state["registryManagement"].get("maintenance")
            seen_during_window.append(copy.deepcopy(marker) if marker else None)
            return result

        with patch.object(control_server, "_mutate_control_state", side_effect=mutate):
            control_server._run_registry_offline_task(
                state,
                {
                    "host": "registry.internal",
                    "baseUrl": "https://registry.internal",
                    "node": "builder",
                    "volumeName": "vol",
                    "image": "registry:2",
                    "jobId": "registry",
                },
                operation="gc",
            )

        # Recorded before the stop, carrying the job needed to rebuild it...
        self.assertEqual(seen_during_window[0]["jobId"], "registry")
        self.assertEqual(seen_during_window[0]["job"]["ID"], "registry")
        # ...and cleared only after the Registry answered a health check.
        self.assertIsNone(seen_during_window[-1])
        self.assertNotIn("maintenance", state["registryManagement"])

    @patch.object(control_server, "_managed_registry_client")
    @patch.object(control_server, "_wait_registry_healthy")
    @patch.object(control_server, "_wait_registry_job_stopped")
    @patch.object(control_server, "_run_node_agent_task")
    @patch.object(control_server, "NomadApi")
    @patch.object(control_server, "load_config")
    def test_maintenance_marker_survives_a_failed_restart(
        self, _config, nomad_api, run_task, _stopped, healthy, _client
    ) -> None:
        state = {"nomadToken": "t", "registryManagement": {"deletions": [], "audit": []}}
        nomad = nomad_api.return_value
        nomad.request.return_value = {"ID": "registry", "TaskGroups": []}
        run_task.return_value = {"operation": "gc"}
        healthy.side_effect = LumaError("registry never became healthy")

        def mutate(callback):
            return callback(state)

        with patch.object(control_server, "_mutate_control_state", side_effect=mutate):
            with self.assertRaises(LumaError):
                control_server._run_registry_offline_task(
                    state,
                    {
                        "host": "registry.internal",
                        "baseUrl": "https://registry.internal",
                        "node": "builder",
                        "volumeName": "vol",
                        "image": "registry:2",
                        "jobId": "registry",
                    },
                    operation="gc",
                )
        # The Registry did not come back, so the marker must remain for recovery.
        self.assertEqual(state["registryManagement"]["maintenance"]["jobId"], "registry")

    @patch.object(control_server, "_managed_registry_client")
    @patch.object(control_server, "_invalidate_registry_scan")
    @patch.object(control_server, "_wait_registry_healthy")
    @patch.object(control_server, "_managed_registry_spec")
    @patch.object(control_server, "NomadApi")
    @patch.object(control_server, "load_config")
    @patch.object(control_server, "load_state")
    def test_recovery_restarts_a_registry_stranded_by_a_crash(
        self, load_state, _config, nomad_api, managed_spec, _healthy, _invalidate, _client
    ) -> None:
        """A Control crash inside the window must not leave the Registry down."""
        state = {
            "nomadToken": "t",
            "registryManagement": {
                "deletions": [],
                "audit": [],
                "maintenance": {
                    "jobId": "registry",
                    "job": {"ID": "registry", "Stop": True, "TaskGroups": []},
                    "host": "registry.internal",
                    "openedAt": 1_800_000_000,
                },
            },
        }
        load_state.return_value = state
        managed_spec.return_value = {
            "host": "registry.internal",
            "baseUrl": "https://registry.internal",
            "node": "builder",
            "volumeName": "vol",
            "image": "registry:2",
            "jobId": "registry",
        }
        nomad = nomad_api.return_value

        def mutate(callback):
            return callback(state)

        with patch.object(control_server, "_mutate_control_state", side_effect=mutate):
            result = control_server._registry_recover_pending_maintenance()

        self.assertTrue(result["recovered"])
        submitted = [call for call in nomad.request.call_args_list if call.args[0] == "POST"]
        self.assertEqual(len(submitted), 1)
        # Stop is cleared so the job actually runs again.
        self.assertFalse(submitted[0].args[2]["Job"]["Stop"])
        self.assertNotIn("maintenance", state["registryManagement"])

    @patch.object(control_server, "load_state")
    def test_recovery_is_a_noop_without_a_marker(self, load_state) -> None:
        load_state.return_value = {"registryManagement": {"deletions": [], "audit": []}}
        self.assertIsNone(control_server._registry_recover_pending_maintenance())

    @patch.object(control_server, "_managed_registry_spec")
    @patch.object(control_server, "load_config")
    @patch.object(control_server, "load_state")
    def test_recovery_keeps_marker_and_never_raises_when_restart_fails(
        self, load_state, _config, managed_spec
    ) -> None:
        state = {
            "nomadToken": "t",
            "registryManagement": {
                "deletions": [],
                "audit": [],
                "maintenance": {
                    "jobId": "registry",
                    "job": {"ID": "registry", "TaskGroups": []},
                    "host": "registry.internal",
                },
            },
        }
        load_state.return_value = state
        managed_spec.side_effect = LumaError("registry spec unavailable")

        def mutate(callback):
            return callback(state)

        with patch.object(control_server, "_mutate_control_state", side_effect=mutate):
            # Must not propagate: recovery runs on Control startup and in the
            # automation loop, neither of which may be blocked by it.
            result = control_server._registry_recover_pending_maintenance()

        self.assertFalse(result["recovered"])
        self.assertEqual(state["registryManagement"]["maintenance"]["jobId"], "registry")

    @patch.object(control_server, "_record_registry_automation")
    @patch.object(control_server, "_registry_inventory_for_state")
    @patch.object(control_server, "load_state")
    def test_automation_tick_recovers_registry_before_anything_else(
        self, load_state, inventory, _record
    ) -> None:
        load_state.return_value = {
            "deployToken": "token",
            "registryManagement": {"policy": {"mode": "off"}, "deletions": [], "audit": []},
        }
        inventory.return_value = {"protectionComplete": True, "entries": []}
        with patch.object(control_server, "_registry_recover_pending_maintenance") as recover:
            control_server._registry_automation_tick()
        # Recovery is an availability guarantee: it runs even with retention off.
        recover.assert_called_once()

    def test_gc_container_enables_blob_deletion(self) -> None:
        """Without delete.enabled, garbage-collect exits 0 and frees nothing."""
        from luma import agent as luma_agent

        captured: list[list[str]] = []

        def fake_stream(command, **_kwargs):
            captured.append(list(command))
            return SimpleNamespace(code=0, output="blob eligible for deletion: sha256:x")

        with patch.object(luma_agent, "_docker_binary", return_value="/usr/bin/docker"), patch.object(
            luma_agent, "node_agent_os", return_value="linux"
        ), patch.object(luma_agent.subprocess, "run") as run, patch.object(
            luma_agent, "inspect_registry_storage"
        ) as storage, patch.object(luma_agent, "_run_process_streaming", side_effect=fake_stream):
            run.return_value = SimpleNamespace(returncode=0, stdout="")
            storage.side_effect = [{"volumeBytes": 10240}, {"volumeBytes": 4096}]
            result = luma_agent.registry_maintenance(
                volume_name="vol", image="registry:2", operation="gc", manifests=[]
            )

        self.assertIn("REGISTRY_STORAGE_DELETE_ENABLED=true", captured[0])
        self.assertEqual(result["reclaimedBytes"], 6144)

    def test_gc_refuses_to_report_success_when_nothing_shrank(self) -> None:
        from luma import agent as luma_agent

        with patch.object(luma_agent, "_docker_binary", return_value="/usr/bin/docker"), patch.object(
            luma_agent, "node_agent_os", return_value="linux"
        ), patch.object(luma_agent.subprocess, "run") as run, patch.object(
            luma_agent, "inspect_registry_storage"
        ) as storage, patch.object(luma_agent, "_run_process_streaming") as stream:
            run.return_value = SimpleNamespace(returncode=0, stdout="")
            # Collector claims two blobs were collectable, volume never shrank.
            stream.return_value = SimpleNamespace(
                code=0,
                output="blob eligible for deletion: a\nblob eligible for deletion: b",
            )
            storage.side_effect = [{"volumeBytes": 10240}, {"volumeBytes": 10240}]
            with self.assertRaises(LumaError) as caught:
                luma_agent.registry_maintenance(
                    volume_name="vol", image="registry:2", operation="gc", manifests=[]
                )
        self.assertIn("reclaimed no space", str(caught.exception))

    def test_gc_preview_stays_a_dry_run_and_never_asserts_shrinkage(self) -> None:
        from luma import agent as luma_agent

        captured: list[list[str]] = []

        def fake_stream(command, **_kwargs):
            captured.append(list(command))
            return SimpleNamespace(code=0, output="blob eligible for deletion: sha256:x")

        with patch.object(luma_agent, "_docker_binary", return_value="/usr/bin/docker"), patch.object(
            luma_agent, "node_agent_os", return_value="linux"
        ), patch.object(luma_agent.subprocess, "run") as run, patch.object(
            luma_agent, "inspect_registry_storage"
        ) as storage, patch.object(luma_agent, "_run_process_streaming", side_effect=fake_stream):
            run.return_value = SimpleNamespace(returncode=0, stdout="")
            storage.side_effect = [{"volumeBytes": 10240}, {"volumeBytes": 10240}]
            result = luma_agent.registry_maintenance(
                volume_name="vol", image="registry:2", operation="gc-preview", manifests=[]
            )
        self.assertIn("--dry-run", captured[0])
        self.assertEqual(result["eligibleBlobs"], 1)
        self.assertEqual(result["reclaimedBytes"], 0)

    def test_maintenance_refuses_to_run_while_the_volume_is_mounted(self) -> None:
        """Collecting against a live volume risks corrupting it."""
        from luma import agent as luma_agent

        with patch.object(luma_agent, "_docker_binary", return_value="/usr/bin/docker"), patch.object(
            luma_agent, "node_agent_os", return_value="linux"
        ), patch.object(luma_agent.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="abc123\n")
            with self.assertRaises(LumaError) as caught:
                luma_agent.registry_maintenance(
                    volume_name="vol", image="registry:2", operation="gc", manifests=[]
                )
        self.assertIn("still mounted", str(caught.exception))

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
