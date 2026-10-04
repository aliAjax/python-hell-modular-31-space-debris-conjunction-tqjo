import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def _base_payload(**overrides):
    payload = {
        "primary_object_id": "SAT-1",
        "secondary_object_id": "DEB-9",
        "tca": "2026-09-28T12:00:00+00:00",
        "miss_distance_m": 120,
        "covariance_m": 100,
        "fuel_budget_m_s": 5,
        "track_age_hours": 1,
        "operating_organizations": ["Org-A", "Org-B"],
    }
    payload.update(overrides)
    return payload


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _go_to_coordinating(self, **overrides):
        item = self.service.create_item(_base_payload(**overrides), "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.5,
            "maneuver_window": "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z",
        }, "coordinator-1", "coordinator", item["version"])
        return item

    def _go_to_executing(self, **overrides):
        item = self._go_to_coordinating(**overrides)
        item = self.service.act(item["id"], "initiate_command", {"command_ref": "CMD-7"}, "operator-1", "operator", item["version"])
        return item

    def test_command_sent_is_pending_until_receipt(self):
        item = self._go_to_executing()
        self.assertEqual(item["status"], "executing")
        self.assertEqual(len(item["commands"]), 1)
        command = item["commands"][0]
        self.assertEqual(command["status"], "pending_confirm")
        self.assertIsNone(command["coordination_number"])

        item = self.service.act(item["id"], "record_receipt", {
            "coordination_number": "COORD-7",
            "received_at": "2026-09-28T08:30:00Z",
        }, "operator-1", "operator", item["version"])
        command = item["commands"][0]
        self.assertEqual(command["status"], "executed")
        self.assertEqual(command["coordination_number"], "COORD-7")
        self.assertEqual(len(item["receipts"]), 1)
        self.assertEqual(item["receipts"][0]["coordination_number"], "COORD-7")

    def test_duplicate_receipt_recognized_once(self):
        item = self._go_to_executing()
        first = self.service.act(item["id"], "record_receipt", {
            "coordination_number": "COORD-DUP",
            "received_at": "2026-09-28T08:30:00Z",
        }, "operator-1", "operator", item["version"])
        self.assertEqual(first["commands"][0]["status"], "executed")

        second = self.service.act(item["id"], "record_receipt", {
            "coordination_number": "COORD-DUP",
            "received_at": "2026-09-28T08:31:00Z",
        }, "operator-2", "operator", first["version"])
        self.assertEqual(second["commands"][0]["status"], "executed")
        self.assertEqual(len(second["receipts"]), 1)
        dup_audits = [e for e in second["audit"] if e["event_type"] == "duplicate_receipt"]
        self.assertEqual(len(dup_audits), 1)

    def test_reinitiate_supersedes_pending_command(self):
        item = self._go_to_executing()
        self.assertEqual(item["commands"][0]["command_ref"], "CMD-7")

        item = self.service.act(item["id"], "initiate_command", {"command_ref": "CMD-8"}, "operator-1", "operator", item["version"])
        statuses = {c["command_ref"]: c["status"] for c in item["commands"]}
        self.assertEqual(statuses["CMD-7"], "superseded")
        self.assertEqual(statuses["CMD-8"], "pending_confirm")
        pending = [c for c in item["commands"] if c["status"] == "pending_confirm"]
        self.assertEqual(len(pending), 1)

        item = self.service.act(item["id"], "record_receipt", {
            "coordination_number": "COORD-8",
            "received_at": "2026-09-28T08:30:00Z",
        }, "operator-1", "operator", item["version"])
        statuses = {c["command_ref"]: c["status"] for c in item["commands"]}
        self.assertEqual(statuses["CMD-7"], "superseded")
        self.assertEqual(statuses["CMD-8"], "executed")

    def test_cannot_reinitiate_executed_command(self):
        item = self._go_to_executing()
        item = self.service.act(item["id"], "record_receipt", {
            "coordination_number": "COORD-9",
            "received_at": "2026-09-28T08:30:00Z",
        }, "operator-1", "operator", item["version"])
        with self.assertRaises(ConflictError) as context:
            self.service.act(item["id"], "initiate_command", {"command_ref": "CMD-10"}, "operator-1", "operator", item["version"])
        self.assertEqual(context.exception.code, "command_already_executed")

    def test_risk_change_voids_pending_command_and_returns_to_evaluation(self):
        item = self._go_to_executing(miss_distance_m=10, covariance_m=100)
        self.assertEqual(item["payload"]["assessment"]["level"], "high")

        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-09-27T10:00:00Z",
            "miss_distance_m": 2000,
            "covariance_m": 100,
            "source": "new-tracking",
        }, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["status"], "assessed")
        self.assertEqual(item["payload"]["assessment"]["level"], "low")
        command = item["commands"][0]
        self.assertEqual(command["status"], "voided")
        self.assertEqual(command["reason"], "risk_changed")
        void_audits = [e for e in item["audit"] if e["event_type"] == "command_voided"]
        self.assertEqual(len(void_audits), 1)

    def test_resolve_blocked_without_receipt(self):
        item = self._go_to_executing()
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "resolve", {"report_ref": "RPT-7"}, "coordinator-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "command_not_executed")

    def test_crash_recovery_persists_pending_command(self):
        item = self._go_to_executing()
        self.assertEqual(item["commands"][0]["status"], "pending_confirm")

        # Simulate a service restart: a brand new repository handle on the same file.
        recovered = Repository(self.tmp.name)
        recovered.initialize()
        recovered_service = Service(recovered)
        loaded = recovered_service.get_item(item["id"])
        self.assertEqual(loaded["status"], "executing")
        self.assertEqual(loaded["commands"][0]["status"], "pending_confirm")

        # The pending command can still be reconciled after restart.
        loaded = recovered_service.act(item["id"], "record_receipt", {
            "coordination_number": "COORD-RECOVERED",
            "received_at": "2026-09-28T08:30:00Z",
        }, "operator-1", "operator", loaded["version"])
        self.assertEqual(loaded["commands"][0]["status"], "executed")

    def test_optimistic_concurrency_first_write_wins(self):
        item = self._go_to_coordinating()
        version = item["version"]

        first = self.service.act(item["id"], "initiate_command", {"command_ref": "CMD-A"}, "operator-1", "operator", version)
        self.assertEqual(first["commands"][0]["command_ref"], "CMD-A")

        with self.assertRaises(ConflictError) as context:
            self.service.act(item["id"], "initiate_command", {"command_ref": "CMD-B"}, "operator-2", "operator", version)
        self.assertEqual(context.exception.code, "version_conflict")

        # The losing operator re-reads the latest version and retries.
        reread = self.service.get_item(item["id"])
        self.assertEqual(reread["version"], version + 1)
        second = self.service.act(item["id"], "initiate_command", {"command_ref": "CMD-B"}, "operator-2", "operator", reread["version"])
        statuses = {c["command_ref"]: c["status"] for c in second["commands"]}
        self.assertEqual(statuses["CMD-A"], "superseded")
        self.assertEqual(statuses["CMD-B"], "pending_confirm")

    def test_reconciliation_view_and_outstanding(self):
        item = self._go_to_executing()
        recon = self.service.reconciliation(item["id"])
        self.assertEqual(len(recon["pending"]), 1)
        self.assertFalse(recon["all_reconciled"])

        state = self.service.reconciliation_state()
        self.assertEqual(state["outstanding_count"], 1)
        self.assertEqual(state["outstanding"][0]["item"]["id"], item["id"])

        item = self.service.act(item["id"], "record_receipt", {
            "coordination_number": "COORD-VIEW",
            "received_at": "2026-09-28T08:30:00Z",
        }, "operator-1", "operator", item["version"])
        recon = self.service.reconciliation(item["id"])
        self.assertEqual(len(recon["executed"]), 1)
        self.assertTrue(recon["all_reconciled"])
        state = self.service.reconciliation_state()
        self.assertEqual(state["outstanding_count"], 0)


if __name__ == "__main__":
    unittest.main()
