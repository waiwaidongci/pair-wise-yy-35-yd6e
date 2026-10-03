from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from .domain import (ensure_role, normalize_severity, require_batch_source,
                     require_dose, require_measured_at, require_number,
                     require_period, require_person, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_SUBMIT_ROLES,
                    BATCH_VIEW_ROLES, CREATE_ROLES, DEFAULT_ANNUAL_THRESHOLD, ENTITY,
                    RECALCULATE_ROLES, RECORD_ROLES, TITLE, VIEW_ROLES, annual_totals,
                    backfill_annual_key, completion_blockers,
                    conclusion_deadline_hours, disposition_kind,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 批次重算链 ----
    @staticmethod
    def _content_hash(source: str, readings: List[Dict[str, Any]]) -> str:
        raw = json.dumps({"source": source, "readings": readings},
                         ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _validate_readings(self, raw_readings: Any, source: str) -> List[Dict[str, Any]]:
        if not isinstance(raw_readings, list) or not raw_readings:
            raise ValidationError("readings必须是非空列表")
        readings: List[Dict[str, Any]] = []
        for r in raw_readings:
            if not isinstance(r, dict):
                raise ValidationError("每条读数必须是对象")
            person = require_person(r.get("person"))
            period = require_period(r.get("period"))
            measured_at = require_measured_at(r.get("measured_at"))
            dose = require_dose(r.get("dose"))
            annual_key = r.get("annual_key")
            if annual_key is None or (isinstance(annual_key, str) and not annual_key.strip()):
                annual_key = backfill_annual_key(measured_at)
            else:
                annual_key = require_text(annual_key, "annual_key", 10)
            external_ref = r.get("external_ref")
            if external_ref is not None:
                external_ref = require_text(external_ref, "external_ref", 100)
            readings.append({
                "external_ref": external_ref, "person": person, "period": period,
                "measured_at": measured_at, "dose": dose, "annual_key": annual_key,
                "source": source,
            })
        return readings

    def _overlap_inconsistent(self, readings: List[Dict[str, Any]]) -> bool:
        """重叠读数（同人员/周期/测量时刻）剂量不一致 → 整批挂起。"""
        persons = set(r["person"] for r in readings)
        existing: List[Dict[str, Any]] = []
        for person in persons:
            existing.extend(self.repository.list_confirmed_readings(person))
        seen: Dict[tuple, Dict[str, Any]] = {}
        for r in readings:
            key = (r["person"], r["period"], r["measured_at"])
            if key in seen:
                if abs(seen[key]["dose"] - r["dose"]) > 1e-9:
                    return True
            else:
                seen[key] = r
            for er in existing:
                if (er["person"] == r["person"] and er["period"] == r["period"]
                        and er["measured_at"] == r["measured_at"]):
                    if abs(er["dose"] - r["dose"]) > 1e-9:
                        return True
        return False

    def _compute_person_data(self, person: str,
                             new_readings: Optional[List[Dict[str, Any]]] = None,
                             threshold: float = DEFAULT_ANNUAL_THRESHOLD) -> Dict[str, Any]:
        """从读数重算某人员的年度累计与处置结论（不持久化）。

        new_readings 与已确认读数合并后重算，旧结论立即失效。
        """
        readings = self.repository.list_confirmed_readings(person)
        if new_readings:
            readings = readings + new_readings
        totals = annual_totals(readings)
        annual_totals_list: List[Dict[str, Any]] = []
        conclusions: List[Dict[str, Any]] = []
        for annual_key, data in totals.items():
            annual_totals_list.append({
                "annual_key": annual_key, "total_dose": data["total_dose"],
                "reading_count": data["reading_count"],
            })
            kind = disposition_kind(data["total_dose"], threshold)
            deadline = conclusion_deadline_hours(data["total_dose"], threshold)
            result = {
                "total_dose": data["total_dose"], "threshold": threshold,
                "reading_count": data["reading_count"], "deadline_hours": deadline,
            }
            conclusions.append({
                "annual_key": annual_key, "period": None, "kind": kind, "result": result,
            })
        return {"annual_totals": annual_totals_list, "conclusions": conclusions}

    def _recompute_person(self, person: str,
                          threshold: float = DEFAULT_ANNUAL_THRESHOLD) -> None:
        """重算某人员的年度累计与处置结论：旧结论立即失效并重算。"""
        data = self._compute_person_data(person, threshold=threshold)
        self.repository.persist_person_data(person, data)

    def _batch_result(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(batch)
        result["payload"] = batch["payload"]
        result["detail"] = batch["detail"]
        return result

    def submit_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        external_ref = require_text(payload.get("external_ref"), "external_ref", 100)
        source = require_batch_source(payload.get("source"), ["dosimeter", "officer", "physicist"])
        readings = self._validate_readings(payload.get("readings"), source)
        content_hash = self._content_hash(source, readings)
        persons = set(r["person"] for r in readings)

        # 同批次重传沿用首次结果
        existing = self.repository.get_batch_by_ref(external_ref)
        if existing is not None:
            if existing["status"] in ("confirmed", "suspended"):
                self.repository.append_audit("batch_duplicate", "batch", existing["id"], actor, {
                    "external_ref": external_ref, "status": existing["status"],
                })
                return self._batch_result(existing)
            # pending/failed → 从最后确认批次恢复重放
            batch_id = existing["id"]
        else:
            batch_id = None

        # 重叠读数不一致 → 整批挂起
        if self._overlap_inconsistent(readings):
            if batch_id is None:
                batch_id = self.repository.create_batch(
                    external_ref, source, readings, content_hash,
                    {"source": source, "readings": readings}, actor, status="suspended")
            else:
                self.repository.update_batch_status(batch_id, "suspended")
            batch = self.repository.get_batch(batch_id)
            self.repository.append_audit("batch_suspended", "batch", batch_id, actor, {
                "external_ref": external_ref, "reason": "overlap_inconsistent",
            })
            return self._batch_result(batch)

        # 写入失败后从最后确认批次恢复：整批在一个事务内确认，重放不重复计入
        if batch_id is None:
            batch_id = self.repository.create_batch(
                external_ref, source, readings, content_hash,
                {"source": source, "readings": readings}, actor, status="pending")
        persons_data = {p: self._compute_person_data(p, new_readings=readings) for p in persons}
        try:
            self.repository.confirm_batch(batch_id, readings, persons_data)
        except Exception as exc:
            self.repository.update_batch_status(batch_id, "failed")
            self.repository.append_audit("batch_failed", "batch", batch_id, actor, {
                "external_ref": external_ref, "error": str(exc),
            })
            raise
        batch = self.repository.get_batch(batch_id)
        self.repository.append_audit("batch_confirmed", "batch", batch_id, actor, {
            "external_ref": external_ref, "reading_count": len(readings),
        })
        return self._batch_result(batch)

    def list_batches(self, role: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, BATCH_VIEW_ROLES)
        return [self._batch_result(b) for b in self.repository.list_batches(status)]

    def get_batch(self, external_ref: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_VIEW_ROLES)
        batch = self.repository.get_batch_by_ref(external_ref)
        if batch is None:
            from .domain import NotFoundError
            raise NotFoundError("批次不存在")
        return self._batch_result(batch)

    def recover_batches(self, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        last = self.repository.last_confirmed_batch()
        if last is None:
            candidates = self.repository.list_batches(status="pending") + \
                         self.repository.list_batches(status="failed")
        else:
            candidates = self.repository.batches_after(last["id"])
        replayed: List[str] = []
        for batch in candidates:
            if batch["status"] in ("confirmed", "suspended"):
                continue
            readings = batch["payload"].get("readings", [])
            if not readings:
                continue
            # 旧数据缺少年度键时按测量时刻回填
            for r in readings:
                if not r.get("annual_key"):
                    r["annual_key"] = backfill_annual_key(r["measured_at"])
                if not r.get("source"):
                    r["source"] = batch["source"]
            persons = set(r["person"] for r in readings)
            persons_data = {p: self._compute_person_data(p, new_readings=readings) for p in persons}
            try:
                self.repository.confirm_batch(batch["id"], readings, persons_data)
                replayed.append(batch["external_ref"])
            except Exception as exc:
                self.repository.update_batch_status(batch["id"], "failed")
                self.repository.append_audit("batch_failed", "batch", batch["id"], actor, {
                    "external_ref": batch["external_ref"], "error": str(exc),
                })
                raise
        self.repository.append_audit("batch_recover", "batch", 0, actor, {"replayed": replayed})
        return {"replayed": replayed}

    def get_annual_totals(self, person: str, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, BATCH_VIEW_ROLES)
        return self.repository.get_annual_totals(person)

    def get_conclusions(self, person: str, role: str,
                        status: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, BATCH_VIEW_ROLES)
        return self.repository.get_conclusions(person, status)

    def recalculate(self, person: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECALCULATE_ROLES)
        actor = require_text(actor, "actor", 100)
        self._recompute_person(person)
        self.repository.append_audit("recalculate", "batch", 0, actor, {"person": person})
        return {"person": person, "recomputed": True}

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
