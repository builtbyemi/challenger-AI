from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from challenger.config import Settings, load_application_settings, load_settings
from challenger.coordinator import Coordinator
from challenger.engine import Refresh, Worker
from challenger.models import Identity, Message
from challenger.storage import Store


TOPICS = [
    ("What time does the group meeting start?", "The meeting starts at eight tonight", "It starts at eight tonight"),
    ("Where is the parcel collection point?", "The parcel collection point is the gate", "Collection point is the gate"),
    ("What caused the transfer fee change?", "The transfer fee changed because network charges increased", "The network charges increased"),
    ("Where will the football match happen?", "The football match is at the stadium", "Match is at the stadium"),
    ("What does the rent notice mention?", "The rent notice includes water payments", "Rent includes water payments"),
    ("Which room has the library books?", "The library books are in room three", "Books are in room three"),
]


class Service:
    def __init__(self):
        self.now = 10000.0
        self.messages = {}
        self.answers = {}
        self.sent = []
        self.calls = 0

    async def sync(self):
        return list(self.messages.values())

    async def refresh(self, target_id):
        return Refresh(self.messages.get(target_id), list(self.messages.values()))

    async def decide(self, candidates, context, recent):
        self.calls += 1
        target = candidates[0].message_id
        return dict(action="REPLY", target_id=target, text=self.answers[target], emoji=None,
                    reason="Specific answer grounded in earlier discussion")

    async def send(self, text, target_id):
        self.sent.append((text, target_id, self.now))
        return Message(1000 + len(self.sent), -100, 11, text, self.now, outgoing=True, reply_to_id=target_id)


class HourlyOnlyTests(unittest.IsolatedAsyncioTestCase):
    def test_obsolete_gap_environment_variables_cannot_restore_cooldown(self):
        environment = dict(API_ID="1", API_HASH="fake", TARGET_CHAT_ID="-100", GROQ_API_KEY="fake",
                           MIN_GAP_SECONDS="not-a-number", GROUP_MIN_GAP_SECONDS="999999")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, environment, clear=True):
            single = load_settings(Path(directory))
            app = load_application_settings(Path(directory))
        self.assertFalse(hasattr(single, "min_gap"))
        self.assertFalse(hasattr(app.accounts[0], "min_gap"))
        self.assertFalse(hasattr(app, "group_min_gap"))
        self.assertEqual(app.group_max_actions, 25)

    async def test_two_valid_replies_seconds_apart_five_cap_and_restart_in_both_modes(self):
        for live in (False, True):
            with self.subTest(live=live), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                service = Service()
                store = Store(path / "account.sqlite3", "one", -100)
                group = Coordinator(path / "group.sqlite3")
                group.register("one", 11)
                settings = Settings(1, "fake", -100, "fake", "fake", account_id="one", auto_send=live)
                worker = Worker(settings, store, Identity(11), service, service,
                                clock=lambda: service.now, coordinator=group)

                def add(index, number):
                    question, evidence, answer = TOPICS[index]
                    source = Message(number - 1, -100, 30 + index, evidence, service.now - 2)
                    target = Message(number, -100, 80 + index, question, service.now)
                    for message in (source, target):
                        service.messages[message.message_id] = message
                        worker.ingest(message)
                    store.mark([source], "reviewed", "Existing conversation context")
                    service.answers[number] = answer

                try:
                    for index in range(5):
                        service.now = 10000 + index * 30
                        add(index, index * 10 + 2)
                        with self.assertLogs("challenger.engine", level="INFO") as logs:
                            await worker.process_once()
                        self.assertIn(f"remaining={4-index}/5", "\n".join(logs.output))
                        self.assertEqual(store.status(index * 10 + 2), "sent" if live else "previewed")
                    self.assertEqual(len(service.sent), 5 if live else 0)
                    if live:
                        self.assertEqual(service.sent[1][2] - service.sent[0][2], 30)
                    service.now = 10150
                    add(5, 52)
                    await worker.process_once()
                    self.assertEqual(service.calls, 5)
                    self.assertEqual(store.quota_wait(service.now), 3450)
                    store.close()
                    group.close()
                    store = Store(path / "account.sqlite3", "one", -100)
                    group = Coordinator(path / "group.sqlite3")
                    worker.store, worker.coordinator = store, group
                    await worker.process_once()
                    self.assertEqual(service.calls, 5)
                    self.assertEqual(group.quota_remaining(-100, service.now), 0)
                    service.now = 13600
                    add(5, 62)
                    await worker.process_once()
                    self.assertEqual(service.calls, 6)
                    self.assertEqual(store.quota_remaining(service.now), 0)
                    self.assertEqual(group.quota_remaining(-100, service.now), 0)
                finally:
                    store.close()
                    group.close()

    async def test_definite_rejection_releases_slot_but_unknown_outcome_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "account.sqlite3", "one", -100)
            group = Coordinator(Path(directory) / "group.sqlite3")
            group.register("one", 11)
            try:
                for index in range(5):
                    message = Message(index + 1, -100, 30, "A concrete group question", 10000)
                    store.enqueue(message)
                    action = store.reserve(message, TOPICS[index][2], "live", 10000)
                    claim = group.reserve("one", -100, index + 1, TOPICS[index][2], "live", 10000)
                    state = "rejected" if index == 0 else "uncertain"
                    store.finish(action, state, 10000)
                    group.finish(claim, state, 10000)
                self.assertEqual(store.quota_remaining(10000), 1)
                self.assertEqual(group.quota_remaining(-100, 10000), 1)
                self.assertEqual(store.quota_wait(10000), 0)
                self.assertEqual(group.quota_wait(-100, 10000), 0)
            finally:
                store.close()
                group.close()
