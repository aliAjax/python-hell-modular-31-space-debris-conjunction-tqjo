import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


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
                CREATE TABLE IF NOT EXISTS commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    command_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    coordination_number TEXT,
                    payload TEXT NOT NULL,
                    reason TEXT,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE INDEX IF NOT EXISTS idx_commands_item ON commands(item_id);
                CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    command_id INTEGER,
                    coordination_number TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(coordination_number),
                    FOREIGN KEY(item_id) REFERENCES items(id),
                    FOREIGN KEY(command_id) REFERENCES commands(id)
                );
                CREATE INDEX IF NOT EXISTS idx_receipts_item ON receipts(item_id);
                """
            )
        finally:
            conn.close()

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

    def _row_to_command(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _row_to_receipt(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def list_commands(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM commands WHERE item_id=? ORDER BY id DESC", (item_id,)
            ).fetchall()
            return [self._row_to_command(row) for row in rows]
        finally:
            conn.close()

    def list_receipts(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM receipts WHERE item_id=? ORDER BY id DESC", (item_id,)
            ).fetchall()
            return [self._row_to_receipt(row) for row in rows]
        finally:
            conn.close()

    def pending_commands(self):
        """All unconfirmed commands across every item — the operator's outstanding work list."""
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT c.*, i.status AS item_status
                FROM commands c JOIN items i ON i.id = c.item_id
                WHERE c.status = 'pending_confirm'
                ORDER BY c.id
                """
            ).fetchall()
            return [self._row_to_command(row) for row in rows]
        finally:
            conn.close()

    def _void_pending_commands(self, conn, item_id, actor, role, reason):
        """Void every unconfirmed command for an item. Caller holds the transaction."""
        rows = conn.execute(
            "SELECT id, command_ref FROM commands WHERE item_id=? AND status='pending_confirm' ORDER BY id",
            (item_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE commands SET status='voided', reason=?, updated_at=? WHERE id=?",
                (reason, now_iso(), row["id"]),
            )
            self.append_audit(
                conn,
                item_id,
                "command_voided",
                actor,
                role,
                {"command_id": row["id"], "command_ref": row["command_ref"], "reason": reason},
            )
        return len(rows)

    def initiate_command(self, item_id, command_ref, command_payload, actor, role, expected_version=None):
        """Send an avoidance command to the external coordination system.

        Recorded as pending_confirm. Re-initiating while a command is already
        pending supersedes the old one; an already-executed command blocks re-initiation.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取最新版本")

            existing = conn.execute(
                "SELECT * FROM commands WHERE item_id=? ORDER BY id DESC", (item_id,)
            ).fetchall()
            for cmd in existing:
                if cmd["status"] == "executed":
                    raise ConflictError("command_already_executed", "指令已执行完成，不能重新发起")
            superseded = None
            for cmd in existing:
                if cmd["status"] == "pending_confirm":
                    superseded = cmd
                    break
            if not existing and row["status"] != "coordinating":
                raise DomainError("invalid_state", "当前状态 %s 不允许发起规避指令" % row["status"])
            if superseded is not None and row["status"] != "executing":
                raise DomainError("invalid_state", "当前状态 %s 不允许重新发起规避指令" % row["status"])
            if superseded is not None:
                conn.execute(
                    "UPDATE commands SET status='superseded', reason='reinitiated', updated_at=? WHERE id=?",
                    (now_iso(), superseded["id"]),
                )
                self.append_audit(
                    conn,
                    item_id,
                    "command_superseded",
                    actor,
                    role,
                    {"command_id": superseded["id"], "command_ref": superseded["command_ref"], "reason": "reinitiated"},
                )

            cur = conn.execute(
                "INSERT INTO commands(item_id,command_ref,status,payload,created_by,created_role,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (item_id, command_ref, "pending_confirm", canonical_json(command_payload), actor, role, now_iso(), now_iso()),
            )
            command_id = cur.lastrowid

            new_payload = json.loads(row["payload"])
            new_payload["command_ref"] = command_ref
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                ("executing", version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "initiate_command", actor, role, canonical_json({"command_ref": command_ref, "reinitiated": superseded is not None}), now_iso()),
            )
            self.append_audit(
                conn,
                item_id,
                "command_initiated",
                actor,
                role,
                {"command_id": command_id, "command_ref": command_ref, "reinitiated": superseded is not None},
            )
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

    def record_receipt(self, item_id, coordination_number, received_at, receipt_payload, actor, role, expected_version=None):
        """Record an external receipt carrying a coordination number.

        Idempotent: a coordination number already seen is acknowledged once and
        does not change state. A new number confirms the pending command.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取最新版本")
            if row["status"] != "executing":
                raise DomainError("invalid_state", "当前状态 %s 不允许登记回执" % row["status"])

            duplicate = conn.execute(
                "SELECT * FROM receipts WHERE coordination_number=?", (coordination_number,)
            ).fetchone()
            if duplicate is not None:
                self.append_audit(
                    conn,
                    item_id,
                    "duplicate_receipt",
                    actor,
                    role,
                    {"coordination_number": coordination_number, "receipt_id": duplicate["id"]},
                )
                conn.execute("COMMIT")
                return {"duplicate": True, "receipt": self._row_to_receipt(duplicate), "item": self.get_item(item_id)}

            pending = conn.execute(
                "SELECT * FROM commands WHERE item_id=? AND status='pending_confirm' ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if pending is None:
                raise ConflictError("no_pending_command", "没有待确认的指令，回执无法对账")

            cur = conn.execute(
                "INSERT INTO receipts(item_id,command_id,coordination_number,payload,received_at,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (item_id, pending["id"], coordination_number, canonical_json(receipt_payload), received_at, now_iso()),
            )
            receipt_id = cur.lastrowid
            conn.execute(
                "UPDATE commands SET status='executed', coordination_number=?, reason=NULL, updated_at=? WHERE id=?",
                (coordination_number, now_iso(), pending["id"]),
            )
            conn.execute(
                "UPDATE items SET version=?,updated_at=? WHERE id=?",
                (int(row["version"]) + 1, now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "record_receipt", actor, role, canonical_json({"coordination_number": coordination_number, "receipt_id": receipt_id}), now_iso()),
            )
            self.append_audit(
                conn,
                item_id,
                "receipt_recorded",
                actor,
                role,
                {"receipt_id": receipt_id, "coordination_number": coordination_number},
            )
            self.append_audit(
                conn,
                item_id,
                "command_executed",
                actor,
                role,
                {"command_id": pending["id"], "command_ref": pending["command_ref"], "coordination_number": coordination_number},
            )
            conn.execute("COMMIT")
            return {
                "duplicate": False,
                "receipt": self._row_to_receipt(conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()),
                "command": self._row_to_command(conn.execute("SELECT * FROM commands WHERE id=?", (pending["id"],)).fetchone()),
                "item": self.get_item(item_id),
            }
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def reconciliation(self, item_id):
        """The reconcilable record for one event: event -> commands -> receipts."""
        item = self.get_item(item_id)
        commands = self.list_commands(item_id)
        receipts = self.list_receipts(item_id)
        pending = [c for c in commands if c["status"] == "pending_confirm"]
        executed = [c for c in commands if c["status"] == "executed"]
        voided = [c for c in commands if c["status"] == "voided"]
        superseded = [c for c in commands if c["status"] == "superseded"]
        return {
            "item": item,
            "commands": commands,
            "receipts": receipts,
            "pending": pending,
            "executed": executed,
            "voided": voided,
            "superseded": superseded,
            "all_reconciled": bool(executed) and not pending,
        }

    def reconciliation_state(self):
        """Global reconciliation view: every event with its command/receipt status."""
        items = self.list_items()
        result = []
        outstanding = []
        for item in items:
            commands = self.list_commands(item["id"])
            receipts = self.list_receipts(item["id"])
            pending = [c for c in commands if c["status"] == "pending_confirm"]
            executed = [c for c in commands if c["status"] == "executed"]
            entry = {
                "item": item,
                "commands": commands,
                "receipts": receipts,
                "pending_count": len(pending),
                "executed_count": len(executed),
                "outstanding": bool(pending),
            }
            result.append(entry)
            if pending:
                outstanding.append(entry)
        return {"items": result, "outstanding": outstanding, "outstanding_count": len(outstanding)}

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取最新版本")
            void_pending = bool(event_payload.pop("_void_pending_commands", False))
            if void_pending:
                pending_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM commands WHERE item_id=? AND status='pending_confirm'",
                    (item_id,),
                ).fetchone()["c"]
                if pending_count == 0:
                    new_status = row["status"]
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
            if void_pending:
                self._void_pending_commands(conn, item_id, actor, role, "risk_changed")
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
