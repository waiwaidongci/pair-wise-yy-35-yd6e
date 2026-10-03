from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .recalc import derive_year
from .rules import ID_PREFIX, STATES


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
                CREATE TABLE IF NOT EXISTS dose_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    batch_type TEXT NOT NULL DEFAULT 'normal',
                    reason TEXT,
                    status TEXT NOT NULL CHECK(status IN ('confirmed','suspended')),
                    content_hash TEXT NOT NULL,
                    conflict_detail TEXT,
                    replay_count INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS dose_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER REFERENCES dose_batches(id),
                    person_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    measured_at TEXT NOT NULL,
                    year_key INTEGER,
                    dose REAL NOT NULL,
                    instrument_id TEXT,
                    source TEXT NOT NULL,
                    reading_type TEXT NOT NULL DEFAULT 'raw'
                        CHECK(reading_type IN ('raw','correction','recalibration','legacy')),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    superseded_by INTEGER REFERENCES dose_readings(id),
                    provenance TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    legacy_batch_no TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_readings_active_key
                    ON dose_readings(person_id, period) WHERE is_active=1;
                CREATE INDEX IF NOT EXISTS ix_readings_person_year
                    ON dose_readings(person_id, year_key);
                CREATE TABLE IF NOT EXISTS dose_annual_totals (
                    person_id TEXT NOT NULL,
                    year_key INTEGER NOT NULL,
                    total_dose REAL NOT NULL,
                    reading_count INTEGER NOT NULL,
                    updated_batch_id INTEGER REFERENCES dose_batches(id),
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(person_id, year_key)
                );
                CREATE TABLE IF NOT EXISTS dose_dispositions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT NOT NULL,
                    year_key INTEGER NOT NULL,
                    total_dose REAL NOT NULL,
                    investigation_level REAL NOT NULL,
                    severity TEXT NOT NULL,
                    investigation_required INTEGER NOT NULL,
                    follow_up_required INTEGER NOT NULL,
                    deadline_hours INTEGER NOT NULL,
                    report_due_at TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    superseded_at TEXT,
                    batch_id INTEGER REFERENCES dose_batches(id),
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_dispositions_current
                    ON dose_dispositions(person_id, year_key) WHERE is_current=1;
                CREATE TABLE IF NOT EXISTS ingest_inbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','done')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    enqueued_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_inbox_pending_batch
                    ON ingest_inbox(batch_no) WHERE status='pending';
            """)

    @property
    def lock(self):
        return self._lock

    @contextmanager
    def transaction(self):
        with self._lock, self.conn:
            yield self.conn

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

    # ----------------------------------------------------- 重算链持久化

    def get_batch_by_no(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dose_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
        return dict(row) if row else None

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dose_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return dict(row)

    def enqueue_batches(self, pairs: List[Tuple[str, str]]) -> None:
        now = utc_now()
        try:
            with self._lock, self.conn:
                self.conn.executemany(
                    "INSERT INTO ingest_inbox(batch_no, payload, enqueued_at)"
                    " VALUES(?,?,?)",
                    [(batch_no, payload, now) for batch_no, payload in pairs],
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("批次已在处理队列中") from exc

    def get_pending_inbox(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM ingest_inbox WHERE status='pending'"
                " ORDER BY seq LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def last_done_batch_no(self) -> Optional[str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT batch_no FROM ingest_inbox WHERE status='done'"
                " ORDER BY seq DESC LIMIT 1").fetchone()
        return row["batch_no"] if row else None

    def increment_attempt(self, seq: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE ingest_inbox SET attempts=attempts+1 WHERE seq=?",
                (seq,))

    def finish_inbox(self, seq: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE ingest_inbox SET status='done', finished_at=? WHERE seq=?",
                (utc_now(), seq))

    def touch_replay(self, batch_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE dose_batches SET replay_count=replay_count+1 WHERE id=?",
                (batch_id,))

    def get_active_readings(self,
                            keys: List[Tuple[str, str]]
                            ) -> List[Dict[str, Any]]:
        if not keys:
            return []
        clause = " OR ".join("(person_id=? AND period=?)" for _ in keys)
        params: tuple = tuple(v for key in keys for v in key)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM dose_readings WHERE is_active=1 AND ({clause})",
                params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["provenance"] = json.loads(item["provenance"] or "[]")
            result.append(item)
        return result

    def list_readings_without_year(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dose_readings WHERE year_key IS NULL"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_reading_year(self, reading_id: int, year_key: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE dose_readings SET year_key=? WHERE id=?",
                (year_key, reading_id))

    def _recompute_year(self, conn: sqlite3.Connection, person_id: str,
                        year_key: Optional[int], now: str, level: float,
                        disposition_fn: Callable,
                        batch_id: Optional[int]) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT COALESCE(SUM(dose),0) AS total, COUNT(*) AS n"
            " FROM dose_readings WHERE person_id=? AND year_key=? AND is_active=1",
            (person_id, year_key)).fetchone()
        count = int(row["n"])
        if count == 0:  # 年度已无在效读数：旧结论失效，不再生成新结论
            conn.execute(
                "UPDATE dose_dispositions SET is_current=0, superseded_at=?"
                " WHERE person_id=? AND year_key=? AND is_current=1",
                (now, person_id, year_key))
            conn.execute("DELETE FROM dose_annual_totals WHERE person_id=? AND year_key=?",
                         (person_id, year_key))
            return None
        total = float(row["total"])
        conn.execute(
            "INSERT INTO dose_annual_totals(person_id, year_key, total_dose,"
            " reading_count, updated_batch_id, updated_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(person_id, year_key) DO UPDATE SET"
            " total_dose=excluded.total_dose, reading_count=excluded.reading_count,"
            " updated_batch_id=excluded.updated_batch_id,"
            " updated_at=excluded.updated_at, version=version+1",
            (person_id, year_key, round(total, 9), count, batch_id, now))
        old = conn.execute(
            "SELECT * FROM dose_dispositions WHERE person_id=? AND year_key=?"
            " AND is_current=1", (person_id, year_key)).fetchone()
        old_due = dict(old) if old else None
        conn.execute(
            "UPDATE dose_dispositions SET is_current=0, superseded_at=?"
            " WHERE person_id=? AND year_key=? AND is_current=1",
            (now, person_id, year_key))
        disp = disposition_fn(person_id, year_key, total, level, now, batch_id)
        cur = conn.execute(
            "INSERT INTO dose_dispositions(person_id, year_key, total_dose,"
            " investigation_level, severity, investigation_required,"
            " follow_up_required, deadline_hours, report_due_at, batch_id,"
            " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (disp["person_id"], disp["year_key"], disp["total_dose"],
             disp["investigation_level"], disp["severity"],
             disp["investigation_required"], disp["follow_up_required"],
             disp["deadline_hours"], disp["report_due_at"], disp["batch_id"],
             now))
        result = dict(disp)
        result["id"] = int(cur.lastrowid)
        if old_due is not None:  # 期限被改写：记录旧结论失效信息
            result["previous_report_due_at"] = old_due["report_due_at"]
            result["previous_total_dose"] = old_due["total_dose"]
        return result

    def apply_decision(self, decision: Dict[str, Any], actor: str, now: str,
                       level: float, disposition_fn: Callable) -> Dict[str, Any]:
        meta = decision["batch"]
        suspended = decision["status"] == "suspended"
        totals: List[Dict[str, Any]] = []
        dispositions: List[Dict[str, Any]] = []
        superseded: List[Dict[str, Any]] = []
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO dose_batches(batch_no, source, batch_type, reason,"
                " status, content_hash, conflict_detail, created_by,"
                " created_at, confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (meta["batch_no"], meta["source"], meta["batch_type"],
                 meta["reason"], decision["status"], meta["content_hash"],
                 json.dumps(decision["conflicts"], ensure_ascii=False)
                 if suspended else None, actor, now, None if suspended else now))
            batch_id = int(cur.lastrowid)

            if not suspended:
                # 更正/复校正：旧读数先失效，再插入新读数并回填取代指针
                for person_id, period in decision["supersede_keys"]:
                    self.conn.execute(
                        "UPDATE dose_readings SET is_active=0"
                        " WHERE person_id=? AND period=? AND is_active=1",
                        (person_id, period))
                for reading in decision["inserts"]:
                    ins = self.conn.execute(
                        "INSERT INTO dose_readings(batch_id, person_id, period,"
                        " measured_at, year_key, dose, instrument_id, source,"
                        " reading_type, is_active, provenance, created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
                        (batch_id, reading["person_id"], reading["period"],
                         reading["measured_at"], reading["year_key"],
                         reading["dose"], reading.get("instrument_id"),
                         reading["source"], reading["reading_type"],
                         json.dumps(reading["provenance"], ensure_ascii=False),
                         now))
                    if reading["reading_type"] in ("correction", "recalibration"):
                        self.conn.execute(
                            "UPDATE dose_readings SET superseded_by=?"
                            " WHERE person_id=? AND period=? AND is_active=0",
                            (int(ins.lastrowid), reading["person_id"],
                             reading["period"]))
                # 并发同周期提交且读数一致：按来源顺序合并且不重复计入
                for merge in decision["merges"]:
                    self.conn.execute(
                        "UPDATE dose_readings SET provenance=?, source=? WHERE id=?",
                        (json.dumps(merge["provenance"], ensure_ascii=False),
                         merge["winner_source"], merge["winner_id"]))

                for person_id, year_key in decision["affected"]:
                    disp = self._recompute_year(self.conn, person_id, year_key,
                                                now, level, disposition_fn,
                                                batch_id)
                    if disp is not None:
                        totals.append({"person_id": person_id,
                                       "year_key": year_key,
                                       "total_dose": disp["total_dose"]})
                        dispositions.append(disp)
                        if "previous_report_due_at" in disp:
                            superseded.append(
                                {"person_id": person_id, "year_key": year_key,
                                 "previous_report_due_at":
                                     disp["previous_report_due_at"],
                                 "report_due_at": disp["report_due_at"]})
        return {"batch": {"id": batch_id, **meta},
                "suspended": suspended,
                "conflicts": decision["conflicts"],
                "merges": decision["merges"],
                "totals": totals, "dispositions": dispositions,
                "superseded_dispositions": superseded}

    def recompute_affected(self, affected: List[Tuple[str, int]], level: float,
                           disposition_fn: Callable) -> List[Dict[str, Any]]:
        now = utc_now()
        totals: List[Dict[str, Any]] = []
        with self._lock, self.conn:
            for person_id, year_key in affected:
                disp = self._recompute_year(self.conn, person_id, year_key,
                                            now, level, disposition_fn, None)
                if disp is not None:
                    totals.append({"person_id": person_id,
                                   "year_key": year_key,
                                   "total_dose": disp["total_dose"],
                                   "report_due_at": disp["report_due_at"]})
        return totals

    def import_legacy_readings(self, rows: List[Dict[str, Any]], level: float,
                               disposition_fn: Callable) -> Dict[str, Any]:
        now = utc_now()
        inserted = 0
        missing_year = 0
        affected: set = set()
        with self._lock, self.conn:
            for row in rows:
                cur = self.conn.execute(
                    "INSERT INTO dose_readings(person_id, period, measured_at,"
                    " year_key, dose, source, reading_type, is_active,"
                    " provenance, created_at, legacy_batch_no)"
                    " SELECT ?,?,?,?,?,?,'legacy',1,'[]',?,NULL"
                    " WHERE NOT EXISTS(SELECT 1 FROM dose_readings"
                    " WHERE person_id=? AND period=? AND is_active=1)",
                    (row["person_id"], row["period"], row["measured_at"],
                     row["year_key"], row["dose"], row["source"], now,
                     row["person_id"], row["period"]))
                if cur.rowcount:
                    inserted += 1
                    if row["year_key"] is None:
                        missing_year += 1
                    else:
                        affected.add((row["person_id"], row["year_key"]))
            totals: List[Dict[str, Any]] = []
            for person_id, year_key in sorted(affected):
                disp = self._recompute_year(self.conn, person_id, year_key,
                                            now, level, disposition_fn, None)
                if disp is not None:
                    totals.append({"person_id": person_id,
                                   "year_key": year_key,
                                   "total_dose": disp["total_dose"]})
        return {"inserted": inserted, "missing_year": missing_year,
                "totals": totals}

    def list_dose_state(self) -> Dict[str, Any]:
        with self._lock:
            batches = [dict(r) for r in self.conn.execute(
                "SELECT * FROM dose_batches ORDER BY id").fetchall()]
            totals = [dict(r) for r in self.conn.execute(
                "SELECT * FROM dose_annual_totals ORDER BY person_id, year_key"
            ).fetchall()]
            dispositions = [dict(r) for r in self.conn.execute(
                "SELECT * FROM dose_dispositions WHERE is_current=1"
                " ORDER BY person_id, year_key").fetchall()]
            readings = [dict(r) for r in self.conn.execute(
                "SELECT id, batch_id, person_id, period, measured_at, year_key,"
                " dose, source, reading_type, is_active, superseded_by,"
                " provenance FROM dose_readings ORDER BY id").fetchall()]
        for batch in batches:
            batch["conflict_detail"] = json.loads(
                batch["conflict_detail"] or "[]")
        for reading in readings:
            reading["provenance"] = json.loads(reading["provenance"] or "[]")
        return {"batches": batches, "readings": readings, "totals": totals,
                "dispositions": dispositions}

    def close(self) -> None:
        with self._lock:
            self.conn.close()
