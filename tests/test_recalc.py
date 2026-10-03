import json
import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.recalc import (ANNUAL_INVESTIGATION_LEVEL, RecalcEngine, build_decision,
                        derive_year, normalize_batch)
from src.repository import Repository
from src.service import Service


def batch(batch_no, readings, source="lab_direct", batch_type="normal",
          reason=None):
    payload = {"batch_no": batch_no, "source": source,
               "batch_type": batch_type}
    if reason:
        payload["reason"] = reason
    payload["readings"] = readings
    return payload


def reading(person, period, dose, measured_at="2026-01-15T10:00:00+00:00",
            source=None, instrument=None):
    row = {"person_id": person, "period": period, "dose": dose,
           "measured_at": measured_at}
    if source:
        row["source"] = source
    if instrument:
        row["instrument_id"] = instrument
    return row


class RecalcBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "recalc.db"))
        self.service = Service(self.repo)
        self.engine = self.service.recalc

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def submit(self, *payloads):
        return self.service.submit_batches(
            {"batches": list(payloads)}, "tech", "dosimetrist")["outcomes"]

    def state(self):
        return self.service.dose_state("viewer")


class RecalcChainTest(RecalcBase):
    def test_chain_total_and_disposition(self):
        outcomes = self.submit(batch("B1", [
            reading("P-1", "2026-01", 8.0), reading("P-1", "2026-02", 7.0)]))
        self.assertEqual(outcomes[0]["status"], "confirmed")
        totals = self.state()["totals"]
        self.assertEqual(len(totals), 1)
        self.assertAlmostEqual(totals[0]["total_dose"], 15.0)
        disp = self.state()["dispositions"][0]
        self.assertEqual(disp["year_key"], 2026)
        self.assertEqual(disp["severity"], "elevated")
        self.assertEqual(disp["investigation_required"], 0)

    def test_retransmitted_batch_reuses_first_result(self):
        self.submit(batch("B1", [reading("P-1", "2026-01", 8.0)]))
        outcomes = self.submit(batch("B1", [reading("P-1", "2026-01", 99.0)]))
        self.assertTrue(outcomes[0]["replayed"])
        self.assertEqual(outcomes[0]["status"], "confirmed")
        # 重传读数不重复计入
        totals = self.state()["totals"]
        self.assertAlmostEqual(totals[0]["total_dose"], 8.0)
        stored = self.repo.get_batch_by_no("B1")
        self.assertEqual(stored["replay_count"], 1)
        self.assertEqual(len(self.state()["readings"]), 1)

    def test_inconsistent_overlapping_reading_suspends_whole_batch(self):
        self.submit(batch("B1", [reading("P-1", "2026-01", 8.0)]))
        outcomes = self.submit(batch("B2", [
            reading("P-1", "2026-01", 12.0), reading("P-2", "2026-01", 3.0)]))
        self.assertEqual(outcomes[0]["status"], "suspended")
        self.assertTrue(all(c["kind"] == "overlap"
                            for c in outcomes[0]["conflicts"]))
        # 整批挂起：同批的其他读数也不得入账
        totals = self.state()["totals"]
        self.assertAlmostEqual(totals[0]["total_dose"], 8.0)
        self.assertEqual(len(totals), 1)
        # 挂起批次重传仍是挂起
        replay = self.submit(batch("B2", [
            reading("P-1", "2026-01", 12.0), reading("P-2", "2026-01", 3.0)]))
        self.assertEqual(replay[0]["status"], "suspended")
        self.assertTrue(replay[0]["replayed"])

    def test_within_batch_conflict_suspends_too(self):
        outcomes = self.submit(batch("B1", [
            reading("P-1", "2026-01", 8.0, source="lab_direct"),
            reading("P-1", "2026-01", 9.5, source="dosimetrist")]))
        self.assertEqual(outcomes[0]["status"], "suspended")
        self.assertEqual(outcomes[0]["conflicts"][0]["kind"], "within_batch")
        self.assertEqual(self.state()["totals"], [])

    def test_concurrent_same_period_merges_by_source_order(self):
        p1 = batch("B1", [reading("P-1", "2026-01", 8.0, source="dosimetrist")],
                   source="dosimetrist")
        p2 = batch("B2", [reading("P-1", "2026-01", 8.0, source="lab_direct")],
                   source="lab_direct")
        result = self.service.submit_batches(
            {"batches": [p1, p2]}, "tech", "dosimetrist")["outcomes"]
        self.assertEqual({o["status"] for o in result}, {"confirmed"})
        readings = [r for r in self.state()["readings"]
                    if r["person_id"] == "P-1" and r["is_active"]]
        self.assertEqual(len(readings), 1)  # 合并不重复计入
        self.assertEqual(readings[0]["source"], "lab_direct")  # 来源优先
        self.assertEqual(set(readings[0]["provenance"]),
                         {"lab_direct", "dosimetrist"})

    def test_correction_and_recalibration_invalidate_and_recompute(self):
        self.submit(batch("B1", [
            reading("P-1", "2026-01", 15.0),
            reading("P-1", "2026-02", 10.0)]))
        before = self.state()["dispositions"][0]
        self.assertEqual(before["investigation_required"], 1)
        old_due = before["report_due_at"]
        # 人员更正：把2月读数改为2
        self.submit(batch("C1", [reading("P-1", "2026-02", 2.0)],
                          batch_type="correction", reason="人员更正"))
        after = self.state()["dispositions"][0]
        self.assertAlmostEqual(after["total_dose"], 17.0)
        self.assertNotEqual(after["report_due_at"], old_due)  # 期限被改写
        self.assertEqual(after["investigation_required"], 0)
        readings = self.state()["readings"]
        inactive = [r for r in readings if not r["is_active"]]
        active = [r for r in readings if r["is_active"]]
        self.assertEqual(len(inactive), 1)
        self.assertEqual(inactive[0]["superseded_by"], active[1]["id"])
        self.assertEqual(active[1]["reading_type"], "correction")
        # 晚到的仪器复校正再次改写
        self.submit(batch("R1", [
            reading("P-1", "2026-01", 19.0, instrument="GM-9")],
            batch_type="recalibration", reason="仪器复校正"))
        final = self.state()["dispositions"][0]
        self.assertAlmostEqual(final["total_dose"], 21.0)
        self.assertEqual(final["severity"], "high")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_correction_releases_follow_up_deadline(self):
        self.submit(batch("B1", [reading("P-1", "2026-01", 31.0)]))
        first = self.state()["dispositions"][0]
        self.assertEqual(first["follow_up_required"], 1)
        self.submit(batch("C1", [reading("P-1", "2026-01", 5.0)],
                          batch_type="correction"))
        second = self.state()["dispositions"][0]
        self.assertEqual(second["follow_up_required"], 0)
        self.assertEqual(second["severity"], "low")
        history = [d for d in self.repo.conn.execute(
            "SELECT * FROM dose_dispositions ORDER BY id").fetchall()]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["is_current"], 0)
        self.assertIsNotNone(history[0]["superseded_at"])

    def test_correction_dropping_all_readings_clears_conclusion(self):
        self.submit(batch("B1", [reading("P-1", "2026-01", 31.0)]))
        # 更正把剂量清零（仍属人员周期）：年度累计为0，结论仍在但级别下降
        self.submit(batch("C1", [reading("P-1", "2026-01", 0.0)],
                          batch_type="correction"))
        disp = self.state()["dispositions"][0]
        self.assertAlmostEqual(disp["total_dose"], 0.0)
        self.assertEqual(disp["investigation_required"], 0)


class RecoveryTest(RecalcBase):
    def test_recover_from_last_confirmed_without_double_count(self):
        self.submit(batch("B1", [reading("P-1", "2026-01", 5.0)]))
        # 模拟B2已入持久收件箱、但处理中途写入失败（仍pending）
        norm = normalize_batch(batch("B2", [reading("P-1", "2026-02", 7.0)]))
        self.repo.enqueue_batches(
            [("B2", json.dumps(norm, ensure_ascii=False,
                                sort_keys=True, default=str))])
        self.assertEqual(self.repo.get_batch_by_no("B2"), None)
        report = self.service.recover_batches({}, "tech", "dosimetrist")
        self.assertEqual(report["checkpoint"], "B1")
        self.assertIn("B2", report["confirmed"])
        self.assertAlmostEqual(self.state()["totals"][0]["total_dose"], 12.0)
        # 再次恢复：没有pending，且已确认批次绝不重复计入
        again = self.service.recover_batches({}, "tech", "dosimetrist")
        self.assertEqual(again["confirmed"], [])
        self.assertAlmostEqual(self.state()["totals"][0]["total_dose"], 12.0)

    def test_recovery_when_batch_landed_but_inbox_not_acknowledged(self):
        norm = normalize_batch(batch("B1", [reading("P-1", "2026-01", 5.0)]))
        self.repo.enqueue_batches(
            [("B1", json.dumps(norm, ensure_ascii=False,
                                sort_keys=True, default=str))])
        # 直接应用决策但不签收收件箱（模拟崩溃点）
        inbox = self.repo.get_pending_inbox()[0]
        active = self.repo.get_active_readings([("P-1", "2026-01")])
        from src.recalc import build_decision
        from src.audit import utc_now
        decision = build_decision(norm, {(r["person_id"], r["period"]): [r]
                                         for r in active}, utc_now())
        self.repo.apply_decision(
            decision, "tech", utc_now(), ANNUAL_INVESTIGATION_LEVEL,
            self.engine._disposition)
        report = self.service.recover_batches({}, "tech", "dosimetrist")
        self.assertIn("B1", report["replayed"])
        self.assertEqual(len(self.state()["readings"]), 1)
        self.assertAlmostEqual(self.state()["totals"][0]["total_dose"], 5.0)


class LegacyBackfillTest(RecalcBase):
    def test_missing_year_key_backfilled_from_measurement_time(self):
        result = self.service.import_legacy(
            {"readings": [
                reading("P-9", "2024-Q4", 6.0,
                        measured_at="2024-11-20T08:00:00+00:00"),
                reading("P-9", "2025-Q1", 9.0,
                        measured_at="2025-02-01T08:00:00+00:00")]},
            "tech", "dosimetrist")
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["missing_year"], 2)
        self.assertEqual(result["totals"], [])  # 缺年度键时不入累计
        missing = self.repo.list_readings_without_year()
        self.assertEqual(len(missing), 2)
        report = self.service.backfill_years({}, "tech", "dosimetrist")
        self.assertEqual(report["backfilled"], 2)
        totals = {(t["person_id"], t["year_key"]): t["total_dose"]
                  for t in self.state()["totals"]}
        self.assertAlmostEqual(totals[("P-9", 2024)], 6.0)
        self.assertAlmostEqual(totals[("P-9", 2025)], 9.0)
        # 回填后不再有缺键数据
        self.assertEqual(self.repo.list_readings_without_year(), [])
        self.assertEqual(derive_year("2024-11-20T08:00:00+00:00"), 2024)

    def test_backfill_twice_is_idempotent(self):
        self.service.import_legacy(
            {"readings": [reading("P-9", "2024-Q4", 6.0,
                                  measured_at="2024-11-20T08:00:00+00:00")]},
            "tech", "dosimetrist")
        first = self.service.backfill_years({}, "tech", "dosimetrist")
        second = self.service.backfill_years({}, "tech", "dosimetrist")
        self.assertEqual((first["backfilled"], second["backfilled"]), (1, 0))


class RecalcPermissionTest(RecalcBase):
    def test_roles_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_batches(
                {"batches": [batch("B1", [reading("P-1", "p", 1.0)])]},
                "v", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.import_legacy({"readings": [reading("a", "b", 1.0)]},
                                       "v", "radiation_officer")
        with self.assertRaises(ValidationError):
            self.service.submit_batches(
                {"batches": [{"batch_no": "X", "source": "lab_direct"}]},
                "t", "dosimetrist")
        with self.assertRaises(ValueError):
            self.service.submit_batches(
                {"batches": [batch("B1", [
                    reading("P-1", "p", 1.0, measured_at="not-a-date")])]},
                "t", "dosimetrist")


if __name__ == "__main__":
    unittest.main()
