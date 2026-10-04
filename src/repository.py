import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError
from . import rules


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS avoidance_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    command_ref TEXT NOT NULL,
                    instruction TEXT,
                    status TEXT NOT NULL,
                    coordination_ref TEXT,
                    risk_level TEXT,
                    issued_by TEXT NOT NULL,
                    issued_role TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    ended_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(item_id, attempt),
                    UNIQUE(item_id, command_ref),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS command_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    command_id INTEGER,
                    command_ref TEXT NOT NULL,
                    coordination_ref TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    received_by TEXT,
                    received_role TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id),
                    FOREIGN KEY(command_id) REFERENCES avoidance_commands(id)
                );
                CREATE INDEX IF NOT EXISTS idx_commands_item ON avoidance_commands(item_id, attempt);
                CREATE INDEX IF NOT EXISTS idx_receipts_command ON command_receipts(command_id);
                """
            )
            self._create_conditional_indexes(conn)
        finally:
            conn.close()

    def _create_conditional_indexes(self, conn):
        # 同一接近事件同时只能挂一条未确认指令（数据库级保证，崩溃/并发都成立）
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_command_single_open "
            "ON avoidance_commands(item_id) WHERE status='pending'"
        )
        # 一个协调编号全局只能确认一次，重复回执不会二次完成指令
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_command_coordination_once "
            "ON avoidance_commands(coordination_ref) WHERE coordination_ref IS NOT NULL"
        )

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _row_to_command(self, row):
        return None if row is None else dict(row)

    def issue_command(self, item_id, command_ref, instruction, actor, role, expected_version):
        """发出规避指令：记录为待确认；同一事件只能存在一条未确认指令。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            existing_pending = conn.execute(
                "SELECT id FROM avoidance_commands WHERE item_id=? AND status=?",
                (item_id, rules.COMMAND_PENDING),
            ).fetchone()
            if existing_pending is not None:
                raise ConflictError("pending_command_exists", "同一接近事件已有未确认指令，不能重复发起")
            if row["status"] != "coordinating":
                raise DomainError("invalid_state", "当前状态 %s 不允许发起规避指令" % row["status"])
            payload = json.loads(row["payload"])
            attempt = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt FROM avoidance_commands WHERE item_id=?",
                (item_id,),
            ).fetchone()["attempt"]
            risk_level = (payload.get("assessment") or {}).get("level")
            timestamp = now_iso()
            try:
                cur = conn.execute(
                    "INSERT INTO avoidance_commands(item_id,attempt,command_ref,instruction,status,"
                    "risk_level,issued_by,issued_role,issued_at,version) VALUES(?,?,?,?,?,?,?,?,?,1)",
                    (item_id, attempt, command_ref, instruction, rules.COMMAND_PENDING,
                     risk_level, actor, role, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                message = str(exc)
                if "idx_command_single_open" in message:
                    raise ConflictError("pending_command_exists", "同一接近事件已有未确认指令，不能重复发起")
                raise ConflictError("duplicate_command_ref", "该指令编号已经存在")
            command_id = cur.lastrowid
            new_payload = dict(payload)
            new_payload["command_ref"] = command_ref
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status='executing',version=?,payload=?,updated_at=? WHERE id=?",
                (version, canonical_json(new_payload), timestamp, item_id),
            )
            event = {"command_id": command_id, "attempt": attempt, "command_ref": command_ref}
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "execute", actor, role, canonical_json(event), timestamp),
            )
            self.append_audit(conn, item_id, "command_issued", actor, role, event)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def reissue_command(self, item_id, command_ref, instruction, actor, role, expected_version):
        """确认前重新发起：旧指令作废（被取代），新指令挂为唯一的待确认记录。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            if row["status"] != "executing":
                raise DomainError("invalid_state", "只有待确认阶段才能重新发起指令")
            old = conn.execute(
                "SELECT * FROM avoidance_commands WHERE item_id=? AND status=? ORDER BY attempt DESC LIMIT 1",
                (item_id, rules.COMMAND_PENDING),
            ).fetchone()
            if old is None:
                raise DomainError("no_pending_command", "没有可重新发起的待确认指令")
            timestamp = now_iso()
            conn.execute(
                "UPDATE avoidance_commands SET status=?,ended_at=?,version=version+1 WHERE id=?",
                (rules.COMMAND_SUPERSEDED, timestamp, old["id"]),
            )
            attempt = int(old["attempt"]) + 1
            payload = json.loads(row["payload"])
            risk_level = (payload.get("assessment") or {}).get("level")
            try:
                cur = conn.execute(
                    "INSERT INTO avoidance_commands(item_id,attempt,command_ref,instruction,status,"
                    "risk_level,issued_by,issued_role,issued_at,version) VALUES(?,?,?,?,?,?,?,?,?,1)",
                    (item_id, attempt, command_ref, instruction, rules.COMMAND_PENDING,
                     risk_level, actor, role, timestamp),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_command_ref", "该指令编号已经存在")
            command_id = cur.lastrowid
            new_payload = dict(payload)
            new_payload["command_ref"] = command_ref
            conn.execute(
                "UPDATE items SET version=?,payload=?,updated_at=? WHERE id=?",
                (int(row["version"]) + 1, canonical_json(new_payload), timestamp, item_id),
            )
            supersede_event = {
                "command_id": old["id"], "attempt": old["attempt"],
                "command_ref": old["command_ref"], "superseded_by": command_ref,
            }
            self.append_audit(conn, item_id, "command_superseded", actor, role, supersede_event)
            event = {"command_id": command_id, "attempt": attempt, "command_ref": command_ref}
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "reissue_command", actor, role, canonical_json(event), timestamp),
            )
            self.append_audit(conn, item_id, "command_issued", actor, role, event)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def cancel_command(self, item_id, actor, role, expected_version, reason):
        """撤销待确认指令：指令记为已撤销，事件回到 coordinating 等待重新发起。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            if row["status"] != "executing":
                raise DomainError("invalid_state", "只有待确认阶段才能撤销指令")
            pending = conn.execute(
                "SELECT * FROM avoidance_commands WHERE item_id=? AND status=? ORDER BY attempt DESC LIMIT 1",
                (item_id, rules.COMMAND_PENDING),
            ).fetchone()
            if pending is None:
                raise DomainError("no_pending_command", "没有可撤销的待确认指令")
            timestamp = now_iso()
            conn.execute(
                "UPDATE avoidance_commands SET status=?,ended_at=?,version=version+1 WHERE id=?",
                (rules.COMMAND_CANCELLED, timestamp, pending["id"]),
            )
            payload = json.loads(row["payload"])
            new_payload = dict(payload)
            new_payload["command_cancellation"] = {"reason": reason, "cancelled_by": actor}
            new_payload.pop("command_ref", None)
            conn.execute(
                "UPDATE items SET status='coordinating',version=?,payload=?,updated_at=? WHERE id=?",
                (int(row["version"]) + 1, canonical_json(new_payload), timestamp, item_id),
            )
            event = {"command_id": pending["id"], "command_ref": pending["command_ref"], "reason": reason}
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "cancel_command", actor, role, canonical_json(event), timestamp),
            )
            self.append_audit(conn, item_id, "command_cancelled", actor, role, event)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def apply_revision(self, item_id, revision, actor, role, expected_version, reassess_fn):
        """记录观测修订；若风险等级改变且指令仍待确认，指令作废、事件退回 assessed 重新评估。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            status = row["status"]
            if status not in {"pending", "assessed", "coordinating", "executing"}:
                raise DomainError("invalid_state", "当前状态 %s 不允许记录观测修订" % status)
            payload = json.loads(row["payload"])
            previous_level = (payload.get("assessment") or {}).get("level")
            new_payload = dict(payload)
            new_payload.setdefault("revisions", []).append(revision)
            new_payload["miss_distance_m"] = revision["miss_distance_m"]
            new_payload["covariance_m"] = revision["covariance_m"]
            new_assessment = reassess_fn(new_payload)
            new_payload["assessment"] = new_assessment
            new_level = new_assessment.get("level")
            level_changed = previous_level is not None and previous_level != new_level
            timestamp = now_iso()
            voided = None
            if level_changed:
                pending = conn.execute(
                    "SELECT * FROM avoidance_commands WHERE item_id=? AND status=? ORDER BY attempt DESC LIMIT 1",
                    (item_id, rules.COMMAND_PENDING),
                ).fetchone()
                if pending is not None:
                    conn.execute(
                        "UPDATE avoidance_commands SET status=?,ended_at=?,version=version+1 WHERE id=?",
                        (rules.COMMAND_VOIDED, timestamp, pending["id"]),
                    )
                    new_payload.pop("command_ref", None)
                    status = "assessed"
                    voided = {
                        "command_id": pending["id"], "attempt": pending["attempt"],
                        "command_ref": pending["command_ref"],
                        "previous_level": previous_level, "new_level": new_level,
                    }
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (status, int(row["version"]) + 1, canonical_json(new_payload), timestamp, item_id),
            )
            event = {"revision": revision}
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "report_revision", actor, role, canonical_json(event), timestamp),
            )
            self.append_audit(conn, item_id, "report_revision", actor, role, event)
            if voided is not None:
                self.append_audit(conn, item_id, "command_voided", actor, role, voided)
            conn.execute("COMMIT")
            return self.get_item(item_id), voided
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def ingest_receipt(self, item_id, receipt, actor, role):
        """接收外部回执：带协调编号的首次回执把待确认指令置为确认完成；重复/晚到只记录不生效。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            command_ref = receipt["command_ref"]
            coordination_ref = receipt["coordination_ref"]
            command = conn.execute(
                "SELECT * FROM avoidance_commands WHERE item_id=? AND command_ref=? ORDER BY attempt DESC LIMIT 1",
                (item_id, command_ref),
            ).fetchone()
            timestamp = now_iso()
            disposition = "unmatched"
            result = {"matched": False, "duplicate": False, "confirmed": False}

            if command is not None:
                clash = conn.execute(
                    "SELECT id FROM avoidance_commands WHERE coordination_ref=? AND id<>?",
                    (coordination_ref, command["id"]),
                ).fetchone()
                if clash is not None:
                    raise ConflictError(
                        "coordination_ref_conflict",
                        "协调编号 %s 已属于另一条指令" % coordination_ref,
                    )
                prior = conn.execute(
                    "SELECT * FROM command_receipts WHERE command_id=? AND coordination_ref=?",
                    (command["id"], coordination_ref),
                ).fetchone()
                if prior is not None or command["status"] != rules.COMMAND_PENDING:
                    disposition = "duplicate" if prior is not None else "late"
                    result.update(matched=True, duplicate=True)
                else:
                    disposition = "confirmed"
                    conn.execute(
                        "UPDATE avoidance_commands SET status=?,coordination_ref=?,confirmed_at=?,"
                        "ended_at=?,version=version+1 WHERE id=?",
                        (rules.COMMAND_CONFIRMED, coordination_ref, timestamp, timestamp, command["id"]),
                    )
                    result.update(matched=True, confirmed=True, command_id=command["id"])
                    event = {
                        "command_id": command["id"], "command_ref": command_ref,
                        "coordination_ref": coordination_ref,
                    }
                    conn.execute(
                        "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                        (item_id, "ingest_receipt", actor, role, canonical_json(event), timestamp),
                    )
                    self.append_audit(conn, item_id, "command_confirmed", actor, role, event)

            cur = conn.execute(
                "INSERT INTO command_receipts(item_id,command_id,command_ref,coordination_ref,"
                "disposition,payload,received_by,received_role,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (item_id, command["id"] if command else None, command_ref, coordination_ref,
                 disposition, canonical_json(receipt), actor, role, timestamp),
            )
            result["receipt_id"] = cur.lastrowid
            result["disposition"] = disposition
            conn.execute("COMMIT")
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_commands(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM avoidance_commands WHERE item_id=? ORDER BY attempt", (item_id,)
            ).fetchall()
            return [self._row_to_command(row) for row in rows]
        finally:
            conn.close()

    def list_receipts(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM command_receipts WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def reconciliation_summary(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT c.id AS command_id, c.item_id, i.stable_key AS item_key, c.attempt,
                       c.command_ref, c.coordination_ref, c.status, c.risk_level,
                       c.issued_at, c.confirmed_at,
                       (SELECT COUNT(*) FROM command_receipts r
                          WHERE r.command_id = c.id AND r.disposition='confirmed') AS confirmed_receipts,
                       (SELECT COUNT(*) FROM command_receipts r
                          WHERE r.command_id = c.id AND r.disposition IN ('duplicate','late')) AS duplicate_receipts
                  FROM avoidance_commands c JOIN items i ON i.id = c.item_id
                 ORDER BY c.item_id, c.attempt
                """
            ).fetchall()
            commands = []
            totals = {"pending": 0, "confirmed": 0, "superseded": 0, "voided": 0, "cancelled": 0}
            for row in rows:
                value = dict(row)
                value["reconciled"] = value["status"] == rules.COMMAND_CONFIRMED and value["confirmed_receipts"] == 1
                totals[value["status"]] = totals.get(value["status"], 0) + 1
                commands.append(value)
            unmatched = conn.execute(
                "SELECT id,command_ref,coordination_ref,disposition,created_at "
                "FROM command_receipts WHERE disposition='unmatched' ORDER BY id"
            ).fetchall()
            return {
                "totals": totals,
                "commands": commands,
                "unmatched_receipts": [dict(row) for row in unmatched],
            }
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
