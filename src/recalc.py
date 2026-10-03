from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from .audit import utc_now
from .domain import ValidationError, require_number, require_text
from .rules import escalation_required, response_deadline_hours

if TYPE_CHECKING:  # 避免运行期 repository <-> recalc 循环导入
    from .repository import Repository

# 批次类型：常规补发、人员更正、仪器复校正
BATCH_TYPES = ("normal", "correction", "recalibration")
# 来源顺序：排在越前越优先，同一人员周期并发提交时按此序合并
SOURCE_ORDER = ("lab_direct", "dosimetrist", "radiation_officer", "manual")
# 年度调查水平（mSv）与随访触发倍数
ANNUAL_INVESTIGATION_LEVEL = 20.0
FOLLOW_UP_RATIO = 1.5
DOSE_EPS = 1e-9


# ---------------------------------------------------------------- 纯函数规则

def parse_measured_at(value: str) -> datetime:
    text = require_text(value, "measured_at", 40)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("measured_at必须是ISO-8601时间") from exc


def derive_year(measured_at: str) -> int:
    """旧数据缺少年度键时，按测量时刻回填年度键。"""
    return parse_measured_at(measured_at).year


def source_rank(source: str) -> Tuple[int, str]:
    try:
        return SOURCE_ORDER.index(source), source
    except ValueError:
        return len(SOURCE_ORDER), source


def ordered_sources(sources: List[str]) -> List[str]:
    return sorted(dict.fromkeys(sources), key=source_rank)


def canonical_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def band_severity(ratio: float) -> str:
    if ratio >= FOLLOW_UP_RATIO:
        return "critical"
    if ratio >= 1.0:
        return "high"
    if ratio >= 0.5:
        return "elevated"
    return "low"


def build_disposition(person_id: str, year_key: int, total_dose: float,
                      threshold: float, now: str,
                      batch_id: Optional[int]) -> Dict[str, Any]:
    """由年度累计推导处置结论：调查、随访与报告期限。"""
    ratio = total_dose / threshold if threshold > 0 else 0.0
    severity = band_severity(ratio)
    investigation = bool(escalation_required(severity, total_dose, threshold))
    follow_up = ratio >= FOLLOW_UP_RATIO
    deadline_hours = response_deadline_hours(severity, total_dose, threshold)
    base = datetime.fromisoformat(now)
    due = (base + timedelta(hours=deadline_hours)).replace(microsecond=0)
    return {
        "person_id": person_id, "year_key": year_key,
        "total_dose": round(total_dose, 9),
        "investigation_level": threshold,
        "severity": severity,
        "investigation_required": 1 if investigation else 0,
        "follow_up_required": 1 if follow_up else 0,
        "deadline_hours": deadline_hours,
        "report_due_at": due.isoformat(),
        "batch_id": batch_id,
    }


# ------------------------------------------------------- 批次规范化与冲突判定

def normalize_batch(payload: Dict[str, Any]) -> Dict[str, Any]:
    batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
    source = require_text(payload.get("source"), "source", 100)
    batch_type = payload.get("batch_type", "normal")
    if batch_type not in BATCH_TYPES:
        raise ValueError("batch_type必须是normal/correction/recalibration")
    reason = payload.get("reason")
    if reason is not None:
        reason = require_text(reason, "reason", 500)
    raw_readings = payload.get("readings")
    if not isinstance(raw_readings, list) or not raw_readings:
        raise ValidationError("readings必须是非空列表")
    readings: List[Dict[str, Any]] = []
    for item in raw_readings:
        if not isinstance(item, dict):
            raise ValidationError("reading必须是对象")
        person_id = require_text(item.get("person_id"), "person_id", 100)
        period = require_text(item.get("period"), "period", 40)
        measured_at = parse_measured_at(item.get("measured_at")).replace(
            microsecond=0).isoformat()
        dose = require_number(item.get("dose"), "dose", 0.0)
        reading_source = item.get("source") or source
        reading_source = require_text(reading_source, "source", 100)
        readings.append({
            "person_id": person_id, "period": period,
            "measured_at": measured_at, "year_key": derive_year(measured_at),
            "dose": dose,
            "instrument_id": item.get("instrument_id"),
            "source": reading_source,
            "reading_type": batch_type if batch_type != "normal" else "raw",
            "provenance": [reading_source],
        })
    content_hash = canonical_hash({
        "batch_no": batch_no, "source": source, "batch_type": batch_type,
        "readings": [{k: r[k] for k in
                      ("person_id", "period", "measured_at", "dose", "source")}
                     for r in readings],
    })
    return {"batch_no": batch_no, "source": source, "batch_type": batch_type,
            "reason": reason, "readings": readings, "content_hash": content_hash}


def build_decision(norm: Dict[str, Any],
                   active_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]],
                   now: str) -> Dict[str, Any]:
    """根据库内在效读数判定：整批挂起 / 合并 / 更正取代。结果可一次性落库。"""
    batch_no = norm["batch_no"]
    # 批内先按键折叠：等值去重（按来源顺序），不等即整批挂起
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for reading in norm["readings"]:
        grouped.setdefault((reading["person_id"], reading["period"]), []).append(
            reading)
    conflicts: List[Dict[str, Any]] = []
    collapsed: List[Dict[str, Any]] = []
    for (person_id, period), rows in grouped.items():
        doses = [r["dose"] for r in rows]
        if max(doses) - min(doses) > DOSE_EPS:
            conflicts.append({"person_id": person_id, "period": period,
                              "kind": "within_batch",
                              "doses": sorted(set(doses))})
            continue
        first = min(rows, key=lambda r: source_rank(r["source"]))
        first = dict(first)
        first["provenance"] = ordered_sources(
            [s for r in rows for s in r["provenance"]])
        collapsed.append(first)

    inserts: List[Dict[str, Any]] = []
    supersede_keys: List[Tuple[str, str]] = []
    merges: List[Dict[str, Any]] = []
    affected = set()

    for reading in collapsed:  # 与库内重叠读数比对
        key = (reading["person_id"], reading["period"])
        affected.add((reading["person_id"], reading["year_key"]))
        existing = active_by_key.get(key, [])
        if norm["batch_type"] in ("correction", "recalibration"):
            # 更正/复校正直接取代旧读数，随后旧结论失效重算
            supersede_keys.append(key)
            for row in existing:
                affected.add((row["person_id"], row["year_key"]))
            inserts.append(reading)
            continue
        if not existing:
            inserts.append(reading)
            continue
        current = existing[0]
        if abs(current["dose"] - reading["dose"]) <= DOSE_EPS:
            provenance = ordered_sources(
                list(current["provenance"]) + reading["provenance"])
            merges.append({"winner_id": current["id"],
                           "provenance": provenance,
                           "winner_source": provenance[0],
                           "incoming": reading})
        else:
            conflicts.append({"person_id": reading["person_id"],
                              "period": reading["period"],
                              "kind": "overlap",
                              "stored_dose": current["dose"],
                              "incoming_dose": reading["dose"],
                              "stored_source": current["source"]})

    decision = {
        "batch": {"batch_no": batch_no, "source": norm["source"],
                  "batch_type": norm["batch_type"], "reason": norm["reason"],
                  "content_hash": norm["content_hash"]},
        "status": "suspended" if conflicts else "confirmed",
        "conflicts": conflicts,
        "inserts": inserts if not conflicts else [],
        "supersede_keys": supersede_keys if not conflicts else [],
        "merges": merges if not conflicts else [],
        "affected": sorted(affected),
    }
    return decision


# ----------------------------------------------------------------- 重算链引擎

class RecalcEngine:
    """编排 收件箱 -> 批次 -> 读数 -> 年度累计 -> 处置结论 的重算链。"""

    def __init__(self, repository: "Repository",
                 investigation_level: float = ANNUAL_INVESTIGATION_LEVEL):
        self.repository = repository
        self.investigation_level = investigation_level

    # 批次补发：同批次重传沿用首次结果，新批次入持久收件箱后顺序处理
    def submit_batches(self, payloads: List[Dict[str, Any]],
                       actor: str) -> Dict[str, Any]:
        normalized = [normalize_batch(p) for p in payloads]
        outcomes: List[Dict[str, Any]] = []
        to_enqueue: List[Tuple[str, str]] = []
        for norm in normalized:
            existing = self.repository.get_batch_by_no(norm["batch_no"])
            if existing is not None:  # 同批次重传：沿用首次结果，不重复计入
                outcomes.append(self._replay_outcome(existing, actor))
            else:
                to_enqueue.append(
                    (norm["batch_no"], json.dumps(norm, ensure_ascii=False,
                                                  sort_keys=True, default=str)))
        if to_enqueue:
            self.repository.enqueue_batches(to_enqueue)
            outcomes.extend(self._drain(actor))
        order = {norm["batch_no"]: i for i, norm in enumerate(normalized)}
        outcomes.sort(key=lambda o: order[o["batch_no"]])
        return {"outcomes": outcomes}

    def recover(self, actor: str) -> Dict[str, Any]:
        """写入失败后从最后确认批次之后重放；已确认批次幂等跳过。"""
        report = {"checkpoint": self.repository.last_done_batch_no(),
                  "replayed": [], "confirmed": [], "suspended": []}
        self._drain(actor, report)
        return report

    def _drain(self, actor: str, report: Optional[Dict[str, Any]] = None
               ) -> List[Dict[str, Any]]:
        outcomes: List[Dict[str, Any]] = []
        while True:
            pending = self.repository.get_pending_inbox(limit=1)
            if not pending:
                break
            outcomes.append(self._process_one(pending[0], actor, report))
        return outcomes

    def _process_one(self, inbox_row: Dict[str, Any], actor: str,
                     report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        batch_no = inbox_row["batch_no"]
        with self.repository.lock:
            self.repository.increment_attempt(inbox_row["seq"])
            existing = self.repository.get_batch_by_no(batch_no)
            if existing is not None:
                # 崩溃恢复窗口：批次已落库、收件箱未签收，幂等补签收
                self.repository.finish_inbox(inbox_row["seq"])
                outcome = self._replay_outcome(existing, actor)
            else:
                norm = json.loads(inbox_row["payload"])
                keys = [(r["person_id"], r["period"])
                        for r in norm["readings"]]
                active = self.repository.get_active_readings(keys)
                active_by_key: Dict[Tuple[str, str],
                                    List[Dict[str, Any]]] = {}
                for row in active:
                    active_by_key.setdefault(
                        (row["person_id"], row["period"]), []).append(row)
                decision = build_decision(norm, active_by_key, utc_now())
                summary = self.repository.apply_decision(
                    decision, actor, utc_now(), self.investigation_level,
                    self._disposition)
                self.repository.finish_inbox(inbox_row["seq"])
                outcome = self._audit_apply(summary, actor)
            if report is not None:
                bucket = ("replayed" if outcome.get("replayed")
                          else outcome["status"])
                report.setdefault(bucket, []).append(batch_no)
        return outcome

    def _replay_outcome(self, batch: Dict[str, Any], actor: str) -> Dict[str, Any]:
        self.repository.touch_replay(batch["id"])
        self.repository.append_audit("batch_replayed", "dose_batch",
                                     batch["id"], actor,
                                     {"batch_no": batch["batch_no"],
                                      "status": batch["status"]})
        return {"batch_no": batch["batch_no"], "status": batch["status"],
                "replayed": True, "conflicts": json.loads(
                    batch["conflict_detail"] or "[]")}

    def _audit_apply(self, summary: Dict[str, Any], actor: str) -> Dict[str, Any]:
        batch = summary["batch"]
        if summary["suspended"]:
            self.repository.append_audit("batch_suspended", "dose_batch",
                                         batch["id"], actor,
                                         {"batch_no": batch["batch_no"],
                                          "conflicts": summary["conflicts"]})
        else:
            self.repository.append_audit("batch_confirmed", "dose_batch",
                                         batch["id"], actor,
                                         {"batch_no": batch["batch_no"],
                                          "batch_type": batch["batch_type"],
                                          "totals": summary["totals"],
                                          "superseded_dispositions":
                                              summary["superseded_dispositions"]})
        return {"batch_no": batch["batch_no"],
                "status": "suspended" if summary["suspended"] else "confirmed",
                "replayed": False,
                "conflicts": summary["conflicts"],
                "totals": summary["totals"],
                "dispositions": summary["dispositions"]}

    def _disposition(self, person_id, year_key, total, level, now, batch_id):
        return build_disposition(person_id, year_key, total, level, now,
                                 batch_id)

    # 旧数据缺年度键：按测量时刻回填，再重算受影响年度
    def import_legacy(self, readings: List[Dict[str, Any]],
                      actor: str) -> Dict[str, Any]:
        rows = []
        for item in readings:
            person_id = require_text(item.get("person_id"), "person_id", 100)
            period = require_text(item.get("period"), "period", 40)
            measured_at = parse_measured_at(item.get("measured_at")).replace(
                microsecond=0).isoformat()
            dose = require_number(item.get("dose"), "dose", 0.0)
            source = require_text(item.get("source", "manual"), "source", 100)
            year_value = item.get("year_key")
            year_key = int(year_value) if year_value is not None else None
            rows.append({"person_id": person_id, "period": period,
                         "measured_at": measured_at, "year_key": year_key,
                         "dose": dose, "source": source})
        result = self.repository.import_legacy_readings(
            rows, self.investigation_level, self._disposition)
        self.repository.append_audit("legacy_import", "dose_reading", 0, actor,
                                     {"inserted": result["inserted"]})
        return result

    def backfill_year_keys(self, actor: str) -> Dict[str, Any]:
        fixed = self.repository.list_readings_without_year()
        affected = set()
        for row in fixed:  # 年度键一律由测量时刻推导
            year_key = derive_year(row["measured_at"])
            self.repository.set_reading_year(row["id"], year_key)
            affected.add((row["person_id"], year_key))
        totals = self.repository.recompute_affected(
            sorted(affected), self.investigation_level, self._disposition)
        self.repository.append_audit("year_key_backfill", "dose_reading", 0,
                                     actor, {"backfilled": len(fixed)})
        return {"backfilled": len(fixed), "totals": totals}
