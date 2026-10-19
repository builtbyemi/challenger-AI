import asyncio
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from challenger.coordinator import Coordinator
from challenger.models import Message
from challenger.storage import Store


REPLIES = (
    "Na this one surprise me",
    "That amount no be small money",
    "Which notice lists the payment deadline",
    "Looks like they changed that location",
    "Check whether the parcel arrived yesterday",
    "Who first mentioned the meeting schedule",
)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "shared.sqlite3"
        self.coordinator = Coordinator(self.path)
        self.now = 10000.0
        self.coordinator.register("one", 11, self.now)
        self.coordinator.register("two", 22, self.now)

    def tearDown(self):
        self.coordinator.close()
        self.temp.cleanup()

    def action_count(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM gc_actions").fetchone()[0]

    def reserve(self, index=0, mode="live", chat_id=-100, now=None, account_id=None):
        account_id = account_id or ("one" if index % 2 == 0 else "two")
        now = self.now + 600 * index if now is None else now
        action = self.coordinator.reserve(account_id, chat_id, index + 1, REPLIES[index], mode, now)
        self.assertIsNotNone(action)
        return action

    def outgoing(self, message_id=101, sender_id=11, now=None, text=REPLIES[0], reply_to_id=1):
        return Message(message_id, -100, sender_id, text, self.now if now is None else now,
                       reply_to_id=reply_to_id, outgoing=True)

    def test_registration_persists_and_disallows_duplicate_or_rebound_identity(self):
        self.coordinator.register("one", 11)
        with self.assertRaises(ValueError):
            self.coordinator.register("three", 11)
        with self.assertRaises(ValueError):
            self.coordinator.register("one", 33)
        self.assertEqual(self.coordinator.identity_for("one"), 11)
        self.assertIsNone(self.coordinator.identity_for("missing"))
        self.coordinator.close()
        self.coordinator = Coordinator(self.path)
        self.assertEqual(self.coordinator.peer_ids(), {11, 22})
        self.assertEqual(self.coordinator.identity_for("two"), 22)

    def test_group_shared_five_per_hour_including_preview(self):
        self.coordinator.window_mode = "rolling"  # Explicit legacy-mode coverage.
        for index in range(5):
            action = self.reserve(index, mode="preview" if index % 2 == 0 else "live")
            self.coordinator.finish(action, "preview" if index % 2 == 0 else "sent", self.now + index * 600)
        self.assertEqual(self.coordinator.quota_wait(-100, self.now + 3000), 600)
        self.assertIsNone(self.coordinator.reserve("two", -100, 6, REPLIES[5], "live", self.now + 3000))
        self.assertGreater(self.coordinator.quota_wait(-100, self.now + 3599.9), 0)
        self.assertEqual(self.coordinator.quota_wait(-100, self.now + 3600), 0)
        self.reserve(5, now=self.now + 3600)

    def test_group_allows_distinct_replies_seconds_apart_across_modes(self):
        self.reserve(0, mode="preview")
        self.assertIsNotNone(self.coordinator.reserve("two", -100, 2, REPLIES[1], "live", self.now + 10))
        self.assertEqual(self.coordinator.quota_remaining(-100, self.now + 10), 3)

    def test_claim_prevents_second_account_answer_after_cooldown(self):
        action = self.reserve()
        self.coordinator.finish(action, "failed", self.now)
        self.assertTrue(self.coordinator.is_claimed(-100, 1))
        self.assertIsNone(self.coordinator.reserve("two", -100, 1, REPLIES[1], "preview", self.now + 7200))
        self.assertEqual(self.coordinator.quota_wait(-100, self.now + 1), 0)

    def test_group_history_rejects_cross_account_near_duplicates(self):
        self.reserve()
        self.assertIsNone(self.coordinator.reserve("two", -100, 2, "Na this one surprise me 😂", "preview", self.now + 600))
        self.assertIsNone(self.coordinator.reserve("two", -100, 2, "Na this one really surprise me", "live", self.now + 600))
        self.assertIsNotNone(self.coordinator.reserve("two", -100, 2, REPLIES[1], "live", self.now + 600))

    def test_groups_are_independent_and_hard_limits_are_clamped(self):
        self.reserve()
        self.reserve(chat_id=-200)
        another = Coordinator(self.path, max_actions=99)
        try:
            self.assertEqual(another.max_actions, 25)
            self.assertFalse(hasattr(another, "min_gap"))
        finally:
            another.close()

    def test_persistent_shared_backoff_never_shortens(self):
        self.coordinator.set_block("groq", self.now + 300)
        self.coordinator.set_block("groq", self.now + 10)
        self.coordinator.close()
        self.coordinator = Coordinator(self.path)
        self.assertEqual(self.coordinator.blocked_until("groq"), self.now + 300)
        self.assertIsNone(self.coordinator.reserve("two", -100, 1, REPLIES[0], "live", self.now + 299))
        self.assertIsNotNone(self.coordinator.reserve("two", -100, 1, REPLIES[0], "live", self.now + 300))

    def test_restart_keeps_group_claims_quota_and_chronological_history(self):
        self.reserve(0)
        self.reserve(1)
        self.reserve(2)
        self.coordinator.close()
        self.coordinator = Coordinator(self.path)
        self.assertTrue(self.coordinator.is_claimed(-100, 1))
        self.assertEqual(self.coordinator.quota_wait(-100, self.now + 1201), 0)
        self.assertEqual(self.coordinator.recent_replies(-100, self.now + 1201, 2), list(REPLIES[1:3]))

    def test_transactions_prevent_competing_account_reservations(self):
        for index in range(2, 6):
            self.reserve(index, now=self.now)
        barrier = threading.Barrier(2)

        def attempt(index):
            other = Coordinator(self.path)
            try:
                barrier.wait(timeout=10)
                return other.reserve("one" if index == 0 else "two", -100, index + 1,
                                     REPLIES[index], "live", self.now)
            finally:
                other.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (0, 1)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.action_count(), 5)
        self.assertEqual(self.coordinator.quota_remaining(-100, self.now), 0)

    def test_all_listeners_deduplicate_peer_manual_messages(self):
        message = self.outgoing()
        self.coordinator.observe_outgoing("one", message)
        self.coordinator.observe_outgoing("two", replace(message, outgoing=False))
        self.assertEqual(self.action_count(), 1)
        self.assertTrue(self.coordinator.is_claimed(-100, 1))
        self.assertEqual(self.coordinator.quota_wait(-100, self.now), 0)
        unregistered = replace(message, message_id=102, sender_id=999)
        self.coordinator.observe_outgoing("one", unregistered)
        self.assertEqual(self.action_count(), 1)

    def test_event_reconciles_sender_account_even_seen_by_another_account(self):
        action = self.reserve()
        outgoing = replace(self.outgoing(now=self.now + 2), outgoing=False)
        self.coordinator.observe_outgoing("two", outgoing)
        self.coordinator.finish(action, "uncertain", self.now + 3)
        self.assertEqual(self.action_count(), 1)
        with closing(sqlite3.connect(self.path)) as connection:
            status = connection.execute("SELECT status FROM gc_actions WHERE id=?", (action,)).fetchone()[0]
        self.assertEqual(status, "sent")
        self.assertEqual(self.coordinator.quota_wait(-100, self.now + 3), 0)

    def test_finish_deduplicates_telegram_id_before_event(self):
        action = self.reserve()
        self.coordinator.finish(action, "sent", self.now, telegram_id=101)
        self.coordinator.observe_outgoing("two", self.outgoing())
        self.assertEqual(self.action_count(), 1)

    def test_finish_merges_normalized_text_observation(self):
        action = self.reserve()
        self.coordinator.observe_outgoing("two", self.outgoing(text=REPLIES[0] + " "))
        self.assertEqual(self.action_count(), 2)
        self.coordinator.finish(action, "sent", self.now + 2, telegram_id=101)
        self.assertEqual(self.action_count(), 1)
        self.assertTrue(self.coordinator.is_claimed(-100, 1))

    def test_preview_never_reconciles_real_outgoing(self):
        self.reserve(mode="preview")
        self.coordinator.observe_outgoing("one", self.outgoing())
        self.assertEqual(self.action_count(), 2)

    def test_local_export_import_deduplicates_existing_group_preview_on_restart(self):
        local = Store(Path(self.temp.name) / "one.sqlite3", "one", -100)
        try:
            message = Message(1, -100, 99, "What happened to the payment deadline?", self.now)
            local.enqueue(message)
            group_action = self.reserve(mode="preview")
            local_action = local.reserve(message, REPLIES[0], "preview", self.now)
            local.finish(local_action, "preview", self.now + 2)
            self.coordinator.finish(group_action, "preview", self.now + 2)
            records = local.export_actions(self.now + 2)
            self.assertEqual(len(records), 1)
            self.coordinator.import_activity("one", -100, records)
            self.coordinator.import_activity("one", -100, records)
            self.assertEqual(self.action_count(), 1)
            self.coordinator.close()
            self.coordinator = Coordinator(self.path)
            self.coordinator.import_activity("one", -100, local.export_actions(self.now + 3))
            self.assertEqual(self.action_count(), 1)
            self.assertEqual(self.coordinator.quota_wait(-100, self.now + 3), 0)
        finally:
            local.close()

    def test_legacy_import_retains_distinct_reservations_and_reactions_once(self):
        records = [
            {"id": 1, "source_id": 1, "text": REPLIES[0], "mode": "legacy", "activity_at": self.now, "kind": "REPLY"},
            {"id": 2, "source_id": 1, "text": REPLIES[0], "mode": "legacy", "activity_at": self.now, "kind": "REPLY"},
            {"id": 3, "source_id": 3, "text": "😂", "mode": "legacy", "activity_at": self.now, "kind": "REACT"},
        ]
        self.coordinator.import_activity("one", -100, records)
        self.coordinator.import_activity("one", -100, records)
        self.assertEqual(self.action_count(), 3)
        self.assertEqual(len(self.coordinator.recent_replies(-100, self.now)), 2)
        self.assertTrue(self.coordinator.is_claimed(-100, 3))

    def test_import_telegram_history_and_shared_observation_are_one_action(self):
        outgoing = self.outgoing()
        self.coordinator.observe_outgoing("one", outgoing)
        records = [{"id": 1, "source_id": 1, "text": REPLIES[0], "mode": "observed",
                    "activity_at": self.now, "telegram_id": 101, "status": "sent"}]
        self.coordinator.import_activity("one", -100, records)
        self.assertEqual(self.action_count(), 1)
        records[0]["activity_at"] = self.now + 5
        self.coordinator.import_activity("one", -100, records)
        self.assertEqual(self.action_count(), 1)
        self.assertEqual(self.coordinator.quota_wait(-100, self.now + 5), 0)


class GroupLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_entire_pass_serialized_across_instances_but_other_groups_proceed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared.sqlite3"
            first, second = Coordinator(path), Coordinator(path)
            entered_first, entered_second, entered_other = asyncio.Event(), asyncio.Event(), asyncio.Event()
            release = asyncio.Event()

            async def hold():
                async with first.group_lock(-100):
                    entered_first.set()
                    await release.wait()

            async def same_group():
                await entered_first.wait()
                async with second.group_lock(-100):
                    entered_second.set()

            async def other_group():
                await entered_first.wait()
                async with second.group_lock(-200):
                    entered_other.set()

            tasks = [asyncio.create_task(work()) for work in (hold, same_group, other_group)]
            try:
                await asyncio.wait_for(entered_other.wait(), timeout=2)
                self.assertFalse(entered_second.is_set())
                release.set()
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
                self.assertTrue(entered_second.is_set())
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                first.close()
                second.close()


if __name__ == "__main__":
    unittest.main()
