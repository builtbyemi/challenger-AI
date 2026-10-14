import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from challenger.config import Settings
from challenger.coordinator import Coordinator
from challenger.engine import Refresh, Worker
from challenger.models import Identity, Message
from challenger.storage import Store


class Clock:
    def __init__(self, value=10000.0):
        self.value = value

    def __call__(self):
        return self.value


class Decider:
    def __init__(self, text="The meeting starts at eight tonight"):
        self.text = text
        self.calls = 0
        self.recent = []
        self.callback = None

    async def decide(self, candidates, context, recent):
        self.calls += 1
        self.recent = list(recent)
        target = candidates[0].message_id
        if self.callback:
            await self.callback()
        return {"action": "REPLY", "target_id": target, "text": self.text,
                "emoji": None, "reason": "The earlier notice directly answers this question"}


class Gateway:
    def __init__(self, identity, chat_id, clock, messages):
        self.identity, self.chat_id, self.clock = identity, chat_id, clock
        self.messages = messages
        self.sent = []
        self.syncs = 0

    async def sync(self):
        self.syncs += 1
        return list(self.messages.values())

    async def refresh(self, target_id):
        return Refresh(self.messages.get(target_id), list(self.messages.values()))

    async def send(self, text, target_id):
        self.sent.append((text, target_id))
        return Message(1000 + self.identity.user_id * 100 + len(self.sent), self.chat_id,
                       self.identity.user_id, text, self.clock(), reply_to_id=target_id,
                       outgoing=True)


class MultiEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.clock = Clock()
        self.coordinator_path = self.base / "shared.sqlite3"
        self.coordinator = Coordinator(self.coordinator_path)
        self.coordinator.register("one", 11, self.clock())
        self.coordinator.register("two", 22, self.clock())
        self.messages = {}
        self.workers = []
        self.stores = []
        self.first = self.make_worker("one", 11)
        self.second = self.make_worker("two", 22)

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.coordinator.close()
        self.temp.cleanup()

    def make_worker(self, account_id, user_id, chat_id=-100):
        path = self.base / f"{account_id}_{chat_id}.sqlite3"
        settings = Settings(1, "test", chat_id, "test", "test", account_id=account_id,
                            database_path=path)
        store = Store(path, account_id, chat_id)
        self.stores.append(store)
        identity = Identity(user_id, "first" if account_id == "one" else "second")
        messages = self.messages.setdefault(chat_id, {})
        gateway = Gateway(identity, chat_id, self.clock, messages)
        worker = Worker(settings, store, identity, Decider(), gateway, self.clock,
                        coordinator=self.coordinator)
        self.workers.append(worker)
        return worker

    def add(self, message):
        self.messages.setdefault(message.chat_id, {})[message.message_id] = message
        for worker in self.workers:
            worker.ingest(message)

    def add_meeting(self, target_id=2, chat_id=-100, at=None):
        at = self.clock() if at is None else at
        self.add(Message(target_id - 1, chat_id, 99, "The meeting starts at eight tonight", at - 10))
        for worker in self.workers:
            if worker.settings.chat_id == chat_id:
                worker.store.mark([worker.store.get(target_id - 1)], "reviewed", "Context-only fixture")
        target = Message(target_id, chat_id, 88, "What time does the meeting start tonight?", at)
        self.add(target)
        return target

    def seed_history(self, account_id, source_id, text, timestamp, local=False):
        records = [{"id": source_id, "source_id": source_id, "text": text, "mode": "preview",
                    "activity_at": timestamp, "status": "previewed"}]
        self.coordinator.import_activity(account_id, -100, records)
        if local:
            worker = self.first if account_id == "one" else self.second
            message = Message(source_id, -100, 99, "What time does this meeting start?", timestamp)
            worker.store.enqueue(message)
            action = worker.store.reserve(message, text, "preview", timestamp)
            self.assertIsNotNone(action)
            worker.store.finish(action, "previewed", timestamp)

    async def test_no_account_replies_to_a_configured_peer(self):
        peer_question = Message(5, -100, 22, "@first are you a bot?", self.clock(),
                                mentions=("first",), outgoing=False)
        self.add(peer_question)
        await asyncio.gather(self.first.process_once(), self.second.process_once())
        self.assertEqual(self.first.decider.calls + self.second.decider.calls, 0)
        self.assertTrue(self.first.store.get(5).is_bot)
        self.assertEqual(self.second.store.status(5), "outgoing")
        self.clock.value += 600
        await self.first.process_once()
        self.assertEqual(self.first.decider.calls, 0)
        self.assertEqual(self.first.store.status(5), "reviewed")

    async def test_concurrent_workers_make_only_one_decision_for_shared_target(self):
        self.add_meeting()
        started, release = asyncio.Event(), asyncio.Event()

        async def waiting():
            started.set()
            await release.wait()

        self.first.decider.callback = waiting
        first_task = asyncio.create_task(self.first.process_once())
        await asyncio.wait_for(started.wait(), timeout=2)
        second_task = asyncio.create_task(self.second.process_once())
        await asyncio.sleep(0)
        self.assertEqual(self.second.decider.calls, 0)
        self.assertTrue(self.second.store.get(2) is not None)
        release.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=2)
        self.assertEqual(self.first.decider.calls + self.second.decider.calls, 1)
        self.assertEqual(self.first.store.status(2), "previewed")
        self.assertEqual(self.second.store.status(2), "reviewed")
        self.assertEqual(self.first.gateway.sent + self.second.gateway.sent, [])
        self.assertTrue(self.coordinator.is_claimed(-100, 2))
        self.clock.value += 600
        await self.second.process_once()
        self.assertEqual(self.second.store.status(2), "reviewed")
        self.assertEqual(self.second.decider.calls, 0)

    async def test_group_target_claim_blocks_duplicates_without_cooldown(self):
        self.add_meeting()
        await self.first.process_once()
        self.assertNotEqual(self.first.store.path, self.second.store.path)
        self.assertEqual(self.first.store.quota_wait(self.clock()), 0)
        self.assertEqual(self.second.store.quota_wait(self.clock()), 0)
        self.assertEqual(self.second.delay(), 0)
        self.assertEqual(self.coordinator.quota_remaining(-100, self.clock()), 4)
        for delta in (1, 60, 599):
            self.clock.value = 10000 + delta
            await self.second.process_once()
        self.assertEqual(self.second.decider.calls, 0)

    async def test_group_five_message_cap_blocks_both_account_workers(self):
        history = ["First existing group preview", "Second existing group preview",
                   "Third existing group preview", "Fourth existing group preview",
                   "Fifth existing group preview"]
        for index, text in enumerate(history):
            self.seed_history("one" if index % 2 == 0 else "two", 100 + index,
                              text, 10000 + index * 600)
        self.clock.value = 13000
        self.add_meeting()
        delays = await asyncio.gather(self.first.process_once(), self.second.process_once())
        self.assertEqual(delays, [600, 600])
        self.assertEqual(self.first.decider.calls + self.second.decider.calls, 0)
        self.assertEqual(self.first.store.quota_wait(self.clock()), 0)
        self.assertEqual(self.second.store.quota_wait(self.clock()), 0)
        self.clock.value = 13600
        await self.second.process_once()
        self.assertEqual(self.second.decider.calls, 1)
        self.assertEqual(self.second.store.status(2), "previewed")

    async def test_different_chats_have_independent_group_quotas(self):
        other = self.make_worker("two", 22, chat_id=-200)
        self.add_meeting(chat_id=-100)
        self.add_meeting(chat_id=-200)
        await asyncio.gather(self.first.process_once(), other.process_once())
        self.assertEqual(self.first.store.status(2), "previewed")
        self.assertEqual(other.store.status(2), "previewed")
        self.assertEqual(self.first.decider.calls, 1)
        self.assertEqual(other.decider.calls, 1)

    async def test_cross_account_repeat_rejected_after_cooldown(self):
        self.add_meeting()
        await self.first.process_once()
        self.clock.value += 600
        self.add_meeting(target_id=4)
        await self.second.process_once()
        self.assertEqual(self.second.decider.calls, 1)
        self.assertEqual(self.second.store.status(4), "reviewed")
        self.assertFalse(self.coordinator.is_claimed(-100, 4))
        self.assertEqual(self.second.store.quota_wait(self.clock()), 0)
        self.assertIn("The meeting starts at eight tonight", self.second.decider.recent)

    async def test_live_targeting_is_correct_and_shared_budget_prevents_peer_send(self):
        self.first.settings = replace(self.first.settings, auto_send=True)
        self.second.settings = replace(self.second.settings, auto_send=True)
        self.add_meeting(target_id=8)
        await asyncio.gather(self.first.process_once(), self.second.process_once())
        sent = self.first.gateway.sent + self.second.gateway.sent
        self.assertEqual(sent, [("The meeting starts at eight tonight", 8)])
        self.assertEqual(len(self.coordinator.recent_replies(-100, self.clock())), 1)

    async def test_shared_history_order_keeps_newest_question_in_style_window(self):
        history = ["Existing oldest answer", "Another earlier group contribution",
                   "An older peer observation", "A different previous answer",
                   "Which payment notice do you mean?"]
        for index, text in enumerate(history):
            self.seed_history("one" if index in (0, 4) else "two", 100 + index,
                              text, 10000 + index * 600, local=index in (0, 4))
        self.clock.value = 13600
        self.assertEqual(self.first.recent_replies(), history)
        self.assertEqual(self.first.recent_replies()[-1], history[-1])
        target = Message(500, -100, 88, "Which payment notice mentions the deadline?", self.clock())
        self.add(target)
        self.first.decider.text = "What deadline does the payment notice mention?"
        await self.first.process_once()
        self.assertIn(history[-1], self.first.decider.recent[-3:])
        self.assertFalse(self.coordinator.is_claimed(-100, 500))
        self.assertEqual(self.first.store.status(500), "reviewed")

    async def test_manual_peer_message_is_shared_once_across_observers(self):
        manual = Message(40, -100, 11, "The payment notice arrived today", self.clock(),
                         outgoing=False, reply_to_id=9)
        self.add(manual)
        await asyncio.gather(self.first.process_once(), self.second.process_once())
        self.assertEqual(self.coordinator.recent_replies(-100, self.clock()), [manual.text])
        self.assertEqual(self.coordinator.quota_wait(-100, self.clock()), 0)
        self.assertEqual(self.first.store.quota_wait(self.clock()), 0)
        self.assertEqual(self.second.store.quota_wait(self.clock()), 0)
        self.assertTrue(self.coordinator.is_claimed(-100, 9))
        self.assertEqual(self.first.decider.calls + self.second.decider.calls, 0)

    async def test_manually_repeated_question_remains_in_newest_style_window(self):
        history = ["Which payment notice do you mean?", "Another earlier group contribution",
                   "An older peer observation", "A different previous answer",
                   "Which payment notice do you mean?"]
        for index, text in enumerate(history):
            self.seed_history("one" if index in (0, 4) else "two", 100 + index,
                              text, 10000 + index * 600, local=index in (0, 4))
        self.clock.value = 13600
        self.assertEqual(self.first.recent_replies(), history)
        self.assertIn(history[-1], self.first.recent_replies()[-3:])

    async def test_restart_imports_existing_local_preview_without_duplicate_group_slot(self):
        self.add_meeting()
        await self.first.process_once()
        self.coordinator.close()
        self.coordinator = Coordinator(self.coordinator_path)
        for worker in self.workers:
            worker.coordinator = self.coordinator
            self.coordinator.import_activity(worker.settings.account_id, worker.settings.chat_id,
                                             worker.store.export_actions(self.clock()))
        self.assertEqual(len(self.coordinator.recent_replies(-100, self.clock())), 1)
        self.clock.value += 1
        await self.second.process_once()
        self.assertEqual(self.second.delay(), 0)
        self.assertEqual(self.coordinator.quota_remaining(-100, self.clock()), 4)
        self.assertEqual(self.second.decider.calls, 0)
        self.assertTrue(self.coordinator.is_claimed(-100, 2))

    async def test_canceled_target_does_not_consume_group_reservation(self):
        self.add_meeting()

        async def answered():
            self.add(Message(3, -100, 77, "The meeting starts at eight tonight", self.clock() + 1,
                             reply_to_id=2, reply_to_sender_id=88))

        self.first.decider.callback = answered
        await self.first.process_once()
        self.assertEqual(self.first.store.status(2), "reviewed")
        self.assertFalse(self.coordinator.is_claimed(-100, 2))
        self.assertEqual(self.coordinator.quota_wait(-100, self.clock()), 0)
        self.assertEqual(self.first.store.quota_wait(self.clock()), 0)

    async def test_deletion_during_inference_does_not_claim_target(self):
        self.add_meeting()

        async def deleted():
            self.messages[-100].pop(2)

        self.first.decider.callback = deleted
        await self.first.process_once()
        self.assertEqual(self.first.store.status(2), "reviewed")
        self.assertFalse(self.coordinator.is_claimed(-100, 2))
        self.assertEqual(self.coordinator.quota_wait(-100, self.clock()), 0)


if __name__ == "__main__":
    unittest.main()
