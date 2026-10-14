import unittest
from dataclasses import replace

from challenger.models import Identity, Message
from challenger.selection import skip_reason, validate_decision


class DiscussionTests(unittest.TestCase):
    def setUp(self):
        self.identity = Identity(100, "challenger")
        self.target = Message(20, -200, 2, "This transfer costs 500 naira for sending 200 naira", 990)
        self.context = [Message(19, -200, 3, "The transfer fee is higher than the amount", 980)]

    def test_open_statement_needs_no_question_or_mention(self):
        self.assertIsNone(skip_reason(self.target, self.context, self.identity, 1000))

    def test_grounded_natural_contribution_can_pass(self):
        raw = dict(action="REPLY", target_id=20, text="The fee is higher than the amount",
                   emoji=None, reason="Relevant transfer cost comparison")
        result = validate_decision(raw, [self.target], self.context, self.identity, [], now=1000)
        self.assertEqual(result.action, "REPLY", result.reason)
        repeated = validate_decision(raw, [self.target], self.context, self.identity,
                                     [raw["text"]], now=1000)
        self.assertEqual(repeated.action, "IGNORE")

    def test_ongoing_discussion_is_not_an_answered_question(self):
        later = replace(self.context[0], message_id=21, sent_at=995)
        self.assertIsNone(skip_reason(self.target, [later], self.identity, 1000))

    def test_private_exchange_stays_filtered(self):
        target = replace(self.target, reply_to_id=19, reply_to_sender_id=3)
        self.assertIsNotNone(skip_reason(target, self.context, self.identity, 1000))
        private_context = [replace(self.context[0], reply_to_id=18, reply_to_sender_id=2)]
        self.assertIsNotNone(skip_reason(self.target, private_context, self.identity, 1000))

    def test_stale_unrelated_or_own_context_cannot_open_discussion(self):
        for context in ([], [replace(self.context[0], sent_at=800)],
                        [replace(self.context[0], text="The meeting starts at eight tonight")],
                        [replace(self.context[0], sender_id=100)],
                        [replace(self.context[0], sender_id=2)],
                        [replace(self.context[0], is_bot=True)]):
            self.assertIsNotNone(skip_reason(self.target, context, self.identity, 1000))

    def test_generic_reactions_stay_ignored_during_discussion(self):
        for text in ("Sounds like a joke", "Enjoy your purchases", "Glad to hear that",
                     "Citizens truly bear the brunt", "Elon's Mars plans are ambitious",
                     "Not spamming, just sharing thoughts"):
            target = replace(self.target, text=text)
            context = [replace(self.context[0], text=text + " today")]
            self.assertIsNotNone(skip_reason(target, context, self.identity, 1000))

    def test_pidgin_discussion_can_be_considered(self):
        target = replace(self.target, text="This transfer fee dey pass the money itself")
        context = [replace(self.context[0], text="Na transfer fee cause this wahala today")]
        self.assertIsNone(skip_reason(target, context, self.identity, 1000))

    def test_repeated_observation_does_not_invite_another_reply(self):
        context = [replace(self.context[0], text=self.target.text)]
        self.assertIsNotNone(skip_reason(self.target, context, self.identity, 1000))

    def test_old_discussion_does_not_reopen_after_cooldown(self):
        self.assertIsNotNone(skip_reason(self.target, self.context, self.identity, 1600))
