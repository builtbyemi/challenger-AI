"""Durable request admission and provider-reported token accounting."""
import sqlite3


class UsageLedger:
    def __init__(self, path, rpm=20, rpd=1000, tpm=10000, tpd=200000):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.limits = ((60, rpm, tpm), (86400, rpd, tpd))
        self.db.execute("""CREATE TABLE IF NOT EXISTS groq_usage (
            id INTEGER PRIMARY KEY, at REAL NOT NULL, account_id TEXT NOT NULL,
            purpose TEXT NOT NULL, reserved_tokens INTEGER NOT NULL,
            tokens INTEGER, status INTEGER)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS groq_usage_time ON groq_usage(at)")

    def reserve(self, now, account, purpose, estimate):
        from .groq_client import GroqBackoff
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for period, requests, tokens in self.limits:
                rows = self.db.execute(
                    "SELECT at,COALESCE(tokens,reserved_tokens) FROM groq_usage WHERE at>? ORDER BY at",
                    (now - period,)).fetchall()
                if len(rows) >= requests or sum(r[1] for r in rows) + estimate > tokens:
                    raise GroqBackoff(max(1, rows[0][0] + period - now) if rows else period)
            cursor = self.db.execute(
                "INSERT INTO groq_usage(at,account_id,purpose,reserved_tokens) VALUES (?,?,?,?)",
                (now, account, purpose, estimate))
            self.db.commit()
            return cursor.lastrowid
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, request_id, status, usage):
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        if type(tokens) is not int or tokens < 0:
            tokens = None
        self.db.execute("UPDATE groq_usage SET status=?,tokens=? WHERE id=?", (status, tokens, request_id))

    def close(self):
        self.db.close()
