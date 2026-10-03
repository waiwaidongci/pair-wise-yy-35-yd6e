from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import BATCH_STATUSES, CONCLUSION_STATUSES, ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        batch_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in BATCH_STATUSES)
        conclusion_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in CONCLUSION_STATUSES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_ref TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ({batch_statuses})),
                    content_hash TEXT NOT NULL,
                    reading_count INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL DEFAULT '{{}}',
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    detail TEXT NOT NULL DEFAULT '{{}}'
                );
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    external_ref TEXT,
                    person TEXT NOT NULL,
                    period TEXT NOT NULL,
                    measured_at TEXT NOT NULL,
                    dose REAL NOT NULL,
                    annual_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, external_ref)
                );
                CREATE INDEX IF NOT EXISTS idx_readings_person_period
                    ON readings(person, period);
                CREATE INDEX IF NOT EXISTS idx_readings_person_annual
                    ON readings(person, annual_key);
                CREATE TABLE IF NOT EXISTS annual_totals (
                    person TEXT NOT NULL,
                    annual_key TEXT NOT NULL,
                    total_dose REAL NOT NULL,
                    reading_count INTEGER NOT NULL,
                    computed_at TEXT NOT NULL,
                    PRIMARY KEY(person, annual_key)
                );
                CREATE TABLE IF NOT EXISTS conclusions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person TEXT NOT NULL,
                    annual_key TEXT NOT NULL,
                    period TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'valid'
                        CHECK(status IN ({conclusion_statuses})),
                    result TEXT NOT NULL,
                    computed_at TEXT NOT NULL,
                    invalidated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_conclusions_person
                    ON conclusions(person, annual_key, status);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 批次重算链 ----
    @staticmethod
    def _batch(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["detail"] = json.loads(item["detail"])
        return item

    def create_batch(self, external_ref: str, source: str, readings: List[Dict[str, Any]],
                     content_hash: str, payload: Dict[str, Any], actor: str,
                     status: str = "pending") -> int:
        now = utc_now()
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO batches(external_ref, source, status, content_hash,
                       reading_count, payload, submitted_by, submitted_at, confirmed_at, detail)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (external_ref, source, status, content_hash, len(readings),
                     payload_json, actor, now, None, "{}"),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("批次external_ref已存在") from exc

    def update_batch_status(self, batch_id: int, status: str,
                            confirmed_at: Optional[str] = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE batches SET status=?, confirmed_at=? WHERE id=?",
                (status, confirmed_at, batch_id),
            )

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return self._batch(row)

    def get_batch_by_ref(self, external_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE external_ref=?", (external_ref,)
            ).fetchone()
        return self._batch(row) if row else None

    def list_batches(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM batches"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._batch(row) for row in rows]

    def last_confirmed_batch(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE status='confirmed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._batch(row) if row else None

    def batches_after(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batches WHERE id>? ORDER BY id", (batch_id,)
            ).fetchall()
        return [self._batch(row) for row in rows]

    def add_readings(self, batch_id: int, readings: List[Dict[str, Any]]) -> None:
        now = utc_now()
        with self._lock, self.conn:
            for r in readings:
                self.conn.execute(
                    """INSERT INTO readings(batch_id, external_ref, person, period,
                       measured_at, dose, annual_key, source, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (batch_id, r.get("external_ref"), r["person"], r["period"],
                     r["measured_at"], r["dose"], r["annual_key"], r["source"], now),
                )

    def confirm_batch(self, batch_id: int, readings: List[Dict[str, Any]],
                      persons_data: Dict[str, Dict[str, Any]]) -> None:
        """单事务确认批次：写入读数、年度累计、处置结论并更新批次状态。

        整批在一个事务内确认，失败则全部回滚，重放不重复计入。
        """
        now = utc_now()
        with self._lock, self.conn:
            for r in readings:
                self.conn.execute(
                    """INSERT INTO readings(batch_id, external_ref, person, period,
                       measured_at, dose, annual_key, source, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (batch_id, r.get("external_ref"), r["person"], r["period"],
                     r["measured_at"], r["dose"], r["annual_key"], r["source"], now),
                )
            self.conn.execute(
                "UPDATE batches SET status='confirmed', confirmed_at=? WHERE id=?",
                (now, batch_id),
            )
            for person, data in persons_data.items():
                for total in data["annual_totals"]:
                    self.conn.execute(
                        """INSERT INTO annual_totals(person, annual_key, total_dose,
                           reading_count, computed_at)
                           VALUES(?,?,?,?,?)
                           ON CONFLICT(person, annual_key) DO UPDATE SET
                             total_dose=excluded.total_dose,
                             reading_count=excluded.reading_count,
                             computed_at=excluded.computed_at""",
                        (person, total["annual_key"], total["total_dose"],
                         total["reading_count"], now),
                    )
                for conclusion in data["conclusions"]:
                    self.conn.execute(
                        """UPDATE conclusions SET status='invalidated', invalidated_at=?
                           WHERE person=? AND annual_key=? AND status='valid'""",
                        (now, person, conclusion["annual_key"]),
                    )
                    result_json = json.dumps(conclusion["result"], ensure_ascii=False, sort_keys=True)
                    self.conn.execute(
                        """INSERT INTO conclusions(person, annual_key, period, kind, status,
                           result, computed_at, invalidated_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (person, conclusion["annual_key"], conclusion.get("period"),
                         conclusion["kind"], "valid", result_json, now, None),
                    )

    def list_confirmed_readings(self, person: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.* FROM readings r
                   JOIN batches b ON r.batch_id = b.id
                   WHERE b.status='confirmed' AND r.person=?
                   ORDER BY r.id""",
                (person,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_all_confirmed_readings(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.* FROM readings r
                   JOIN batches b ON r.batch_id = b.id
                   WHERE b.status='confirmed'
                   ORDER BY r.id""",
            ).fetchall()
        return [dict(row) for row in rows]

    def persist_person_data(self, person: str, data: Dict[str, Any]) -> None:
        """单事务持久化某人员的年度累计与处置结论。"""
        now = utc_now()
        with self._lock, self.conn:
            for total in data["annual_totals"]:
                self.conn.execute(
                    """INSERT INTO annual_totals(person, annual_key, total_dose,
                       reading_count, computed_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(person, annual_key) DO UPDATE SET
                         total_dose=excluded.total_dose,
                         reading_count=excluded.reading_count,
                         computed_at=excluded.computed_at""",
                    (person, total["annual_key"], total["total_dose"],
                     total["reading_count"], now),
                )
            for conclusion in data["conclusions"]:
                self.conn.execute(
                    """UPDATE conclusions SET status='invalidated', invalidated_at=?
                       WHERE person=? AND annual_key=? AND status='valid'""",
                    (now, person, conclusion["annual_key"]),
                )
                result_json = json.dumps(conclusion["result"], ensure_ascii=False, sort_keys=True)
                self.conn.execute(
                    """INSERT INTO conclusions(person, annual_key, period, kind, status,
                       result, computed_at, invalidated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (person, conclusion["annual_key"], conclusion.get("period"),
                     conclusion["kind"], "valid", result_json, now, None),
                )

    def upsert_annual_total(self, person: str, annual_key: str,
                            total_dose: float, reading_count: int) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO annual_totals(person, annual_key, total_dose, reading_count, computed_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(person, annual_key) DO UPDATE SET
                     total_dose=excluded.total_dose,
                     reading_count=excluded.reading_count,
                     computed_at=excluded.computed_at""",
                (person, annual_key, total_dose, reading_count, now),
            )

    def get_annual_totals(self, person: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM annual_totals WHERE person=? ORDER BY annual_key",
                (person,),
            ).fetchall()
        return [dict(row) for row in rows]

    def invalidate_conclusions(self, person: str, annual_key: str) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE conclusions SET status='invalidated', invalidated_at=?
                   WHERE person=? AND annual_key=? AND status='valid'""",
                (now, person, annual_key),
            )

    def add_conclusion(self, person: str, annual_key: str, period: Optional[str],
                       kind: str, result: Dict[str, Any]) -> int:
        now = utc_now()
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO conclusions(person, annual_key, period, kind, status,
                   result, computed_at, invalidated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (person, annual_key, period, kind, "valid", result_json, now, None),
            )
            return int(cur.lastrowid)

    def get_conclusions(self, person: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM conclusions WHERE person=?"
        params: tuple = (person,)
        if status:
            sql += " AND status=?"
            params = (person, status)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item["result"])
            result.append(item)
        return result

    def close(self) -> None:
        with self._lock:
            self.conn.close()
