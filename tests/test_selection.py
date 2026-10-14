import unittest
from dataclasses import replace

from challenger.models import Identity, Message
from challenger.selection import bot_reply, is_direct_bot_question, skip_reason, validate_decision


NOW = 10000.0
ME = Identity(100, "challenger")


def message(text="What time does the group meeting start?", **kwargs):
    values = dict(message_id=20, chat_id=-200, sender_id=2, text=text, sent_at=NOW - 10)
    values.update(kwargs)
    return Message(**values)


def reply(text="It starts at eight tonight", **kwargs):
    result = dict(action="REPLY", target_id=20, text=text, emoji=None, reason="Specific answer available in context")
    result.update(kwargs)
    return result


class SelectionTests(unittest.TestCase):
    def skip(self, candidate, context=None):
        return skip_reason(candidate, context or [], ME, NOW)

    def validate(self, raw, candidate=None, context=None, recent=None):
        if context is None:
            context = [message("The meeting starts at eight tonight, same venue", message_id=19, sender_id=3, sent_at=NOW - 30)]
        return validate_decision(raw, [candidate or message()], context, ME, recent or [], now=NOW)

    def test_user_bad_examples_are_ignored(self):
        for text in (
            "Not spamming, just sharing thoughts", "Citizens truly bear the brunt",
            "Sounds like a joke", "Enjoy your purchases", "Elon's Mars plans are ambitious", "Glad to hear that",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(self.skip(message(text)))

    def test_statements_and_reactions_remain_ignored_when_addressed(self):
        for text in ("That price is really high", "Thanks for sharing", "Okay", "😂😂😂", "Sounds like a joke?"):
            with self.subTest(text=text):
                self.assertIsNotNone(self.skip(message(text, reply_to_id=1, reply_to_sender_id=ME.user_id)))

    def test_clear_open_question_can_be_considered(self):
        self.assertIsNone(self.skip(message()))
        self.assertIsNone(self.skip(message("Wetin make the meeting time change?")))

    def test_direct_question_can_be_considered_without_question_mark(self):
        self.assertIsNone(self.skip(message("Can you explain the SQLite error", reply_to_id=1, reply_to_sender_id=100)))
        self.assertIsNone(self.skip(message("@challenger what time does the meeting start", mentions=("challenger",))))

    def test_unaddressed_second_person_and_vague_questions_are_ignored(self):
        for text in ("Can you explain the SQLite error?", "What happened?", "What is this?", "Any thoughts?", "Wetin happen for there?", "John, what time does the meeting start?"):
            with self.subTest(text=text):
                self.assertIsNotNone(self.skip(message(text)))

    def test_other_member_threads_are_ignored_even_with_good_question(self):
        self.assertIsNotNone(self.skip(message(reply_to_id=10, reply_to_sender_id=3)))
        self.assertIsNotNone(self.skip(message("@other what time does the group meeting start?", mentions=("other",))))
        self.assertIsNotNone(self.skip(message(mentioned_ids=(3,))))

    def test_bot_keywords_alone_do_not_trigger_replies(self):
        for text in ("Are you a bot?", "Who is spamming this busy group?", "Why is that bot posting random words?", "@challenger you are spamming random words", "@challenger are you spamming?"):
            with self.subTest(text=text):
                self.assertFalse(is_direct_bot_question(message(text), ME))
                self.assertIsNotNone(self.skip(message(text)))

    def test_bot_question_requires_real_addressing_evidence(self):
        variants = (
            message("Are you a bot?", reply_to_id=1, reply_to_sender_id=100),
            message("@challenger are you automated?"),
            message("Are you a robot?", mentioned_ids=(100,)),
            message("You be bot?", reply_to_id=1, reply_to_sender_id=100),
            message("Are you human?", reply_to_id=1, reply_to_sender_id=100),
            message("Is this account run by a human?", mentioned_ids=(100,)),
        )
        for candidate in variants:
            with self.subTest(candidate=candidate):
                self.assertTrue(is_direct_bot_question(candidate, ME))
                self.assertIsNone(self.skip(candidate))

    def test_bot_disclosure_is_truthful_and_language_matched(self):
        english = message("Are you a bot?", reply_to_id=1, reply_to_sender_id=100)
        pidgin = message("Shey you be bot?", reply_to_id=1, reply_to_sender_id=100)
        for candidate in (english, pidgin):
            result = self.validate(reply("I am a real human"), candidate=candidate)
            self.assertEqual(result.action, "REPLY")
            self.assertEqual(result.text, bot_reply(candidate))
            self.assertIn("automated", result.text)
        self.assertIn("na", bot_reply(pidgin).lower())
        self.assertNotIn(" na ", bot_reply(english))

    def test_bot_disclosure_still_rejects_repetition(self):
        candidate = message("Are you a bot?", reply_to_id=1, reply_to_sender_id=100)
        self.assertEqual(self.validate(reply(), candidate=candidate, recent=[bot_reply(candidate)]).action, "IGNORE")

    def test_expired_forwarded_bot_and_own_messages_are_ignored(self):
        for candidate in (
            message(sent_at=NOW - 720), message(sent_at=NOW + 40), message(is_bot=True),
            message(forwarded=True), message(outgoing=True), message(sender_id=100),
        ):
            self.assertIsNotNone(self.skip(candidate))

    def test_incomplete_messages_and_promotions_are_ignored(self):
        for text in ("What about the...", "Why this", "What time does the giveaway start?", "Can anyone join our amazing project?", "What about https://spam.example?"):
            self.assertIsNotNone(self.skip(message(text)))

    def test_question_already_answered_is_ignored(self):
        candidate = message()
        answer = message("It starts at eight tonight", message_id=21, sender_id=3, sent_at=NOW - 5, reply_to_id=20, reply_to_sender_id=2)
        self.assertIsNotNone(self.skip(candidate, [answer]))
        self.assertEqual(self.validate(reply(), context=[answer]).action, "IGNORE")

    def test_emoji_reaction_does_not_count_as_answer(self):
        reaction = message("😂", message_id=21, sender_id=3, sent_at=NOW - 5, reply_to_id=20)
        self.assertIsNone(self.skip(message(), [reaction]))

    def test_unthreaded_topic_answer_or_short_answer_closes_question(self):
        for text in ("The group meeting starts at eight", "It's at eight tonight"):
            answer = message(text, message_id=21, sender_id=3, sent_at=NOW - 5)
            self.assertIsNotNone(self.skip(message(), [answer]))

    def test_author_can_resolve_own_question_without_thread_reply(self):
        followup = message("Found it, the meeting starts at eight", message_id=21, sent_at=NOW - 2)
        self.assertIsNotNone(self.skip(message(), [followup]))
        unrelated = replace(followup, chat_id=-999)
        self.assertIsNone(self.skip(message(), [unrelated]))

    def test_nearby_back_and_forth_is_ignored(self):
        context = [
            message("The venue was changed", message_id=18, sent_at=NOW - 30, sender_id=2, reply_to_id=16, reply_to_sender_id=3),
            message("I saw the notice", message_id=19, sent_at=NOW - 20, sender_id=3, reply_to_id=18, reply_to_sender_id=2),
        ]
        self.assertIsNotNone(self.skip(message(), context))
        self.assertIsNone(self.skip(message("Anyone know what time the group meeting starts?"), context))

    def test_invalid_schema_or_types_fail_closed(self):
        malformed = [None, [], {}, reply(action="reply"), reply(action=True), reply(target_id=True), reply(target_id="20"), reply(target_id=0), reply(text=[]), reply(emoji=[]), reply(reason=5), reply(extra="instruction")]
        for raw in malformed:
            with self.subTest(raw=raw):
                self.assertEqual(self.validate(raw).action, "IGNORE")

    def test_ignore_content_must_be_null(self):
        result = self.validate(dict(action="IGNORE", target_id=None, text=None, emoji=None, reason="Nothing to add"))
        self.assertEqual(result.reason, "Nothing to add")
        self.assertEqual(self.validate(reply(action="IGNORE")).reason, "IGNORE must not include reply content")

    def test_reply_targets_exact_candidate_id(self):
        self.assertEqual(self.validate(reply(target_id=999)).action, "IGNORE")
        result = self.validate(reply())
        self.assertEqual(result.action, "REPLY")
        self.assertEqual(result.target_id, 20)

    def test_punctuation_preserves_apostrophes_and_commas(self):
        result = self.validate(reply("It's eight tonight, same venue!!!."))
        self.assertEqual(result.action, "REPLY")
        self.assertEqual(result.text, "It's eight tonight, same venue")

    def test_generic_and_formal_filler_is_rejected(self):
        for text in ("Thanks for sharing this", "Sounds like a good plan", "That is really interesting", "Keep pushing through it", "I agree with you", "Indeed, that is quite commendable"):
            with self.subTest(text=text):
                self.assertEqual(self.validate(reply(text)).action, "IGNORE")

    def test_exact_and_near_duplicate_reply_is_rejected(self):
        for text in ("It starts at eight tonight", "It starts at eight tonight 😅", "It starts at eight tonight, yes"):
            with self.subTest(text=text):
                self.assertEqual(self.validate(reply(text), recent=["It starts at eight tonight"]).action, "IGNORE")

    def test_reply_length_and_emoji_are_validated(self):
        for text in ("Eight tonight", "The meeting is planned to start at eight tonight"):
            self.assertEqual(self.validate(reply(text)).action, "IGNORE")
        self.assertEqual(self.validate(reply(emoji="😂😂")).action, "IGNORE")
        self.assertEqual(self.validate(reply(emoji="not an emoji")).action, "IGNORE")
        self.assertEqual(self.validate(reply("It starts at eight tonight 😂", emoji="👍")).action, "IGNORE")
        self.assertEqual(self.validate(reply(emoji="😅")).action, "REPLY")

    def test_personal_experience_and_identity_are_rejected(self):
        for text in ("I bought the same one", "I'm a real human here", "My name is John", "I live in Lagos now", "We are all from Nigeria"):
            with self.subTest(text=text):
                self.assertEqual(self.validate(reply(text)).action, "IGNORE")

    def test_english_and_pidgin_remain_natural_to_target(self):
        pidgin = message("Wetin make this meeting time change?")
        context = [message("Meeting change na the venue issue cause am", message_id=19, sender_id=3, sent_at=NOW - 30)]
        self.assertEqual(self.validate(reply("Na the venue issue cause am"), candidate=pidgin, context=context).action, "REPLY")
        self.assertEqual(self.validate(reply("Na the venue issue cause am")).action, "IGNORE")
        self.assertEqual(self.validate(reply("It starts at eight tonight"), candidate=pidgin).action, "IGNORE")

    def test_unsupported_numbers_and_named_entities_rejected(self):
        self.assertEqual(self.validate(reply("It starts at 8pm tonight")).action, "IGNORE")
        self.assertEqual(self.validate(reply("The venue moved to Lagos")).action, "IGNORE")
        context = [message("Meeting notice says it starts at 8pm tonight", message_id=19, sender_id=3, sent_at=NOW - 30)]
        self.assertEqual(self.validate(reply("It starts at 8pm tonight"), context=context).action, "REPLY")

    def test_unrelated_context_cannot_ground_fabricated_answer(self):
        context = [message("Dinner reservation is at 8pm tonight", message_id=19, sender_id=3, sent_at=NOW - 30)]
        self.assertEqual(self.validate(reply("It starts at 8pm tonight"), context=context).action, "IGNORE")
        self.assertEqual(self.validate(reply("The exchange is binance"), context=[]).action, "IGNORE")
        self.assertEqual(self.validate(reply("It starts at eight tonight"), context=[]).action, "IGNORE")

    def test_followup_questions_cannot_introduce_unsupported_claims(self):
        for text in ("Did Binance steal the meeting funds?", "The meeting starts at nine?", "Does the meeting start at nine?"):
            with self.subTest(text=text):
                self.assertEqual(self.validate(reply(text)).action, "IGNORE")

    def test_question_suggesting_an_answer_is_not_fact_evidence(self):
        candidate = message("Does the meeting start at 8pm tonight?")
        self.assertEqual(self.validate(reply("It starts at 8pm tonight"), candidate=candidate, context=[]).action, "IGNORE")
        earlier_question = message("Does the meeting start at 8pm tonight?", message_id=19, sender_id=3, sent_at=NOW - 30)
        self.assertEqual(self.validate(reply("It starts at 8pm tonight"), candidate=candidate, context=[earlier_question]).action, "IGNORE")
        evidence = replace(earlier_question, text="Meeting starts at 8pm tonight")
        self.assertEqual(self.validate(reply("It starts at 8pm tonight"), candidate=candidate, context=[evidence]).action, "REPLY")

    def test_fact_grounding_does_not_reverse_simple_negation(self):
        evidence = message("The meeting does not start at eight tonight", message_id=19, sender_id=3, sent_at=NOW - 30)
        self.assertEqual(self.validate(reply("It starts at eight tonight"), context=[evidence]).action, "IGNORE")

    def test_questions_are_not_forced_repeatedly(self):
        self.assertEqual(self.validate(reply("Which venue does the notice mention?")).action, "REPLY")
        self.assertEqual(self.validate(reply("Which venue does the notice mention?"), recent=["Who sent the meeting notice?"]).action, "IGNORE")


if __name__ == "__main__":
    unittest.main()
