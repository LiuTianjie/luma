import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.control import database, server, state
from luma.errors import LumaError


class RegistryReadPerformanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        cache = patch.object(server, "_REGISTRY_SCAN_CACHE", {})
        cache.start()
        self.addCleanup(cache.stop)
        self.original = {
            "deployToken": "test-token",
            "build": {"registryHost": "registry.example.test"},
            "deployments": {"services": {"luma-registry": {"manifest":
                "image: registry:2\nnode: builder\nvolumes:\n  - registry-data:/var/lib/registry\n"
            }}},
            "registryManagement": {"policy": {"mode": "recommend"}, "deletions": [], "audit": [{"action": "scan"}]},
            "buildRuns": {"historic-build": {"id": "historic-build", "events": [{"message": "old build log"}]}},
            "deploymentEvents": [{"id": "historic-deploy", "steps": [{"title": "old deployment"}]}],
            "agentTasks": {"historic-task": {"id": "historic-task", "events": [{"message": "old task log"}]}},
        }
        state.save_state(self.original)
        state.save_state({
            "host": "registry.example.test",
            "result": {"entries": [{"repository": "app", "tags": ["latest"]}], "protectionComplete": True},
        }, Path(directory.name) / "registry-inventory.json")

    def test_inventory_cache_read_keeps_config_without_hydrating_history_or_scanning(self):
        with patch.object(database, "read_entity", side_effect=AssertionError("cached inventory must not hydrate history")), \
                patch.object(server, "_refresh_registry_inventory", side_effect=AssertionError("read must not scan")), \
                patch.object(server, "_start_registry_background_scan", side_effect=AssertionError("snapshot already exists")):
            result = server.handle_registry_inventory("test-token")
        self.assertEqual(result["entries"][0]["repository"], "app")
        self.assertEqual(result["policy"]["mode"], "recommend")
        self.assertEqual(result["audit"], [{"action": "scan"}])
        self.assertTrue(result["protectionComplete"])

    def test_inventory_refresh_still_loads_history_for_protection_references(self):
        with patch.object(server, "_registry_inventory_for_state", return_value={"refreshed": True}) as inventory:
            result = server.handle_registry_inventory("test-token", refresh=True)
        self.assertEqual(result, {"refreshed": True})
        snapshot = inventory.call_args.args[0]
        self.assertEqual(snapshot["buildRuns"], self.original["buildRuns"])
        self.assertEqual(snapshot["deploymentEvents"], self.original["deploymentEvents"])
        self.assertEqual(snapshot["agentTasks"], self.original["agentTasks"])
        self.assertEqual(inventory.call_args.kwargs, {"refresh": True})

    def test_inventory_rejects_invalid_token_before_accessing_snapshot(self):
        with patch.object(server, "_registry_inventory_for_state") as inventory:
            with self.assertRaisesRegex(LumaError, "unauthorized"):
                server.handle_registry_inventory("invalid-token")
        inventory.assert_not_called()

    def test_policy_read_does_not_hydrate_any_entities(self):
        with patch.object(database, "read_entity", side_effect=AssertionError("policy does not need history")):
            result = server.handle_registry_policy_get("test-token")
        self.assertEqual(result["policy"]["mode"], "recommend")

    def test_page_copies_selected_entries_only_and_keeps_result_detached(self):
        class UnselectedEntry(dict):
            def __deepcopy__(self, memo):
                raise AssertionError("pagination must not deep-copy an unselected entry")

        snapshot = {
            "entries": [
                UnselectedEntry(repository="older"),
                {"repository": "selected", "tags": ["latest"]},
                UnselectedEntry(repository="later"),
            ],
            "summary": {"repositoryCount": 3},
        }
        page = server._registry_inventory_page(snapshot, offset=1, limit=1)
        self.assertEqual(page["page"], {"offset": 1, "limit": 1, "total": 3, "hasMore": True})
        self.assertEqual(page["entries"], [{"repository": "selected", "tags": ["latest"]}])
        page["entries"][0]["tags"].append("edited")
        page["summary"]["repositoryCount"] = 99
        self.assertEqual(snapshot["entries"][1]["tags"], ["latest"])
        self.assertEqual(snapshot["summary"]["repositoryCount"], 3)


if __name__ == "__main__":
    unittest.main()
