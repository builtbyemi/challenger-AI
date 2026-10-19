import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from challenger.models import Message
from challenger.storage import Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "activity.sqlite3"
        self.store = Store(self.path, "account_1", -100)
        self.now = 10000.0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, message_id=1, now=None, **changes):
        return Message(message_id, -100, 22, "How much did the repair cost?",
                       self.now if now is None else now, **changes)

    def reserve(self, message_id, now, mode="live"):
        message = self.message(message_id, now)
        self.store.enqueue(message)
        action = self.store.reserve(message, "That amount no be small money", mode, now)
        self.assertIsNotNone(action)
        return action

    def test_five_messages_in_rolling_hour(self):
        self.store.window_mode = "rolling"  # Explicit legacy-mode coverage.
        for index in range(5):
            self.reserve(index + 1, self.now + index * 600)
        self.assertEqual(self.store.quota_wait(self.now + 3000), 600)
        sixth = self.message(6, self.now + 3000)
        self.store.enqueue(sixth)
        self.assertIsNone(self.store.reserve(sixth, "Na serious money be that", "live", self.now + 3000))
        self.assertGreater(self.store.quota_wait(self.now + 3599.9), 0)
        self.assertEqual(self.store.quota_wait(self.now + 3600), 0)
        self.reserve(7, self.now + 3600)
        self.assertEqual(self.store.quota_wait(self.now + 3600), 600)

    def test_preview_and_live_allow_replies_seconds_apart(self):
        for first_mode, second_mode in (("preview", "live"), ("live", "preview")):
            with self.subTest(first=first_mode):
                base = self.now if first_mode == "preview" else self.now + 7200
                first_id = 1 if first_mode == "preview" else 3
                action = self.reserve(first_id, base, first_mode)
                self.store.finish(action, first_mode, base)
                message = self.message(first_id + 1, base + 10)
                self.store.enqueue(message)
                self.assertIsNotNone(self.store.reserve(message, "Na this one surprise me", second_mode, base + 10))
                self.assertEqual(self.store.quota_remaining(base + 10), 3)

    def test_cannot_configure_away_hard_limits(self):
        other = Store(self.path, "account_2", -100, max_actions=500)
        try:
            self.assertEqual(other.max_actions, 5)
            self.assertFalse(hasattr(other, "min_gap"))
        finally:
            other.close()

    def test_restart_preserves_queue_quota_and_backoff(self):
        action = self.reserve(1, self.now, "preview")
        self.store.finish(action, "preview", self.now)
        self.store.enqueue(self.message(2, self.now + 1))
        self.store.set_block("telegram", self.now + 900)
        self.store.close()
        self.store = Store(self.path, "account_1", -100)
        self.assertEqual(self.store.quota_wait(self.now + 1), 0)
        self.assertEqual([m.message_id for m in self.store.pending(self.now + 1)], [2])
        self.assertEqual(self.store.blocked_until("telegram"), self.now + 900)
        self.store.set_block("telegram", self.now + 30)
        self.assertEqual(self.store.blocked_until("telegram"), self.now + 900)

    def test_different_accounts_are_isolated_but_chats_share_account_quota(self):
        self.reserve(1, self.now)
        another_account = Store(self.path, "account_2", -100)
        another_chat = Store(self.path, "account_1", -200)
        try:
            self.assertEqual(another_account.quota_wait(self.now), 0)
            self.assertIsNone(another_account.get(1))
            self.assertEqual(another_chat.quota_wait(self.now), 0)
            self.assertIsNone(another_chat.get(1))
        finally:
            another_account.close()
            another_chat.close()

    def test_simultaneous_connections_cannot_double_reserve(self):
        for index in range(4):
            self.reserve(100 + index, self.now)
        self.store.enqueue(self.message(1))
        self.store.enqueue(self.message(2))
        barrier = threading.Barrier(2)

        def attempt(message_id):
            connection = Store(self.path, "account_1", -100)
            try:
                barrier.wait(timeout=10)
                return connection.reserve(self.message(message_id), "Na this one surprise me", "live", self.now)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (1, 2)))
        self.assertEqual(sum(action is not None for action in results), 1)

    def test_edited_snapshot_cannot_be_reserved_or_marked_by_old_decision(self):
        before = self.message(1)
        self.assertTrue(self.store.enqueue(before))
        self.assertFalse(self.store.enqueue(before))
        after = replace(before, text="Never mind, someone answered it", edited_at=self.now + 2)
        self.assertTrue(self.store.enqueue(after))
        self.store.mark([before], "ignored")
        self.assertEqual(self.store.status(1), "pending")
        self.assertIsNone(self.store.reserve(before, "That amount no be small money", "live", self.now + 3))
        self.assertFalse(self.store.enqueue(before))
        self.assertEqual(self.store.get(1), after)
        self.store.mark([after], "ignored")
        self.assertTrue(self.store.enqueue(replace(after, text="Yet another edit", edited_at=self.now + 4)))
        self.assertEqual(self.store.status(1), "ignored")

    def test_processed_edits_refresh_context_without_resurrecting_candidates(self):
        original = self.message(1)
        self.store.enqueue(original)
        self.store.mark([original], "reviewed", "Already answered this conversation")
        edited = replace(original, text="The repair actually cost eight thousand",
                         edited_at=self.now + 3)
        self.assertTrue(self.store.enqueue(edited))
        self.assertFalse(self.store.enqueue(edited))
        self.assertFalse(self.store.enqueue(original))
        self.assertEqual(self.store.get(1), edited)
        self.assertEqual(self.store.context(self.now + 3), [edited])
        self.assertEqual(self.store.status(1), "reviewed")
        self.assertEqual(self.store.pending(self.now + 3), [])
        self.assertIsNone(self.store.reserve(edited, "That amount no be small money", "live", self.now + 3))
        self.store.mark([original], "ignored")
        self.assertEqual(self.store.status(1), "reviewed")
        with closing(sqlite3.connect(self.path)) as check:
            note = check.execute("SELECT note FROM tc_messages WHERE message_id=1").fetchone()[0]
        self.assertEqual(note, "Already answered this conversation")

    def test_processed_snapshot_can_receive_metadata_enrichment(self):
        original = self.message(1, reply_to_id=88)
        self.store.enqueue(original)
        self.store.mark([original], "reviewed")
        enriched = replace(original, reply_to_sender_id=99, is_bot=True)
        self.assertTrue(self.store.enqueue(enriched))
        self.assertFalse(self.store.enqueue(original))
        self.assertEqual(self.store.get(1), enriched)
        self.assertEqual(self.store.status(1), "reviewed")
        self.assertEqual(self.store.pending(self.now), [])

    def test_reply_metadata_survives_storage_and_snapshot_checks(self):
        message = self.message(1, reply_to_id=88, reply_to_sender_id=99,
                               mentions=("someone",), mentioned_ids=(99,),
                               forwarded=True, is_bot=True)
        self.store.enqueue(message)
        self.assertEqual(self.store.get(1), message)
        changed = replace(message, reply_to_id=90)
        self.store.enqueue(changed)
        self.assertIsNone(self.store.reserve(message, "That amount no be small money", "preview", self.now))

    def test_sparse_duplicate_preserves_enriched_thread_and_bot_metadata(self):
        enriched = self.message(1, reply_to_id=88, reply_to_sender_id=99, is_bot=True)
        self.store.enqueue(enriched)
        sparse = replace(enriched, reply_to_sender_id=None, is_bot=False)
        self.assertFalse(self.store.enqueue(sparse))
        self.assertEqual(self.store.get(1), enriched)
        edited = replace(sparse, text="What about the other repair?", edited_at=self.now + 2)
        self.assertTrue(self.store.enqueue(edited))
        stored = self.store.get(1)
        self.assertEqual(stored.reply_to_sender_id, 99)
        self.assertTrue(stored.is_bot)
        self.assertEqual(stored.text, edited.text)

    def test_busy_queue_retains_arrivals_and_selects_fresh_bounded_batch(self):
        for index in range(1, 101):
            self.store.enqueue(self.message(index, self.now + index))
        batch = self.store.pending(self.now + 100, limit=20)
        self.assertEqual([m.message_id for m in batch], list(range(100, 80, -1)))
        # These messages arrive while a simulated Groq request holds a snapshot.
        for index in range(101, 111):
            self.store.enqueue(self.message(index, self.now + index))
        self.store.mark(batch, "ignored")
        pending = self.store.pending(self.now + 110, limit=200)
        self.assertEqual(len(pending), 90)
        self.assertEqual([m.message_id for m in pending[:10]], list(range(110, 100, -1)))
        context = self.store.context(self.now + 110, limit=3)
        self.assertEqual([m.message_id for m in context], [108, 109, 110])

    def test_stale_or_processed_target_is_never_reserved_twice(self):
        stale = self.message(1, self.now - 720)
        self.store.enqueue(stale)
        self.assertIsNone(self.store.reserve(stale, "That amount no be small money", "live", self.now))
        self.assertEqual(self.store.pending(self.now), [])
        self.store.expire(self.now)
        self.assertEqual(self.store.status(1), "expired")
        current = self.message(2)
        self.store.enqueue(current)
        action = self.store.reserve(current, "That amount no be small money", "live", self.now)
        self.store.finish(action, "sent", self.now)
        self.assertIsNone(self.store.reserve(current, "That amount no be small money", "live", self.now + 600))

    def test_failed_reservations_still_consume_quota(self):
        action = self.reserve(1, self.now)
        self.store.finish(action, "uncertain", self.now + 10)
        self.assertEqual(self.store.quota_wait(self.now + 10), 0)
        self.assertEqual(self.store.status(1), "uncertain")

    def test_confirmation_extends_clock_and_never_moves_it_backwards(self):
        self.store.window_mode = "rolling"
        for index in range(4):
            self.reserve(100 + index, self.now + 25)
        action = self.reserve(1, self.now)
        self.store.finish(action, "sent", self.now + 25, telegram_id=55)
        self.assertEqual(self.store.quota_wait(self.now + 25), 3600)
        self.store.finish(action, "sent", self.now + 5, telegram_id=55)
        self.assertEqual(self.store.quota_wait(self.now + 25), 3600)

    def test_manual_outgoing_is_counted_once_and_not_pending(self):
        message = replace(self.message(101), outgoing=True, sender_id=10)
        self.store.observe_outgoing(message)
        self.store.observe_outgoing(message)
        self.assertEqual(self.store.quota_wait(self.now), 0)
        self.assertEqual(len(self.store.recent_replies(self.now)), 1)
        self.assertEqual(self.store.pending(self.now), [])

    def test_event_before_send_return_reconciles_reservation(self):
        action = self.reserve(1, self.now)
        outgoing = replace(self.message(101, self.now + 2), outgoing=True,
                           text="That amount no be small money", reply_to_id=1)
        self.store.observe_outgoing(outgoing)
        self.store.finish(action, "sent", self.now + 3, telegram_id=101)
        self.store.observe_outgoing(outgoing)
        self.assertEqual(len(self.store.recent_replies(self.now + 3)), 1)
        self.assertEqual(self.store.quota_wait(self.now + 3), 0)

    def test_event_after_send_return_deduplicates_telegram_id(self):
        action = self.reserve(1, self.now)
        self.store.finish(action, "sent", self.now, telegram_id=101)
        outgoing = replace(self.message(101), outgoing=True,
                           text="That amount no be small money", reply_to_id=1)
        self.store.observe_outgoing(outgoing)
        self.assertEqual(len(self.store.recent_replies(self.now)), 1)

    def test_confirmed_outgoing_cannot_be_regressed_by_send_timeout(self):
        action = self.reserve(1, self.now)
        outgoing = replace(self.message(101, self.now + 2), outgoing=True,
                           text="That amount no be small money", reply_to_id=1)
        self.store.observe_outgoing(outgoing)
        self.store.finish(action, "uncertain", self.now + 3)
        self.assertEqual(self.store.status(1), "sent")
        with closing(sqlite3.connect(self.path)) as check:
            status = check.execute("SELECT status FROM tc_actions WHERE id=?", (action,)).fetchone()[0]
        self.assertEqual(status, "sent")
        self.assertEqual(len(self.store.recent_replies(self.now + 3)), 1)

    def test_returned_message_id_merges_normalized_text_event(self):
        action = self.reserve(1, self.now)
        outgoing = replace(self.message(101, self.now + 2), outgoing=True,
                           text="That amount no be small money ", reply_to_id=1)
        self.store.observe_outgoing(outgoing)
        self.assertEqual(len(self.store.recent_replies(self.now + 2)), 2)
        self.store.finish(action, "sent", self.now + 3, telegram_id=101)
        self.assertEqual(len(self.store.recent_replies(self.now + 3)), 1)

    def test_recent_replies_are_chronological_with_newest_bounded(self):
        for index in range(3):
            outgoing = replace(self.message(100 + index, self.now + index),
                               outgoing=True, text=f"Manual message number {index}")
            self.store.observe_outgoing(outgoing)
        self.assertEqual(self.store.recent_replies(self.now + 3, limit=2),
                         ["Manual message number 1", "Manual message number 2"])

    def test_reservation_transaction_respects_persisted_backoff(self):
        message = self.message(1)
        self.store.enqueue(message)
        other = Store(self.path, "account_1", -100)
        try:
            other.set_block("groq", self.now + 45)
            self.assertIsNone(self.store.reserve(message, "That amount no be small money", "preview", self.now))
            self.assertIsNotNone(self.store.reserve(message, "That amount no be small money", "preview", self.now + 45))
        finally:
            other.close()

    def test_manual_message_cannot_reconcile_preview(self):
        self.reserve(1, self.now, "preview")
        outgoing = replace(self.message(101, self.now + 1), outgoing=True,
                           text="That amount no be small money", reply_to_id=1)
        self.store.observe_outgoing(outgoing)
        self.assertEqual(len(self.store.recent_replies(self.now + 1)), 2)

    def test_legacy_migration_is_backed_up_idempotent_and_keeps_original_history(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        timestamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        connection = sqlite3.connect(legacy_path)
        connection.execute("CREATE TABLE actions(ts TEXT,kind TEXT,source_id INTEGER,content TEXT)")
        connection.execute("CREATE TABLE candidates(message_id INTEGER PRIMARY KEY,arrived TEXT,sender_id INTEGER,body TEXT,status TEXT,note TEXT)")
        actions = [(timestamp, "REPLY", 1, "That amount no be small money"),
                   (timestamp, "REACT", 2, "😂")]
        connection.executemany("INSERT INTO actions VALUES (?,?,?,?)", actions)
        candidates = [(1, timestamp, 22, "An old question?", "sent", ""),
                      (2, timestamp, 22, "A pending question?", "pending", "")]
        connection.executemany("INSERT INTO candidates VALUES (?,?,?,?,?,?)", candidates)
        connection.commit()
        connection.close()
        for _ in range(2):
            migrated = Store(legacy_path, "account_1", -100)
            try:
                self.assertEqual(migrated.quota_wait(self.now), 0)
                self.assertEqual(len(migrated.recent_replies(self.now)), 1)
                self.assertEqual(migrated.status(1), "sent")
                self.assertEqual(migrated.status(2), "legacy_skipped")
                self.assertEqual(migrated.pending(self.now), [])
            finally:
                migrated.close()
        backups = list(Path(self.temp.name).glob("legacy.sqlite3.pre-challenger-v1*.bak"))
        self.assertEqual(len(backups), 1)
        for database in (legacy_path, backups[0]):
            with closing(sqlite3.connect(database)) as check:
                self.assertEqual(check.execute("SELECT * FROM actions").fetchall(), actions)
                self.assertEqual(check.execute("SELECT * FROM candidates").fetchall(), candidates)
        other = Store(legacy_path, "account_2", -100)
        try:
            self.assertEqual(other.quota_wait(self.now), 0)
            self.assertEqual(other.recent_replies(self.now), [])
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
