"""Persist only fixed diagnostic categories, never exception text or chat data."""
import logging
import sqlite3
import time
import re


class DiagnosticHandler(logging.Handler):
    def __init__(self, path):
        super().__init__(logging.WARNING)
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=1)
        self.db.execute("CREATE TABLE IF NOT EXISTS diagnostic_events(at REAL,level TEXT,component TEXT,code TEXT)")
        if 'account_id' not in {row[1] for row in self.db.execute('PRAGMA table_info(diagnostic_events)')}:
            self.db.execute('ALTER TABLE diagnostic_events ADD COLUMN account_id TEXT')

    def emit(self, record):
        if not record.name.startswith("challenger."):
            return
        code = "provider_or_worker_error"
        template = str(record.msg).lower()
        for marker, category in (("groq", "groq_error"), ("telegram", "telegram_wait_or_error"),
                                 ("reconnect", "reconnecting"), ("login", "authentication_required"),
                                 ("history", "history_unavailable")):
            if marker in template:
                code = category
                break
        try:
            account = next((arg for arg in record.args if isinstance(arg, str) and re.fullmatch(r'account_[1-5]', arg)), None)
            self.db.execute("INSERT INTO diagnostic_events(at,level,component,code,account_id) VALUES (?,?,?,?,?)",
                            (time.time(), record.levelname, record.name.split(".")[-1][:40], code, account))
            self.db.execute("DELETE FROM diagnostic_events WHERE rowid NOT IN (SELECT rowid FROM diagnostic_events ORDER BY rowid DESC LIMIT 2000)")
        except (sqlite3.Error, TypeError, AttributeError):
            pass

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        super().close()
