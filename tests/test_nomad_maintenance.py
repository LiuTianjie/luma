"""Routine certificate maintenance must not replay the retired migration."""
import copy
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from luma import nomad_upgrade as maintenance


class NomadMaintenanceTests(unittest.TestCase):
    def test_offline_node_does_not_reopen_completed_migration(self):
        journal = {
            "status": "completed", "verifiedAt": 123, "next": 2,
            "steps": [{"kind": "secrets"}, {"kind": "verify"}],
            "nodes": {"air1": {"nodeId": "air-id", "verification": {"ok": True}}},
        }
        original = copy.deepcopy(journal)
        with patch.object(maintenance, "_state", return_value={}), patch.object(
            maintenance, "_journal_lock", return_value=nullcontext()
        ), patch.object(maintenance, "snapshot", return_value={"node": "manager", "nodeId": "manager-id"}), patch.object(
            maintenance, "_read_journal", return_value=journal
        ), patch.object(maintenance, "registered_nodes", return_value=(
            {"air1": {"nodeId": "air-id", "address": "100.64.0.2"}}, ["air1"]
        )), patch.object(maintenance, "_maintain_certificates") as renew, patch.object(
            maintenance, "reconcile_once", side_effect=AssertionError("migration replayed")
        ):
            maintenance.maintain_once()
        renew.assert_called_once_with(journal, {})
        for field in ("status", "verifiedAt", "next", "steps"):
            self.assertEqual(journal[field], original[field])
        self.assertEqual(journal["nodes"]["air1"]["verification"], {"ok": True})

    def test_current_enrollment_replaces_removed_or_reused_node_identity(self):
        journal = {
            "nodes": {"worker": {"nodeId": "old", "verification": {"ok": True}}, "removed": {"nodeId": "gone"}},
            "renewals": {"worker": {"certificate": "old-certificate"}},
        }
        with patch.object(maintenance, "_state", return_value={}), patch.object(
            maintenance, "_journal_lock", return_value=nullcontext()
        ), patch.object(maintenance, "snapshot", return_value={"node": "manager", "nodeId": "manager-id"}), patch.object(
            maintenance, "_read_journal", return_value=journal
        ), patch.object(maintenance, "registered_nodes", return_value=(
            {"worker": {"nodeId": "new", "address": "100.64.0.3"}}, []
        )), patch.object(maintenance, "_maintain_certificates"):
            maintenance.maintain_once()
        self.assertEqual(set(journal["nodes"]), {"manager", "worker"})
        self.assertEqual(journal["nodes"]["worker"], {"nodeId": "new", "address": "100.64.0.3"})
        self.assertNotIn("worker", journal["renewals"])


if __name__ == "__main__":
    unittest.main()
