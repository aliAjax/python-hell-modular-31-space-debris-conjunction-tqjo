import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def high_risk_payload():
    return {
        "primary_object_id": "SAT-7",
        "secondary_object_id": "DEB-2",
        "tca": "2026-10-05T12:00:00+00:00",
        "miss_distance_m": 10,
        "covariance_m": 100,
        "fuel_budget_m_s": 5,
        "track_age_hours": 1,
        "operating_organizations": ["Org-A"],
    }


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self._coordinated_item()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _coordinated_item(self, payload=None):
        item = self.service.create_item(payload or high_risk_payload(), "a-1", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 6}, "a-1", "analyst", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "high")
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 1,
            "maneuver_window": "2026-10-05T08:00:00Z/2026-10-05T09:00:00Z",
        }, "c-1", "coordinator", item["version"])
        return item

    def _issue(self, ref="CMD-1", expected=None):
        return self.service.act(
            self.item["id"], "execute", {"command_ref": ref},
            "o-1", "operator", self.item["version"] if expected is None else expected,
        )

    def test_issue_is_pending_until_coordinated_receipt_confirms(self):
        item = self._issue()
        self.assertEqual(item["status"], "executing")
        self.assertEqual(item["open_command"]["status"], "pending")
        self.assertIsNone(item["open_command"]["coordination_ref"])
        # 缺少协调编号的回执不能完成确认
        with self.assertRaises(DomainError) as ctx:
            self.service.ingest_receipt(item["id"], {"command_ref": "CMD-1"}, "sys", "system")
        self.assertEqual(ctx.exception.code, "field_required")
        # 带协调编号的回执才算执行完成
        result = self.service.ingest_receipt(
            item["id"], {"command_ref": "CMD-1", "coordination_ref": "COORD-100"}, "sys", "system"
        )
        self.assertTrue(result["confirmed"])
        item = self.service.get_item(item["id"])
        self.assertEqual(item["open_command"], None)
        command = item["commands"][-1]
        self.assertEqual(command["status"], "confirmed")
        self.assertEqual(command["coordination_ref"], "COORD-100")
        types = [event["event_type"] for event in item["audit"]]
        self.assertEqual(types[-1], "command_confirmed")
        self.assertIn("command_issued", types)

    def test_same_coordination_ref_duplicate_receipt_counts_once(self):
        item = self._issue()
        first = self.service.ingest_receipt(
            item["id"], {"command_ref": "CMD-1", "coordination_ref": "COORD-100"}, "sys", "system"
        )
        self.assertTrue(first["confirmed"])
        # 网络重发：同一编号重复到达，只认一次
        second = self.service.ingest_receipt(
            item["id"], {"command_ref": "CMD-1", "coordination_ref": "COORD-100"}, "sys", "system"
        )
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["confirmed"])
        item = self.service.get_item(item["id"])
        confirmed = [c for c in item["commands"] if c["status"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(len(item["receipts"]), 2)
        self.assertEqual([r["disposition"] for r in item["receipts"]], ["confirmed", "duplicate"])
        # 审计事件仍只有一次确认
        self.assertEqual([e["event_type"] for e in item["audit"]].count("command_confirmed"), 1)

    def test_only_one_open_command_per_conjunction(self):
        item = self._issue()
        with self.assertRaises(ConflictError) as ctx:
            self.service.act(
                item["id"], "execute", {"command_ref": "CMD-2"}, "o-2", "operator", item["version"]
            )
        self.assertEqual(ctx.exception.code, "pending_command_exists")
        item = self.service.get_item(item["id"])
        pending = [c for c in item["commands"] if c["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["command_ref"], "CMD-1")

    def test_reissue_before_confirmation_supersedes_old(self):
        self._issue()
        item = self.service.get_item(self.item["id"])
        item = self.service.act(
            item["id"], "reissue_command", {"command_ref": "CMD-2"}, "o-1", "operator", item["version"]
        )
        refs = [(c["attempt"], c["command_ref"], c["status"]) for c in item["commands"]]
        self.assertEqual(refs, [(1, "CMD-1", "superseded"), (2, "CMD-2", "pending")])
        # 旧指令的晚到回执不改变新指令状态
        late = self.service.ingest_receipt(
            item["id"], {"command_ref": "CMD-1", "coordination_ref": "COORD-LATE"}, "sys", "system"
        )
        self.assertTrue(late["duplicate"])
        self.assertEqual(late["disposition"], "late")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["open_command"]["command_ref"], "CMD-2")
        # 新指令凭自己的协调编号确认
        done = self.service.ingest_receipt(
            item["id"], {"command_ref": "CMD-2", "coordination_ref": "COORD-200"}, "sys", "system"
        )
        self.assertTrue(done["confirmed"])

    def test_risk_revision_voids_pending_command_and_returns_to_assessment(self):
        self._issue()
        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["open_command"]["risk_level"], "high")
        # 新观测把风险从 high 拉到 low（距离 5000、协方差 100）
        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-10-05T05:00:00+00:00",
            "miss_distance_m": 5000,
            "covariance_m": 100,
            "source": "radar-updated",
        }, "a-1", "analyst", item["version"])
        self.assertEqual(item["status"], "assessed")
        self.assertEqual(item["payload"]["assessment"]["level"], "low")
        self.assertIsNone(item["open_command"])
        self.assertEqual(item["commands"][-1]["status"], "voided")
        types = [e["event_type"] for e in item["audit"]]
        self.assertIn("command_voided", types)
        # 作废指令的晚到回执不能复活它
        stale = self.service.ingest_receipt(
            item["id"], {"command_ref": "CMD-1", "coordination_ref": "COORD-X"}, "sys", "system"
        )
        self.assertEqual(stale["disposition"], "late")
        self.assertFalse(stale["confirmed"])

    def test_same_level_revision_keeps_pending_command_alive(self):
        self._issue()
        item = self.service.get_item(self.item["id"])
        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-10-05T05:00:00+00:00",
            "miss_distance_m": 8,
            "covariance_m": 100,
            "source": "radar-updated",
        }, "a-1", "analyst", item["version"])
        self.assertEqual(item["status"], "executing")
        self.assertIsNotNone(item["open_command"])
        self.assertEqual(item["open_command"]["status"], "pending")

    def test_cancel_vs_issue_concurrency_first_writer_wins(self):
        # 值班员A撤销、值班员B重新发起同时提交，只有先拿到写锁的一方生效
        item = self._issue()
        version = self.service.get_item(item["id"])["version"]
        results = {}

        def cancel():
            try:
                results["cancel"] = self.service.act(
                    item["id"], "cancel_command", {"reason": "窗口冲突"},
                    "o-a", "operator", version,
                )
            except ConflictError as exc:
                results["cancel"] = exc

        def reissue():
            try:
                results["reissue"] = self.service.act(
                    item["id"], "reissue_command", {"command_ref": "CMD-R"},
                    "o-b", "operator", version,
                )
            except ConflictError as exc:
                results["reissue"] = exc

        t1 = threading.Thread(target=cancel)
        t2 = threading.Thread(target=reissue)
        t1.start(); t2.start()
        t1.join(); t2.join()
        ok = [name for name, value in results.items() if not isinstance(value, ConflictError)]
        failed = [name for name, value in results.items() if isinstance(value, ConflictError)]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(results[failed[0]].code, "version_conflict")
        latest = self.service.get_item(item["id"])
        self.assertEqual(latest["version"], version + 1)
        if ok == ["cancel"]:
            self.assertEqual(latest["status"], "coordinating")
            self.assertEqual(latest["commands"][-1]["status"], "cancelled")
        else:
            self.assertEqual(latest["status"], "executing")
            self.assertEqual(latest["open_command"]["command_ref"], "CMD-R")

    def test_two_operators_cannot_open_two_commands(self):
        item = self._issue()
        version = self.service.get_item(item["id"])["version"]
        errors = []

        def attempt(ref):
            try:
                self.service.act(
                    item["id"], "reissue_command", {"command_ref": ref},
                    ref, "operator", version,
                )
            except ConflictError as exc:
                errors.append(exc.code)

        threads = [threading.Thread(target=attempt, args=("CMD-A",)),
                   threading.Thread(target=attempt, args=("CMD-B",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 至少一方被版本或单飞约束挡下，最终只有一条待确认指令
        pending = [c for c in self.service.get_item(item["id"])["commands"] if c["status"] == "pending"]
        self.assertEqual(len(pending), 1)

    def test_recovers_reconciliation_state_after_restart(self):
        self._issue()
        # 用全新的 Repository/Service 重新打开同一个数据库，模拟崩溃重启
        restarted_repo = Repository(self.tmp.name)
        restarted = Service(restarted_repo)
        item = restarted.get_item(self.item["id"])
        self.assertEqual(item["status"], "executing")
        self.assertEqual(item["open_command"]["command_ref"], "CMD-1")
        summary = restarted.reconciliation()
        self.assertEqual(summary["totals"]["pending"], 1)
        # 重启后仍可完成确认
        result = restarted.ingest_receipt(
            item["id"], {"command_ref": "CMD-1", "coordination_ref": "COORD-9"}, "sys", "system"
        )
        self.assertTrue(result["confirmed"])
        summary = Service(Repository(self.tmp.name)).reconciliation()
        self.assertEqual(summary["totals"]["pending"], 0)
        self.assertEqual(summary["totals"]["confirmed"], 1)

    def test_unmatched_receipt_is_recorded_but_does_nothing(self):
        result = self.service.ingest_receipt(
            self.item["id"], {"command_ref": "UNKNOWN", "coordination_ref": "COORD-X"}, "sys", "system"
        )
        self.assertFalse(result["matched"])
        self.assertEqual(result["disposition"], "unmatched")
        summary = self.service.reconciliation()
        self.assertEqual(len(summary["unmatched_receipts"]), 1)

    def test_audit_chain_survives_voiding(self):
        self._issue()
        item = self.service.get_item(self.item["id"])
        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-10-05T05:00:00+00:00",
            "miss_distance_m": 5000,
            "covariance_m": 100,
            "source": "radar-updated",
        }, "a-1", "analyst", item["version"])
        hashes = [event["event_hash"] for event in item["audit"]]
        self.assertEqual(len(hashes), len(set(hashes)))
        chain = item["audit"]
        self.assertEqual(chain[0]["previous_hash"], "GENESIS")
        for previous, current in zip(chain, chain[1:]):
            self.assertEqual(current["previous_hash"], previous["event_hash"])


if __name__ == "__main__":
    unittest.main()
