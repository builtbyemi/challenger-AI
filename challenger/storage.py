"""Durable message snapshots, account quotas, and conservative send reservations."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .models import Message
from .windows import window_start

LOG = logging.getLogger(__name__)
_SCHEMA_VERSION = "1"


class Store:
    """One account/chat view of a database shared safely with other accounts.

    Live sends, previews, manual outgoing text, and uncertain reservations all
    consume the same account quota. Explicit server rejections refund a slot;
    uncertain outcomes do not, since a timeout cannot prove non-delivery.
    """

    def __init__(
        self,
        path: Path | str,
        account_id: str,
        chat_id: int,
        max_actions: int = 5,
        max_age: int = 720,
        window_mode: str = "fixed",
        timezone: str = "Africa/Lagos",
    ) -> None:
        self.path = str(path)
        self.account_id = str(account_id)
        self.chat_id = int(chat_id)
        self.max_actions = max(1, min(5, int(max_actions)))
        self.max_age = max(1, int(max_age))
        self.window_mode, self.timezone = window_mode, timezone
        window_start(0, window_mode, timezone)
        self._con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA busy_timeout = 10000")
        try:
            self._backup_before_migration()
            self._con.execute("PRAGMA journal_mode = WAL")
            self._con.execute("PRAGMA synchronous = FULL")
            self._initialize()
        except Exception:
            self._con.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._con.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._con.rollback()
            raise
        else:
            self._con.commit()

    def _backup_before_migration(self) -> None:
        tables = {
            row[0] for row in self._con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not tables or self.path == ":memory:":
            return
        if "tc_metadata" in tables:
            migrated = self._con.execute(
                "SELECT value FROM tc_metadata WHERE key='schema_version'"
            ).fetchone()
            if migrated and migrated[0] == _SCHEMA_VERSION:
                return
        path = Path(self.path)
        backup = path.with_name(path.name + ".pre-challenger-v1.bak")
        # Never overwrite an earlier snapshot, including one from an interrupted
        # migration. SQLite backup also captures committed WAL contents.
        if backup.exists():
            backup = path.with_name(path.name + f".pre-challenger-v1-{uuid4().hex}.bak")
        with backup.open("xb"):
            pass
        destination = sqlite3.connect(str(backup))
        try:
            self._con.backup(destination)
        finally:
            destination.close()
        LOG.info("Saved database migration backup: %s", backup)

    def _initialize(self) -> None:
        with self._transaction():
            statements = (
                """CREATE TABLE IF NOT EXISTS tc_metadata
                   (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS tc_messages (
                    account_id TEXT NOT NULL, chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL, sender_id INTEGER, text TEXT NOT NULL,
                    sent_at REAL NOT NULL, reply_to_id INTEGER, reply_to_sender_id INTEGER,
                    mentions TEXT NOT NULL DEFAULT '[]', mentioned_ids TEXT NOT NULL DEFAULT '[]',
                    is_bot INTEGER NOT NULL DEFAULT 0, outgoing INTEGER NOT NULL DEFAULT 0,
                    forwarded INTEGER NOT NULL DEFAULT 0, edited_at REAL,
                    status TEXT NOT NULL DEFAULT 'pending', note TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (account_id, chat_id, message_id))""",
                """CREATE INDEX IF NOT EXISTS tc_pending_idx
                   ON tc_messages(account_id, chat_id, status, sent_at DESC)""",
                """CREATE INDEX IF NOT EXISTS tc_context_idx
                   ON tc_messages(account_id, chat_id, sent_at DESC)""",
                """CREATE TABLE IF NOT EXISTS tc_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL, chat_id INTEGER NOT NULL,
                    source_id INTEGER, text TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'REPLY',
                    mode TEXT NOT NULL, reserved_at REAL NOT NULL, activity_at REAL NOT NULL,
                    status TEXT NOT NULL, telegram_id INTEGER,
                    UNIQUE(account_id, chat_id, telegram_id))""",
                """CREATE INDEX IF NOT EXISTS tc_activity_idx
                   ON tc_actions(account_id, activity_at DESC)""",
                """CREATE INDEX IF NOT EXISTS tc_reconcile_idx
                   ON tc_actions(account_id, chat_id, source_id, telegram_id)""",
                """CREATE TABLE IF NOT EXISTS tc_blocks (
                    account_id TEXT NOT NULL, scope TEXT NOT NULL, until REAL NOT NULL,
                    PRIMARY KEY(account_id, scope))""",
            )
            for statement in statements:
                self._con.execute(statement)
            if self._con.execute(
                "SELECT 1 FROM tc_metadata WHERE key='legacy_migrated'"
            ).fetchone() is None:
                self._migrate_legacy()
                self._con.executemany(
                    "INSERT OR REPLACE INTO tc_metadata(key,value) VALUES (?,?)",
                    (("legacy_migrated", "1"), ("legacy_owner", self.account_id)),
                )
            self._con.execute(
                "INSERT OR REPLACE INTO tc_metadata(key,value) VALUES ('schema_version',?)",
                (_SCHEMA_VERSION,),
            )

    @staticmethod
    def _timestamp(value: str | float) -> float:
        if isinstance(value, (float, int)):
            return float(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def _migrate_legacy(self) -> None:
        tables = {
            row[0] for row in self._con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "actions" in tables:
            for row in self._con.execute("SELECT ts,kind,source_id,content FROM actions"):
                if row["kind"] not in ("REPLY", "REACT"):
                    continue
                # Refuse malformed history instead of silently resetting quotas.
                timestamp = self._timestamp(row["ts"])
                self._con.execute(
                    """INSERT INTO tc_actions
                       (account_id,chat_id,source_id,text,kind,mode,reserved_at,activity_at,status)
                       VALUES (?,?,?,?,?,'legacy',?,?,'reserved')""",
                    (self.account_id, self.chat_id, row["source_id"], row["content"] or "",
                     row["kind"], timestamp, timestamp),
                )
        if "candidates" in tables:
            for row in self._con.execute(
                "SELECT message_id,arrived,sender_id,body,status,note FROM candidates"
            ):
                status = "legacy_skipped" if row["status"] == "pending" else row["status"]
                self._con.execute(
                    """INSERT OR IGNORE INTO tc_messages
                       (account_id,chat_id,message_id,sender_id,text,sent_at,status,note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (self.account_id, self.chat_id, row["message_id"], row["sender_id"],
                     row["body"], self._timestamp(row["arrived"]), status,
                     "Imported legacy snapshot; threading unavailable"),
                )

    @staticmethod
    def _decode(row: sqlite3.Row) -> Message:
        return Message(
            message_id=row["message_id"], chat_id=row["chat_id"],
            sender_id=row["sender_id"], text=row["text"], sent_at=row["sent_at"],
            reply_to_id=row["reply_to_id"], reply_to_sender_id=row["reply_to_sender_id"],
            mentions=tuple(json.loads(row["mentions"])),
            mentioned_ids=tuple(json.loads(row["mentioned_ids"])),
            is_bot=bool(row["is_bot"]), outgoing=bool(row["outgoing"]),
            forwarded=bool(row["forwarded"]), edited_at=row["edited_at"],
        )

    def _row(self, message_id: int) -> sqlite3.Row | None:
        return self._con.execute(
            "SELECT * FROM tc_messages WHERE account_id=? AND chat_id=? AND message_id=?",
            (self.account_id, self.chat_id, message_id),
        ).fetchone()

    def enqueue(self, message: Message) -> bool:
        if message.chat_id != self.chat_id:
            return False
        with self._transaction():
            row = self._row(message.message_id)
            if row is not None:
                if row["edited_at"] is not None and (
                    message.edited_at is None or message.edited_at < row["edited_at"]
                ):
                    return False
                # Telegram event payloads may omit metadata enriched by a
                # history fetch. Do not undo known threading/sender information.
                message = replace(
                    message,
                    reply_to_sender_id=(
                        message.reply_to_sender_id if message.reply_to_sender_id is not None
                        else row["reply_to_sender_id"] if message.reply_to_id == row["reply_to_id"]
                        else None
                    ),
                    is_bot=message.is_bot or bool(row["is_bot"]),
                )
                if self._decode(row) == message:
                    return False
            # Context must reflect later edits even after a message has been
            # reviewed. Preserve terminal processing state so an edit cannot
            # resurrect an answered, ignored, or reserved candidate.
            status = row["status"] if row is not None else "pending"
            if status == "pending" and message.outgoing:
                status = "outgoing"
            values = (
                self.account_id, self.chat_id, message.message_id, message.sender_id,
                message.text, message.sent_at, message.reply_to_id, message.reply_to_sender_id,
                json.dumps(message.mentions), json.dumps(message.mentioned_ids),
                int(message.is_bot), int(message.outgoing), int(message.forwarded),
                message.edited_at, status,
            )
            self._con.execute(
                """INSERT INTO tc_messages
                   (account_id,chat_id,message_id,sender_id,text,sent_at,reply_to_id,
                    reply_to_sender_id,mentions,mentioned_ids,is_bot,outgoing,forwarded,
                    edited_at,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id,chat_id,message_id) DO UPDATE SET
                    sender_id=excluded.sender_id,text=excluded.text,sent_at=excluded.sent_at,
                    reply_to_id=excluded.reply_to_id,reply_to_sender_id=excluded.reply_to_sender_id,
                    mentions=excluded.mentions,mentioned_ids=excluded.mentioned_ids,
                    is_bot=excluded.is_bot,outgoing=excluded.outgoing,forwarded=excluded.forwarded,
                    edited_at=excluded.edited_at,status=excluded.status""",
                values,
            )
            return True

    def pending(self, now: float, limit: int = 20) -> list[Message]:
        rows = self._con.execute(
            """SELECT * FROM tc_messages WHERE account_id=? AND chat_id=?
               AND status='pending' AND outgoing=0 AND sent_at>? AND sent_at<=?
               ORDER BY sent_at DESC,message_id DESC LIMIT ?""",
            (self.account_id, self.chat_id, now - self.max_age, now + 30, max(0, limit)),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def context(self, now: float, limit: int = 60) -> list[Message]:
        rows = self._con.execute(
            """SELECT * FROM tc_messages WHERE account_id=? AND chat_id=?
               AND sent_at>? AND sent_at<=?
               ORDER BY sent_at DESC,message_id DESC LIMIT ?""",
            (self.account_id, self.chat_id, now - self.max_age * 2, now + 30, max(0, limit)),
        ).fetchall()
        return [self._decode(row) for row in reversed(rows)]

    def get(self, message_id: int) -> Message | None:
        row = self._row(message_id)
        return self._decode(row) if row else None

    def status(self, message_id: int) -> str | None:
        row = self._row(message_id)
        return row["status"] if row else None

    def mark(self, messages: list[Message], status: str, note: str = "") -> None:
        with self._transaction():
            for message in messages:
                row = self._row(message.message_id)
                if row is None or row["status"] != "pending" or self._decode(row) != message:
                    continue
                self._con.execute(
                    """UPDATE tc_messages SET status=?,note=?
                       WHERE account_id=? AND chat_id=? AND message_id=?""",
                    (status, note[:500], self.account_id, self.chat_id, message.message_id),
                )

    def expire(self, now: float) -> None:
        self._con.execute(
            """UPDATE tc_messages SET status='expired',note='Too old to answer'
               WHERE account_id=? AND chat_id=? AND status='pending' AND sent_at<=?""",
            (self.account_id, self.chat_id, now - self.max_age),
        )

    def recent_replies(self, now: float, limit: int = 50) -> list[str]:
        rows = self._con.execute(
            """SELECT text FROM tc_actions WHERE account_id=? AND kind IN ('REPLY','START_DISCUSSION')
               AND activity_at>=? ORDER BY activity_at DESC LIMIT ?""",
            (self.account_id, now - 7 * 86400, max(0, limit)),
        ).fetchall()
        return [row[0] for row in reversed(rows) if row[0].strip()]

    def quota_remaining(self, now: float) -> int:
        used = self._con.execute(
            """SELECT COUNT(*) FROM tc_actions WHERE account_id=?
               AND kind IN ('REPLY','START_DISCUSSION') AND status!='rejected' AND activity_at>=?""",
            (self.account_id, self._window_boundary(now)),
        ).fetchone()[0]
        return max(0, self.max_actions - used)

    def quota_wait(self, now: float) -> float:
        if self.window_mode == "fixed":
            return max(0, window_start(now, "fixed", self.timezone) + 3600 - now) if self.quota_remaining(now) == 0 else 0
        rows = self._con.execute(
            """SELECT activity_at FROM tc_actions WHERE account_id=?
               AND kind IN ('REPLY','START_DISCUSSION') AND status!='rejected'
               ORDER BY activity_at DESC LIMIT ?""",
            (self.account_id, self.max_actions),
        ).fetchall()
        if not rows:
            return 0.0
        hourly = rows[-1][0] + 3600 - now if len(rows) == self.max_actions else 0.0
        return max(0.0, hourly)

    def _window_boundary(self, now: float) -> float:
        import math
        boundary = window_start(now, self.window_mode, self.timezone)
        return math.nextafter(boundary, math.inf) if self.window_mode == "rolling" else boundary

    def reserve_start(self, text: str, mode: str, now: float) -> int | None:
        if mode not in {"live", "preview"} or not text.strip():
            raise ValueError("Invalid discussion reservation")
        with self._transaction():
            if self.quota_wait(now) or self._con.execute(
                "SELECT 1 FROM tc_blocks WHERE account_id=? AND until>?", (self.account_id, now)
            ).fetchone():
                return None
            cursor = self._con.execute(
                """INSERT INTO tc_actions(account_id,chat_id,text,kind,mode,reserved_at,activity_at,status)
                   VALUES (?,?,?,'START_DISCUSSION',?,?,?,'reserved')""",
                (self.account_id, self.chat_id, text, mode, now, now))
            return int(cursor.lastrowid)

    def discussion_context(self, now: float) -> list[Message]:
        rows = self._con.execute(
            """SELECT * FROM tc_messages WHERE account_id=? AND chat_id=? AND sent_at>?
               ORDER BY sent_at DESC,message_id DESC LIMIT 100""",
            (self.account_id, self.chat_id, now - 86400)).fetchall()
        return [self._decode(row) for row in reversed(rows)]

    def set_state(self, key: str, value: str) -> None:
        self._con.execute("INSERT OR REPLACE INTO tc_metadata(key,value) VALUES (?,?)",
                          (f"{self.account_id}:{key}", str(value)))

    def get_state(self, key: str, default: str = "") -> str:
        row = self._con.execute("SELECT value FROM tc_metadata WHERE key=?",
                                (f"{self.account_id}:{key}",)).fetchone()
        return row[0] if row else default

    def reserve(self, message: Message, text: str, mode: str, now: float) -> int | None:
        if mode not in ("live", "preview"):
            raise ValueError("mode must be live or preview")
        if not text.strip() or message.chat_id != self.chat_id or message.outgoing:
            return None
        with self._transaction():
            row = self._row(message.message_id)
            if (row is None or row["status"] != "pending" or self._decode(row) != message
                    or message.sent_at <= now - self.max_age or message.sent_at > now + 30
                    or self.quota_wait(now) > 0
                    or self._con.execute(
                        "SELECT 1 FROM tc_blocks WHERE account_id=? AND until>? LIMIT 1",
                        (self.account_id, now),
                    ).fetchone() is not None):
                return None
            cursor = self._con.execute(
                """INSERT INTO tc_actions
                   (account_id,chat_id,source_id,text,mode,reserved_at,activity_at,status)
                   VALUES (?,?,?,?,?,?,?,'reserved')""",
                (self.account_id, self.chat_id, message.message_id, text, mode, now, now),
            )
            self._con.execute(
                """UPDATE tc_messages SET status='reserved'
                   WHERE account_id=? AND chat_id=? AND message_id=?""",
                (self.account_id, self.chat_id, message.message_id),
            )
            return int(cursor.lastrowid)

    def finish(
        self, action_id: int, status: str, now: float, telegram_id: int | None = None,
    ) -> None:
        with self._transaction():
            row = self._con.execute(
                "SELECT * FROM tc_actions WHERE id=? AND account_id=? AND chat_id=?",
                (action_id, self.account_id, self.chat_id),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown action for this account/chat")
            if telegram_id is not None and row["telegram_id"] is not None and telegram_id != row["telegram_id"]:
                # A manual send can exactly match an in-flight bot message.
                # The returned ID proves they were distinct: restore the earlier
                # observation as manual instead of silently overwriting its ID.
                old = self._row(row["telegram_id"])
                old_at = old["sent_at"] if old else row["activity_at"]
                self._con.execute("UPDATE tc_actions SET telegram_id=NULL WHERE id=?", (action_id,))
                self._con.execute(
                    """INSERT INTO tc_actions(account_id,chat_id,source_id,text,kind,mode,reserved_at,activity_at,status,telegram_id)
                       VALUES (?,?,?,?,'REPLY','observed',?,?,'sent',?)""",
                    (self.account_id, self.chat_id, old["reply_to_id"] if old else row["source_id"],
                     row["text"], old_at, old_at, row["telegram_id"]))
            if telegram_id is not None:
                observed = self._con.execute(
                    """SELECT id,mode,activity_at FROM tc_actions WHERE account_id=?
                       AND chat_id=? AND telegram_id=? AND id!=?""",
                    (self.account_id, self.chat_id, telegram_id, action_id),
                ).fetchone()
                if observed:
                    if observed["mode"] != "observed":
                        raise ValueError("Telegram message already belongs to another reservation")
                    # Telegram can normalize text, preventing exact-text event
                    # reconciliation. The returned message ID identifies the
                    # same send conclusively, so merge that observation here.
                    now = max(now, observed["activity_at"])
                    self._con.execute("DELETE FROM tc_actions WHERE id=?", (observed["id"],))
            confirmed_id = telegram_id if telegram_id is not None else row["telegram_id"]
            if confirmed_id is not None and status in ("failed", "uncertain", "rejected"):
                status = "sent"
            # An outgoing event may arrive before send_message returns. Reconcile
            # the observed Telegram ID without moving the quota clock backwards.
            self._con.execute(
                """UPDATE tc_actions SET status=?,activity_at=MAX(activity_at,reserved_at,?),
                   telegram_id=COALESCE(?,telegram_id) WHERE id=?""",
                (status, now, confirmed_id, action_id),
            )
            self._con.execute(
                """UPDATE tc_messages SET status=? WHERE account_id=? AND chat_id=?
                   AND message_id=? AND status='reserved'""",
                (status, self.account_id, self.chat_id, row["source_id"]),
            )

    def observe_outgoing(self, message: Message) -> None:
        if not message.outgoing or not message.text.strip() or message.chat_id != self.chat_id:
            return
        self.enqueue(message)
        with self._transaction():
            existing = self._con.execute(
                """SELECT id FROM tc_actions WHERE account_id=? AND chat_id=?
                   AND telegram_id=?""",
                (self.account_id, self.chat_id, message.message_id),
            ).fetchone()
            if existing:
                return
            reservation = None
            if message.reply_to_id is not None:
                reservation = self._con.execute(
                    """SELECT id,source_id FROM tc_actions WHERE account_id=? AND chat_id=?
                       AND source_id=? AND text=? AND telegram_id IS NULL AND mode!='preview'
                       AND kind='REPLY' AND status IN ('reserved','sent','uncertain')
                       AND reserved_at BETWEEN ? AND ?
                       ORDER BY reserved_at DESC LIMIT 1""",
                    (self.account_id, self.chat_id, message.reply_to_id, message.text,
                     message.sent_at - 120, message.sent_at + 30),
                ).fetchone()
            else:
                reservation = self._con.execute(
                    """SELECT id,source_id FROM tc_actions WHERE account_id=? AND chat_id=?
                       AND kind='START_DISCUSSION' AND source_id IS NULL AND text=?
                       AND telegram_id IS NULL AND mode='live' AND status IN ('reserved','uncertain')
                       AND reserved_at BETWEEN ? AND ? ORDER BY reserved_at DESC LIMIT 1""",
                    (self.account_id, self.chat_id, message.text, message.sent_at - 120, message.sent_at + 30)
                ).fetchone()
            if reservation:
                self._con.execute(
                    """UPDATE tc_actions SET telegram_id=?,status='sent',
                       activity_at=MAX(activity_at,reserved_at,?) WHERE id=?""",
                    (message.message_id, message.sent_at, reservation["id"]),
                )
                self._con.execute(
                    """UPDATE tc_messages SET status='sent' WHERE account_id=? AND chat_id=?
                       AND message_id=? AND status='reserved'""",
                    (self.account_id, self.chat_id, reservation["source_id"]),
                )
            else:
                self._con.execute(
                    """INSERT INTO tc_actions
                       (account_id,chat_id,source_id,text,mode,reserved_at,activity_at,status,telegram_id)
                       VALUES (?,?,?,?,'observed',?,?,'sent',?)""",
                    (self.account_id, self.chat_id, message.reply_to_id, message.text,
                     message.sent_at, message.sent_at, message.message_id),
                )

    def set_block(self, scope: str, until: float) -> None:
        self._con.execute(
            """INSERT INTO tc_blocks(account_id,scope,until) VALUES (?,?,?)
               ON CONFLICT(account_id,scope) DO UPDATE SET until=MAX(until,excluded.until)""",
            (self.account_id, scope, until),
        )

    def export_actions(self, now: float, days: int = 7) -> list[dict]:
        """Read bounded recent history for the separate shared coordinator.

        Original local action IDs make repeated imports idempotent. This method
        does not alter the account database or any legacy history.
        """
        rows = self._con.execute(
            """SELECT id,source_id,text,kind,mode,reserved_at,activity_at,status,telegram_id
               FROM tc_actions WHERE account_id=? AND chat_id=? AND activity_at>=?
               ORDER BY activity_at,id""",
            (self.account_id, self.chat_id, now - max(1, days) * 86400),
        ).fetchall()
        return [dict(row) for row in rows]

    def blocked_until(self, scope: str) -> float:
        row = self._con.execute(
            "SELECT until FROM tc_blocks WHERE account_id=? AND scope=?",
            (self.account_id, scope),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def close(self) -> None:
        self._con.close()
