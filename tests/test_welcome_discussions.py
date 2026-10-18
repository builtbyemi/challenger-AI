from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from challenger.models import Identity, Message
from challenger.selection import skip_reason, validate_decision, welcome_history_reason
from challenger.storage import Store


class WelcomeDiscussionTests(unittest.TestCase):
    identity = Identity(100, "challenger")

    def welcome(self, **changes):
        return replace(Message(20, -200, 2, "Welcome @Ada to the community", 990,
                               is_bot=True, mentions=("Ada",)), **changes)

    def validate(self, target, text="Welcome to this community", context=None, recent=None):
        return validate_decision(dict(action="REPLY", target_id=target.message_id,
                                      text=text, emoji=None, reason="Welcome a new arrival"),
                                 [target], context or [], self.identity, recent or [], now=1000)

    def test_welcome_bot_notice_can_receive_correctly_targeted_greeting(self):
        result = self.validate(self.welcome())
        self.assertEqual(result.action, "REPLY", result.reason)
        self.assertEqual(result.target_id, 20)

    def test_welcome_not_every_time_even_with_different_wording(self):
        result = self.validate(self.welcome(), recent=["Welcome to the group"])
        self.assertEqual(result.action, "IGNORE")
        self.assertIn("last five", result.reason)
        recent = ["Welcome to this community"] + [f"Relevant contribution {i}" for i in range(5)]
        self.assertEqual(self.validate(self.welcome(), recent=recent).action, "REPLY")

    def test_welcome_does_not_allow_promotions_or_invented_claims(self):
        for text in ("Welcome @Ada to the community https://spam.example",
                     "Welcome @Ada to the community, send money", "This bot has random words"):
            self.assertIsNotNone(skip_reason(self.welcome(text=text), [], self.identity, 1000))
        self.assertEqual(self.validate(self.welcome(), "This community guarantees huge profits").action, "IGNORE")

    def test_stale_and_already_welcomed_arrivals_are_ignored(self):
        self.assertEqual(self.validate(self.welcome(sent_at=800)).action, "IGNORE")
        greeting = Message(21, -200, 3, "Welcome Ada", 995, reply_to_id=20)
        self.assertEqual(self.validate(self.welcome(), context=[greeting]).action, "IGNORE")

    def test_welcome_history_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = Store(path, "account_1", -200)
            store.observe_outgoing(Message(30, -200, 100, "Welcome to this community", 995, outgoing=True))
            store.close()
            store = Store(path, "account_1", -200)
            try:
                self.assertIsNotNone(welcome_history_reason(self.welcome(), store.recent_replies(1000)))
            finally:
                store.close()

    def test_public_thread_with_three_members_is_eligible_without_mention(self):
        target = Message(20, -200, 2, "This transfer costs 500 naira for sending 200 naira", 990,
                         reply_to_id=19, reply_to_sender_id=3)
        context = [Message(19, -200, 3, "The transfer fee is higher than the amount", 980),
                   Message(18, -200, 4, "Transfer charges keep increasing every week", 970,
                           reply_to_id=17, reply_to_sender_id=3)]
        self.assertIsNone(skip_reason(target, context, self.identity, 1000))
        result = self.validate(target, "The fee is higher than the amount", context=context)
        self.assertEqual(result.action, "REPLY", result.reason)
        self.assertIsNotNone(skip_reason(target, context[:1], self.identity, 1000))
        self.assertIsNotNone(skip_reason(replace(target, text="@Ada your transfer costs too much"),
                                         context, self.identity, 1000))
