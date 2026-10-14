import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from challenger.config import Settings
from challenger.engine import Refresh, TelegramBackoff, Worker
from challenger.groq_client import GroqBackoff
from challenger.models import Identity, Message
from challenger.storage import Store


class Clock:
    value = 10000.0

    def __call__(self):
        return self.value


def question(message_id=1, at=10000):
    return Message(message_id, -100, 22, "@challenger are you a bot?", at,
                   mentions=("challenger",))


class Decider:
    def __init__(self):
        self.calls = 0
        self.target = 1
        self.callback = None

    async def decide(self, candidates, context, recent):
        self.calls += 1
        if self.callback:
            await self.callback()
        return {"action": "REPLY", "target_id": self.target,
                "text": "Yes, this account is automated", "emoji": None,
                "reason": "Direct automation question"}


class Gateway:
    def __init__(self):
        self.messages = {}
        self.sent = []
        self.error = None

    async def sync(self):
        return list(self.messages.values())

    async def refresh(self, target_id):
        return Refresh(self.messages.get(target_id), list(self.messages.values()))

    async def send(self, text, target_id):
        if self.error:
            raise self.error
        self.sent.append((text, target_id))
        return Message(500, -100, 11, text, 10000, reply_to_id=target_id, outgoing=True)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.clock = Clock()
        self.settings = Settings(1, "test", -100, "test", "test")
        self.store = Store(self.path, "account_1", -100)
        self.gateway = Gateway()
        self.decider = Decider()
        self.worker = Worker(self.settings, self.store, Identity(11, "challenger"),
                             self.decider, self.gateway, self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, message):
        self.gateway.messages[message.message_id] = message
        self.worker.ingest(message)

    async def test_preview_records_quota_and_rejects_repetitive_proposal(self):
        self.add(question())
        with self.assertLogs("challenger.engine", level="INFO") as logged:
            self.assertEqual(await self.worker.process_once(), 0)
        messages = "\n".join(logged.output)
        self.assertIn("EVALUATING", messages)
        self.assertIn("PROPOSED", messages)
        self.assertIn("SIMULATED REPLY", messages)
        self.assertNotIn("SENT REPLY", messages)
        self.assertEqual(self.store.status(1), "previewed")
        self.assertEqual(self.gateway.sent, [])
        self.add(question(2))
        self.decider.target = 2
        for seconds in (1, 50, 599):
            self.clock.value = 10000 + seconds
            await self.worker.process_once()
        self.assertEqual(self.decider.calls, 2)
        self.assertEqual(self.store.status(2), "reviewed")
        self.assertEqual(self.store.quota_remaining(self.clock()), 4)
        self.clock.value = 10600
        await self.worker.process_once()
        self.assertEqual(self.decider.calls, 2)

    async def test_live_reply_targets_selected_id_and_quota_survives_restart(self):
        self.worker.settings = replace(self.settings, auto_send=True)
        self.add(question(1))
        self.add(question(2))
        self.decider.target = 2
        await self.worker.process_once()
        self.assertEqual(self.gateway.sent[0][1], 2)
        self.assertEqual(self.store.status(2), "sent")
        self.assertEqual(self.store.status(1), "reviewed")
        self.store.close()
        self.store = Store(self.path, "account_1", -100)
        self.assertEqual(self.store.quota_wait(10001), 0)
        self.assertEqual(self.store.quota_remaining(10001), 4)
        self.worker.store = self.store
        self.add(question(3))
        await self.worker.process_once()
        self.assertEqual(len(self.gateway.sent), 1)

    async def test_ingestion_continues_while_groq_waits(self):
        self.add(question())
        started, release = asyncio.Event(), asyncio.Event()

        async def wait():
            started.set()
            await release.wait()

        self.decider.callback = wait
        task = asyncio.create_task(self.worker.process_once())
        await started.wait()
        for message_id in range(2, 102):
            self.worker.ingest(Message(message_id, -100, 33, "Sounds like a joke", 10001))
        self.assertEqual(len(self.store.pending(self.clock(), 200)), 101)
        release.set()
        await task
        self.assertEqual(len(self.store.pending(self.clock(), 200)), 100)
        self.assertEqual(self.store.status(1), "previewed")

    async def test_answer_arrives_during_inference_cancels_reply(self):
        self.add(question())

        async def answer():
            self.add(Message(2, -100, 33, "This account is automated", 10001,
                             reply_to_id=1, reply_to_sender_id=22))

        self.decider.callback = answer
        await self.worker.process_once()
        self.assertEqual(self.store.status(1), "reviewed")
        self.assertEqual(self.store.quota_wait(10000), 0)

    async def test_answer_outside_normal_context_window_still_cancels_reply(self):
        self.add(question())

        async def answer():
            self.add(Message(2, -100, 33, "This account is automated", 10001,
                             reply_to_id=1, reply_to_sender_id=22))
            for message_id in range(3, 103):
                self.add(Message(message_id, -100, 44, "yeah", 10002))

        self.decider.callback = answer
        await self.worker.process_once()
        self.assertEqual(self.store.quota_wait(10000), 0)
        self.assertEqual(self.store.status(1), "reviewed")

    async def test_edit_during_inference_invalidates_snapshot(self):
        self.add(question())

        async def edit():
            self.add(replace(question(), text="Enjoy your purchases", edited_at=10001))

        self.decider.callback = edit
        await self.worker.process_once()
        self.assertEqual(self.store.status(1), "pending")
        self.assertEqual(self.store.quota_wait(10000), 0)

    async def test_deleted_target_is_never_sent(self):
        self.add(question())

        async def delete():
            self.gateway.messages.clear()

        self.decider.callback = delete
        await self.worker.process_once()
        self.assertEqual(self.store.status(1), "reviewed")
        self.assertEqual(self.store.quota_wait(10000), 0)

    async def test_manual_outgoing_during_inference_blocks_send(self):
        for index in range(4):
            self.store.observe_outgoing(Message(100 + index, -100, 11, f"Earlier text {index}", 9990, outgoing=True))
        self.add(question())

        async def manual():
            self.add(Message(2, -100, 11, "The chat is busy", 10000, outgoing=True))

        self.decider.callback = manual
        await self.worker.process_once()
        self.assertEqual(self.store.status(1), "pending")
        self.assertEqual(self.store.quota_wait(10000), 3590)

    async def test_floodwait_full_duration_is_persistent(self):
        self.worker.settings = replace(self.settings, auto_send=True)
        self.gateway.error = TelegramBackoff(7200)
        self.add(question())
        await self.worker.process_once()
        self.assertEqual(self.store.blocked_until("telegram"), 17200)
        self.assertEqual(self.store.status(1), "rejected")
        self.assertEqual(self.store.quota_remaining(10000), 5)
        self.store.close()
        self.store = Store(self.path, "account_1", -100)
        self.assertEqual(self.store.blocked_until("telegram"), 17200)

    async def test_uncertain_send_is_not_retried(self):
        self.worker.settings = replace(self.settings, auto_send=True)
        self.gateway.error = TimeoutError()
        self.add(question())
        await self.worker.process_once()
        self.assertEqual(self.store.status(1), "uncertain")
        self.assertEqual(self.store.quota_wait(10000), 0)
        self.clock.value = 10600
        await self.worker.process_once()
        self.assertEqual(self.decider.calls, 1)

    async def test_rate_limit_preserves_queue_and_persists_backoff(self):
        self.add(question())

        async def rate_limit():
            raise GroqBackoff(3600)

        self.decider.callback = rate_limit
        await self.worker.process_once()
        self.assertEqual(self.store.status(1), "pending")
        self.assertEqual(self.store.blocked_until("groq"), 13600)
        await self.worker.process_once()
        self.assertEqual(self.decider.calls, 1)

    async def test_expired_and_generic_messages_never_call_groq(self):
        self.add(replace(question(), sent_at=9000))
        for message_id, text in enumerate(("Not spamming, just sharing thoughts",
                                          "Citizens truly bear the brunt", "Sounds like a joke",
                                          "Enjoy your purchases", "Elon's Mars plans are ambitious",
                                          "Glad to hear that"), start=2):
            self.add(Message(message_id, -100, 22, text, 10000))
        await self.worker.process_once()
        self.assertEqual(self.decider.calls, 0)
        self.assertEqual(self.store.status(1), "expired")

    async def test_processed_message_cannot_be_reenqueued_by_history(self):
        self.add(question())
        await self.worker.process_once()
        self.clock.value = 10600
        self.worker.ingest(question())
        await self.worker.process_once()
        self.assertEqual(self.decider.calls, 1)


if __name__ == "__main__":
    unittest.main()
