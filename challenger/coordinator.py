"""Shared group safeguards for explicitly configured Telegram accounts.

The account stores remain authoritative for each account. This additional,
separate database prevents several accounts from answering the same target,
repeating one another, or multiplying a group's activity ceiling.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
import unicodedata
import weakref
from contextlib import asynccontextmanager, contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from typing import AsyncIterator, Iterator

from .models import Message


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    return " ".join(re.findall(r"\w+(?:'\w+)*", text))


def _similar(left: str, right: str) -> bool:
    left, right = _normalized(left), _normalized(right)
    if not left or not right:
        return False
    a, b = set(left.split()), set(right.split())
    return left == right or SequenceMatcher(None, left, right).ratio() >= 0.82 or (
        len(a & b) / len(a | b) >= 0.75
    )


class Coordinator:
    """Durable group-wide claims and quotas shared by live and preview modes."""

    _locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()

    def __init__(self, path: Path | str, max_actions: int = 5) -> None:
        self.path = str(path)
        self.max_actions = max(1, min(5, int(max_actions)))
        self._lock_path = str(Path(self.path).resolve()).casefold() if self.path != ":memory:" else f"memory:{id(self)}"
        self._con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA busy_timeout = 10000")
        try:
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

    def _initialize(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS gc_accounts (
                account_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL UNIQUE,
                registered_at REAL NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS gc_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,account_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,source_id INTEGER,text TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'REPLY',mode TEXT NOT NULL,
                reserved_at REAL NOT NULL,activity_at REAL NOT NULL,
                status TEXT NOT NULL,telegram_id INTEGER,
                UNIQUE(chat_id,telegram_id))""",
            """CREATE INDEX IF NOT EXISTS gc_group_activity_idx
               ON gc_actions(chat_id,activity_at DESC)""",
            """CREATE INDEX IF NOT EXISTS gc_reconcile_idx
               ON gc_actions(account_id,chat_id,source_id,telegram_id)""",
            """CREATE TABLE IF NOT EXISTS gc_targets (
                chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
                action_id INTEGER NOT NULL,PRIMARY KEY(chat_id,message_id))""",
            """CREATE TABLE IF NOT EXISTS gc_imports (
                account_id TEXT NOT NULL,chat_id INTEGER NOT NULL,local_action_id INTEGER NOT NULL,
                action_id INTEGER NOT NULL,PRIMARY KEY(account_id,chat_id,local_action_id))""",
            """CREATE TABLE IF NOT EXISTS gc_blocks (
                scope TEXT PRIMARY KEY,until REAL NOT NULL)""",
        )
        with self._transaction():
            for statement in statements:
                self._con.execute(statement)

    def register(self, account_id: str, user_id: int, now: float | None = None) -> None:
        account_id = str(account_id).strip()
        if not account_id or isinstance(user_id, bool) or int(user_id) <= 0:
            raise ValueError("A nonempty account ID and a positive Telegram user ID are required")
        with self._transaction():
            existing = self._con.execute(
                "SELECT account_id,user_id FROM gc_accounts WHERE account_id=? OR user_id=?",
                (account_id, int(user_id)),
            ).fetchall()
            if existing:
                if len(existing) != 1 or existing[0]["account_id"] != account_id or existing[0]["user_id"] != int(user_id):
                    raise ValueError("A Telegram user cannot be configured as two accounts or rebound to another account ID")
                return
            self._con.execute(
                "INSERT INTO gc_accounts(account_id,user_id,registered_at) VALUES (?,?,?)",
                (account_id, int(user_id), time.time() if now is None else now),
            )

    def peer_ids(self) -> set[int]:
        return {row[0] for row in self._con.execute("SELECT user_id FROM gc_accounts")}

    def identity_for(self, account_id: str) -> int | None:
        row = self._con.execute(
            "SELECT user_id FROM gc_accounts WHERE account_id=?", (account_id,),
        ).fetchone()
        return int(row[0]) if row else None

    @asynccontextmanager
    async def group_lock(self, chat_id: int) -> AsyncIterator[None]:
        """Serialize complete worker passes for this group within one process.

        SQLite reservations additionally prevent duplicate sends across processes;
        only a single deployment should run inference for a shared group.
        """
        key = (self._lock_path, id(asyncio.get_running_loop()), int(chat_id))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        async with lock:
            yield

    def set_block(self, scope: str, until: float) -> None:
        self._con.execute(
            """INSERT INTO gc_blocks(scope,until) VALUES (?,?)
               ON CONFLICT(scope) DO UPDATE SET until=MAX(until,excluded.until)""",
            (scope, until),
        )

    def blocked_until(self, scope: str) -> float:
        row = self._con.execute("SELECT until FROM gc_blocks WHERE scope=?", (scope,)).fetchone()
        return float(row[0]) if row else 0.0

    def recent_replies(self, chat_id: int, now: float, limit: int = 50) -> list[str]:
        rows = self._con.execute(
            """SELECT text FROM gc_actions WHERE chat_id=? AND kind='REPLY' AND activity_at>=?
               ORDER BY activity_at DESC,id DESC LIMIT ?""",
            (chat_id, now - 7 * 86400, max(0, limit)),
        ).fetchall()
        return [row[0] for row in reversed(rows) if row[0].strip()]

    def quota_remaining(self, chat_id: int, now: float) -> int:
        used = self._con.execute(
            """SELECT COUNT(*) FROM gc_actions WHERE chat_id=?
               AND kind='REPLY' AND status!='rejected' AND activity_at>?""",
            (chat_id, now - 3600),
        ).fetchone()[0]
        return max(0, self.max_actions - used)

    def quota_wait(self, chat_id: int, now: float) -> float:
        rows = self._con.execute(
            "SELECT activity_at FROM gc_actions WHERE chat_id=? AND kind='REPLY' AND status!='rejected' ORDER BY activity_at DESC LIMIT ?",
            (chat_id, self.max_actions),
        ).fetchall()
        if not rows:
            return 0.0
        hourly = rows[-1][0] + 3600 - now if len(rows) == self.max_actions else 0.0
        return max(0.0, hourly)

    def is_claimed(self, chat_id: int, message_id: int) -> bool:
        return self._con.execute(
            "SELECT 1 FROM gc_targets WHERE chat_id=? AND message_id=?", (chat_id, message_id),
        ).fetchone() is not None

    def reserve(
        self, account_id: str, chat_id: int, message_id: int,
        text: str, mode: str, now: float,
    ) -> int | None:
        if mode not in ("live", "preview"):
            raise ValueError("mode must be live or preview")
        if not text.strip():
            return None
        with self._transaction():
            if self._con.execute(
                "SELECT 1 FROM gc_accounts WHERE account_id=?", (account_id,),
            ).fetchone() is None:
                raise ValueError("Register this account before reserving a group reply")
            if self.is_claimed(chat_id, message_id) or self.quota_wait(chat_id, now) > 0:
                return None
            if self._con.execute("SELECT 1 FROM gc_blocks WHERE until>? LIMIT 1", (now,)).fetchone():
                return None
            history = self._con.execute(
                "SELECT text FROM gc_actions WHERE chat_id=? AND kind='REPLY' AND activity_at>=?",
                (chat_id, now - 7 * 86400),
            )
            if any(_similar(text, row[0]) for row in history):
                return None
            cursor = self._con.execute(
                """INSERT INTO gc_actions
                   (account_id,chat_id,source_id,text,mode,reserved_at,activity_at,status)
                   VALUES (?,?,?,?,?,?,?,'reserved')""",
                (account_id, chat_id, message_id, text, mode, now, now),
            )
            action_id = int(cursor.lastrowid)
            self._claim_target(chat_id, message_id, action_id)
            return action_id

    def _claim_target(self, chat_id: int, message_id: int | None, action_id: int) -> None:
        if message_id is not None:
            self._con.execute(
                "INSERT OR IGNORE INTO gc_targets(chat_id,message_id,action_id) VALUES (?,?,?)",
                (chat_id, message_id, action_id),
            )

    def _merge_observation(self, action_id: int, observed: sqlite3.Row) -> float:
        if observed["mode"] != "observed":
            raise ValueError("Telegram message already belongs to another group reservation")
        self._con.execute("UPDATE gc_targets SET action_id=? WHERE action_id=?", (action_id, observed["id"]))
        self._con.execute("UPDATE gc_imports SET action_id=? WHERE action_id=?", (action_id, observed["id"]))
        self._con.execute("DELETE FROM gc_actions WHERE id=?", (observed["id"],))
        return float(observed["activity_at"])

    def finish(
        self, claim_id: int, status: str, now: float, telegram_id: int | None = None,
    ) -> None:
        with self._transaction():
            row = self._con.execute("SELECT * FROM gc_actions WHERE id=?", (claim_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown group reservation")
            if telegram_id is not None:
                observed = self._con.execute(
                    "SELECT * FROM gc_actions WHERE chat_id=? AND telegram_id=? AND id!=?",
                    (row["chat_id"], telegram_id, claim_id),
                ).fetchone()
                if observed:
                    now = max(now, self._merge_observation(claim_id, observed))
            confirmed_id = telegram_id if telegram_id is not None else row["telegram_id"]
            if confirmed_id is not None and status in ("failed", "uncertain", "rejected"):
                status = "sent"
            self._con.execute(
                """UPDATE gc_actions SET status=?,activity_at=MAX(activity_at,reserved_at,?),
                   telegram_id=COALESCE(?,telegram_id) WHERE id=?""",
                (status, now, confirmed_id, claim_id),
            )

    def observe_outgoing(self, account_id: str, message: Message) -> None:
        """Count registered peers, including messages seen by another account.

        ``account_id`` identifies the observer; sender attribution comes from the
        registered Telegram ID. Telegram's per-chat message ID deduplicates the
        same event received by all configured listeners.
        """
        if not message.text.strip():
            return
        with self._transaction():
            sender = self._con.execute(
                "SELECT account_id FROM gc_accounts WHERE user_id=?", (message.sender_id,),
            ).fetchone()
            if sender is None:
                return
            sender_account = sender[0]
            if self._con.execute(
                "SELECT 1 FROM gc_actions WHERE chat_id=? AND telegram_id=?",
                (message.chat_id, message.message_id),
            ).fetchone():
                return
            reservation = None
            if message.reply_to_id is not None:
                reservation = self._con.execute(
                    """SELECT id FROM gc_actions WHERE account_id=? AND chat_id=?
                       AND source_id=? AND text=? AND telegram_id IS NULL AND mode!='preview'
                       AND kind='REPLY' AND status IN ('reserved','sent','uncertain')
                       ORDER BY reserved_at DESC LIMIT 1""",
                    (sender_account, message.chat_id, message.reply_to_id, message.text),
                ).fetchone()
            if reservation:
                action_id = reservation[0]
                self._con.execute(
                    """UPDATE gc_actions SET telegram_id=?,status='sent',
                       activity_at=MAX(activity_at,reserved_at,?) WHERE id=?""",
                    (message.message_id, message.sent_at, action_id),
                )
            else:
                cursor = self._con.execute(
                    """INSERT INTO gc_actions
                       (account_id,chat_id,source_id,text,mode,reserved_at,activity_at,status,telegram_id)
                       VALUES (?,?,?,?,'observed',?,?,'sent',?)""",
                    (sender_account, message.chat_id, message.reply_to_id, message.text,
                     message.sent_at, message.sent_at, message.message_id),
                )
                action_id = int(cursor.lastrowid)
            self._claim_target(message.chat_id, message.reply_to_id, action_id)

    def import_activity(self, account_id: str, chat_id: int, records: list[dict]) -> None:
        """Import existing local history without resetting preview/live quotas.

        Each local action is mapped once. Re-imports may conservatively advance
        its timestamp or confirm its Telegram ID but cannot duplicate activity.
        Matching central reservations are linked, not imported a second time.
        """
        with self._transaction():
            for record in records:
                local_id = int(record["id"])
                source_id = record.get("source_id")
                text = str(record.get("text") or "")
                mode = str(record.get("mode") or "legacy")
                kind = str(record.get("kind") or "REPLY")
                activity_at = float(record["activity_at"])
                reserved_at = float(record.get("reserved_at", activity_at))
                telegram_id = record.get("telegram_id")
                status = str(record.get("status") or "reserved")
                imported = self._con.execute(
                    """SELECT a.* FROM gc_imports i JOIN gc_actions a ON a.id=i.action_id
                       WHERE i.account_id=? AND i.chat_id=? AND i.local_action_id=?""",
                    (account_id, chat_id, local_id),
                ).fetchone()
                existing = imported
                if existing is None and telegram_id is not None:
                    existing = self._con.execute(
                        "SELECT * FROM gc_actions WHERE chat_id=? AND telegram_id=?",
                        (chat_id, telegram_id),
                    ).fetchone()
                if existing is None and source_id is not None:
                    existing = self._con.execute(
                        """SELECT a.* FROM gc_actions a WHERE account_id=? AND chat_id=?
                           AND source_id=? AND text=? AND mode=? AND kind=?
                           AND NOT EXISTS (SELECT 1 FROM gc_imports i WHERE i.action_id=a.id)
                           ORDER BY reserved_at DESC LIMIT 1""",
                        (account_id, chat_id, source_id, text, mode, kind),
                    ).fetchone()
                if existing is None:
                    cursor = self._con.execute(
                        """INSERT INTO gc_actions
                           (account_id,chat_id,source_id,text,kind,mode,reserved_at,activity_at,status,telegram_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (account_id, chat_id, source_id, text, kind, mode, reserved_at,
                         activity_at, status, telegram_id),
                    )
                    action_id = int(cursor.lastrowid)
                else:
                    action_id = existing["id"]
                    if telegram_id is not None:
                        observed = self._con.execute(
                            "SELECT * FROM gc_actions WHERE chat_id=? AND telegram_id=? AND id!=?",
                            (chat_id, telegram_id, action_id),
                        ).fetchone()
                        if observed:
                            activity_at = max(activity_at, self._merge_observation(action_id, observed))
                    confirmed_id = telegram_id if telegram_id is not None else existing["telegram_id"]
                    if confirmed_id is not None:
                        status = "sent"
                    self._con.execute(
                        """UPDATE gc_actions SET activity_at=MAX(activity_at,?),status=?,
                           telegram_id=COALESCE(?,telegram_id) WHERE id=?""",
                        (activity_at, status, confirmed_id, action_id),
                    )
                self._con.execute(
                    """INSERT OR IGNORE INTO gc_imports
                       (account_id,chat_id,local_action_id,action_id) VALUES (?,?,?,?)""",
                    (account_id, chat_id, local_id, action_id),
                )
                self._claim_target(chat_id, source_id, action_id)

    def close(self) -> None:
        self._con.close()
