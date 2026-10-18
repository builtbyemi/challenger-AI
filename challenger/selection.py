"""Conservative, deterministic gates around the model's optional suggestion.

Passing these gates permits consideration; it does not require a reply. Chat
text is never executed, and an unrecognised or incomplete decision is ignored.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import re
import time

from .models import Decision, Identity, Message


ALLOWED_EMOJI = frozenset({"😂", "😅", "🙂", "🤔", "😭", "👀", "👍"})
_WORDS = re.compile(r"[\w]+(?:['’][\w]+)*", re.UNICODE)
_BOT = re.compile(r"\b(?:bots?|robots?|spam(?:ming|mer)?|automated|automation)\b|random words", re.I)
_PIDGIN = re.compile(r"\b(?:omo|wetin|dey|abeg|wahala|una|shey|sebi|abi|sabi|na|sha|no be|you be)\b", re.I)
_QUESTION_START = re.compile(
    r"^(?:what(?:'s| is)?|when|where|which|who|why|how|can|could|would|should|"
    r"does|did|do|is|are|was|were|will|has|have|anyone|anybody|wetin|shey|sebi|abi|"
    r"una\s+(?:fit|know)|person\s+sabi)\b", re.I,
)
_GROUP_ADDRESS = re.compile(r"^(?:(?:hi|hey)\s+)?(?:everyone|anyone|anybody|folks|guys|group|people|una|abeg)\b", re.I)
_ACK = re.compile(
    r"^(?:ok(?:ay)?|alright|sure(?: thing)?|yes|no|yeah|yep|nah|lol|lmao|haha+|"
    r"thanks?(?: you)?|thank you|noted|nice|exactly|true|agreed|same|indeed|"
    r"well said|fair enough|got it|i see|hmm+|ehen|ehn|na so|no wahala)[\s.!?,😂😅👍]*$", re.I,
)
_GENERIC = re.compile(
    r"\b(?:thanks? for sharing|glad to hear|sounds? (?:like|interesting|good|great)|"
    r"enjoy your|citizens truly bear|just sharing thoughts|plans are ambitious|"
    r"keep (?:pushing|going|it up)|stay (?:strong|positive)|you(?:'ve| have) got this|"
    r"(?:that(?:'s| is)|this is) (?:really |very |so )?(?:great|interesting|amazing|awesome)|"
    r"i (?:totally |completely )?agree|well said|good (?:point|vibes)|"
    r"time will tell|let(?:'s| us) see|we (?:move|shall see)|"
    r"thank you for|as an ai|happy to help|hope (?:this|that) helps)\b", re.I,
)
_FORMAL = re.compile(
    r"\b(?:furthermore|moreover|nevertheless|indeed|commendable|endeavour|endeavor|"
    r"utilize|utilise|therefore|consequently|undoubtedly|certainly|"
    r"perspective|fascinating|resilience|perseverance)\b", re.I,
)
_PERSONAL = re.compile(
    r"\b(?:i(?:'m| am)\s+(?:a |an )?(?:human|person|real|called|named|from|based|living)|"
    r"my (?:name|wife|husband|kids|family|friend|experience|car|house|job|boss)|"
    r"(?:i|we)\s+(?:personally |actually |also )?(?:bought|purchased|visited|went|"
    r"experienced|remember|grew|live|lived|work|worked|met|own|earned|lost|tried)|"
    r"i(?:'ve| have)\s+(?:been|used|seen|tried|bought|worked)|"
    r"we(?:'re| are)\s+(?:all |mostly |mainly )?(?:from|in|based|living)|"
    r"not (?:a |an )?(?:bot|robot|automated)|real (?:human|person))\b", re.I,
)
_PROMOTION = re.compile(
    r"https?://|www\.|t\.me/|\b(?:airdrop|giveaway|buy now|join (?:my|our)|"
    r"guaranteed (?:profit|returns)|dm me|message me|send (?:money|funds)|"
    r"pump (?:this|the)|presale|referral code)\b", re.I,
)
_VAGUE_QUESTION = re.compile(
    r"^(?:what(?:'s| is) (?:this|that|it)|what (?:happened|do you think)|"
    r"why (?:is|are|did|do) (?:it|this|that|they|he|she)|"
    r"(?:how|what) about (?:it|that|this)|(?:where|who) (?:is|are) (?:it|that|they|he|she)|"
    r"wetin (?:be (?:this|that)|happen(?:ed)?(?: for (?:there|here))?)|"
    r"(?:can|could) (?:you|anyone) (?:help|explain)|any (?:thoughts|updates|news))\s*[?.]*$", re.I,
)
_FUNCTION_WORDS = frozenset((
    "a an the this that these those it its is are was were be been being am do does did done have has had "
    "will would shall should can could may might must of to for from at on in into by with as and or but if "
    "so than then because about through over under just also only still already again now here there yes no "
    "not don't doesn't didn't isn't aren't wasn't weren't won't can't couldn't wouldn't shouldn't it's that's "
    "there's they're we've you've you're i'm he's she's i've i'd i'll we'd we'll their them they he she his her "
    "you your i me my we our us who whose whom what which when where why how all some any each every other same "
    "both either neither much many more most less least very really quite too enough such up down out off apart along around"
).split())
_TOPIC_STOPWORDS = _FUNCTION_WORDS | frozenset("time start starts happen happened happening know think say says said tell explain anyone anybody everyone somebody group guys folks people meetingtime today tonight tomorrow yesterday please abeg omo wetin dey na sha sabi una be fit make need help notice question answer".split())
_PIDGIN_FUNCTION_WORDS = frozenset("na dey be no wetin abeg omo sha sabi una dem am e fit make".split())
_CLARIFICATION_WORDS = frozenset("mean meaning refer referring mention mentioned talking asking clarify explain detail details notice share source".split())
_NEGATION = re.compile(r"\b(?:no|not|never|don't|doesn't|didn't|isn't|aren't|wasn't|weren't|won't|can't|couldn't|wouldn't|shouldn't)\b", re.I)


def _stem(word: str) -> str:
    # Minimal normalisation, deliberately avoiding broad fuzzy fact matching.
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word


def _content_words(text: str) -> set[str]:
    return {_stem(word) for word in _words(text) if word not in _FUNCTION_WORDS}


def _topic_words(text: str) -> set[str]:
    return {_stem(word) for word in _words(text) if len(word) >= 4 and word not in _TOPIC_STOPWORDS}


def _relevant_context(target: Message, context: list[Message]) -> list[Message]:
    topics = _topic_words(target.text)
    return [item for item in context if item.chat_id == target.chat_id
            and 0 <= target.sent_at - item.sent_at <= 720
            and (item.message_id == target.reply_to_id or bool(topics & _topic_words(item.text)))]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'")).strip()


def _words(text: str) -> list[str]:
    return _WORDS.findall(text.lower())


def _mentions(message: Message) -> set[str]:
    return {name.lower().lstrip("@") for name in message.mentions} | {
        name.lower() for name in re.findall(r"(?<!\w)@([A-Za-z0-9_]+)", message.text)
    }


def directly_addressed(message: Message, identity: Identity) -> bool:
    """Use Telegram reply/mention evidence, never just the word 'you'."""
    return (
        message.reply_to_sender_id == identity.user_id
        or identity.user_id in message.mentioned_ids
        or bool(identity.username and identity.username.lower().lstrip("@") in _mentions(message))
    )


def _question_text(message: Message) -> str:
    text = re.sub(r"(?<!\w)@[A-Za-z0-9_]+", "", _clean(message.text)).strip(" ,:")
    text = re.sub(r"^(?:(?:hi|hey)\s+)?(?:guys|folks|everyone|group|abeg|omo)[,:]?\s+", "", text, flags=re.I)
    return text.strip()


def is_question(text: str) -> bool:
    """Require a question form; punctuation alone is not an invitation."""
    return bool(_QUESTION_START.search(text))


def is_direct_bot_question(message: Message, identity: Identity) -> bool:
    if not directly_addressed(message, identity):
        return False
    text = _question_text(message)
    return bool(re.search(
        r"\b(?:are you (?:really |actually )?(?:a |an )?(?:bot|robot|automated|human|real person)|"
        r"is (?:this|your) account (?:a bot|automated|human|run by a human)|"
        r"has (?:this|your) account been automated|"
        r"(?:do|are) you (?:use|using) (?:a bot|automation)|"
        r"(?:you (?:be|na)|na you be) (?:a )?(?:bot|robot)|"
        r"na (?:bot|robot) you be|(?:shey|abi) (?:you (?:be|na)|na you be) (?:bot|robot))\b",
        text, re.I,
    )) and (is_question(text) or "?" in text)


def bot_reply(message: Message) -> str:
    """A disclosure, with no defensive denial or invented human backstory."""
    if _PIDGIN.search(message.text):
        return "Na automated account be this 😂"
    return "This account is automated 😅"


def _substantive_answer(message: Message) -> bool:
    text = _clean(message.text)
    return len(_words(text)) >= 2 and not _ACK.fullmatch(text) and not is_question(_question_text(message))


def is_welcome_notice(message: Message) -> bool:
    """Only plain arrival announcements, including those posted by welcome bots."""
    text = _clean(message.text)
    return (len(text) <= 250 and not message.forwarded and not _PROMOTION.search(text)
            and bool(re.fullmatch(
                r"welcome\b[^.!?\n]{0,180}\bto (?:the |this |our )?(?:group|community)[.! ]*",
                text, re.I)))


def welcome_history_reason(message: Message, recent_replies: list[str]) -> str | None:
    if is_welcome_notice(message) and any(re.search(r"\bwelcome\b", text, re.I)
                                          for text in recent_replies[-5:]):
        return "Welcome already sent among the last five replies"
    return None


def _shared_discussion(message: Message, context: list[Message], identity: Identity) -> bool:
    """Three people discussing the topic is evidence of a public group thread."""
    topics = _topic_words(message.text)
    participants = {message.sender_id}
    for item in context:
        if (item.chat_id == message.chat_id and abs(item.sent_at - message.sent_at) <= 120
                and item.sender_id not in (None, identity.user_id)
                and not (item.is_bot or item.outgoing or item.forwarded)
                and not (_mentions(item) or item.mentioned_ids or _PROMOTION.search(item.text))
                and not re.search(r"\b(?:you|your)\b", item.text, re.I)
                and len(_words(item.text)) >= 4 and topics & _topic_words(item.text)):
            participants.add(item.sender_id)
    return len(participants - {None}) >= 3


def _discussion_reason(message: Message, context: list[Message], identity: Identity, now: float) -> str | None:
    """Permit substantive statements in an active, open discussion."""
    topics = _topic_words(message.text)
    if now - message.sent_at > 120:
        return "Discussion statement is no longer timely"
    if len(_words(message.text)) < 5 or len(topics) < 2:
        return "Statement lacks a specific discussion topic"
    related = []
    for item in context:
        if (item.chat_id != message.chat_id or item.message_id == message.message_id
                or abs(item.sent_at - message.sent_at) > 120
                or item.sender_id in (None, identity.user_id, message.sender_id)
                or item.is_bot or item.outgoing or item.forwarded
                or (item.reply_to_id is not None and not _shared_discussion(message, context, identity))
                or _mentions(item) or item.mentioned_ids
                or re.search(r"\b(?:you|your)\b", item.text, re.I)
                or len(_words(item.text)) < 4
                or _ACK.fullmatch(_clean(item.text)) or _GENERIC.search(item.text)
                or _PROMOTION.search(item.text) or _BOT.search(item.text)):
            continue
        if topics & _topic_words(item.text):
            if _similar(message.text, item.text):
                return "Repeated observation in the discussion"
            related.append(item)
    return None if related else "No active open discussion supporting this statement"


def skip_reason(
    message: Message,
    context: list[Message],
    identity: Identity,
    now: float,
    max_age: float = 720,
) -> str | None:
    """Return a reason to IGNORE, or None to permit model consideration."""
    text = _clean(message.text)
    if re.search(r"\b(?:leaderboard|score\s*farm(?:ing)?|farm(?:ing)?\s*points|"
                 r"boost(?:ing)?\s*(?:our|my|the)?\s*(?:score|rank)|engagement\s*points)\b", text, re.I):
        return "Score-farming or leaderboard participation is not a conversation target"
    if message.outgoing or message.sender_id == identity.user_id:
        return "Own message"
    welcome = is_welcome_notice(message)
    if message.forwarded or (message.is_bot and not welcome):
        return "Bot or forwarded message"
    if message.sent_at > now + 30 or now - message.sent_at >= max_age:
        return "Expired or invalid message timestamp"
    if not text or len(text) > 1500:
        return "Empty or excessive message"
    if text.endswith(("...", "…", "-", ":")) or len(_words(text)) < 3:
        return "Incomplete or fragmentary message"
    if _ACK.fullmatch(text) or _PROMOTION.search(text):
        return "Acknowledgment or promotion"
    if welcome:
        if now - message.sent_at > 120:
            return "Welcome notice is no longer timely"
        if any(item.chat_id == message.chat_id and item.message_id != message.message_id
               and item.sent_at >= message.sent_at and not item.is_bot
               and re.search(r"\bwelcome\b", item.text, re.I) for item in context):
            return "Another member already welcomed the arrival"
        return None
    own_username = identity.username.lower().lstrip("@")
    if _mentions(message) - ({own_username} if own_username else set()):
        return "Message mentions another member"
    if set(message.mentioned_ids) - {identity.user_id}:
        return "Message addresses another member"
    direct = directly_addressed(message, identity)
    shared = _shared_discussion(message, context, identity)
    if message.reply_to_id is not None and message.reply_to_sender_id != identity.user_id and not shared:
        return "Reply in another member's conversation"
    bot_question = is_direct_bot_question(message, identity)
    if _BOT.search(text) or bot_question:
        if not is_direct_bot_question(message, identity):
            return "Bot/spam discussion without a direct automation question"
    if _GENERIC.search(text):
        return "Generic reaction"
    question = _question_text(message)
    question_target = is_question(question) or bot_question
    if _VAGUE_QUESTION.fullmatch(question):
        return "Vague question without a self-contained topic"
    if re.search(r"\b(?:your (?:name|age|wife|husband|kids|job|country)|where (?:are you from|do you live)|"
                 r"have you ever|did you (?:buy|visit|go)|what do you (?:own|do for))\b", question, re.I):
        return "Question requires personal identity or experience"
    if not direct:
        if len(_words(question)) < 5:
            return "Open question is too short to be self-contained"
        if re.search(r"\b(?:you|your|he|she|him|her)\b", question, re.I):
            return "Question appears directed at another person"
        # A name followed by punctuation is an address, unless it names the group.
        if re.match(r"^[A-Z][A-Za-z_]{1,24}[,:]\s", text) and not _GROUP_ADDRESS.search(text):
            return "Named address to another person"
    if not question_target:
        reason = _discussion_reason(message, context, identity, now)
        if reason:
            return reason
    same_chat = [item for item in context if item.chat_id == message.chat_id and item.message_id != message.message_id]
    for item in same_chat:
        if not question_target:
            break  # A discussion continuing is not an already-answered question.
        if item.sent_at < message.sent_at or (item.sent_at == message.sent_at and item.message_id < message.message_id):
            continue
        if item.reply_to_id == message.message_id and _substantive_answer(item):
            return "Question already received an answer"
        if item.sender_id == message.sender_id and re.search(
            r"\b(?:found (?:it|the answer)|figured it out|never ?mind|sorted(?: it)?|"
            r"got (?:it|the answer)|answer is|it(?:'s| is) (?:at|on)|starts? at)\b", item.text, re.I,
        ):
            return "Author already resolved the question"
        if (item.sender_id != message.sender_id and item.reply_to_id is None
                and 0 <= item.sent_at - message.sent_at <= 90 and _substantive_answer(item)):
            overlapping = _topic_words(message.text) & _topic_words(item.text)
            # People commonly answer without Telegram's reply button. Treat a
            # nearby declarative answer about the same topic as sufficient.
            if overlapping or (re.search(r"^(?:it(?:'s| is)|they(?:'re| are)|the answer is|at\s+\d)", item.text, re.I)
                               and len(same_chat) <= 12):
                return "Nearby topic answer already exists"
    if not direct and not shared and not _GROUP_ADDRESS.search(text):
        nearby = [item for item in same_chat if 0 <= message.sent_at - item.sent_at <= 120]
        interlocutors = {
            item.reply_to_sender_id for item in nearby
            if item.sender_id == message.sender_id and item.reply_to_sender_id not in (None, identity.user_id, message.sender_id)
        }
        if any(item.sender_id in interlocutors and item.reply_to_sender_id == message.sender_id for item in nearby):
            return "Nearby back-and-forth between other members"
    return None


def _similar(left: str, right: str) -> bool:
    a, b = " ".join(_words(left)), " ".join(_words(right))
    if not a or not b:
        return False
    left_words, right_words = set(a.split()), set(b.split())
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.82 or (
        len(left_words & right_words) / len(left_words | right_words) >= 0.75
    )


def _ignore(reason: str) -> Decision:
    return Decision(reason=reason)


def validate_decision(
    raw: dict,
    candidates: list[Message],
    context: list[Message],
    identity: Identity,
    recent_replies: list[str],
    *,
    now: float | None = None,
    max_age: float = 720,
    max_words: int = 10,
) -> Decision:
    """Fail closed on malformed, mistargeted, repetitive or unsuitable output."""
    if not isinstance(raw, dict) or set(raw) != {"action", "target_id", "text", "emoji", "reason"}:
        return _ignore("Malformed decision schema")
    action, target_id, text, emoji, reason = (raw[k] for k in ("action", "target_id", "text", "emoji", "reason"))
    if type(action) is not str or action not in {"REPLY", "IGNORE"}:
        return _ignore("Invalid action")
    if type(reason) is not str or not reason.strip() or len(reason) > 500:
        return _ignore("Invalid reason")
    if action == "IGNORE":
        if any(value is not None for value in (target_id, text, emoji)):
            return _ignore("IGNORE must not include reply content")
        return _ignore(_clean(reason)[:200])
    if type(target_id) is not int or target_id <= 0:
        return _ignore("Invalid target message ID")
    target = next((item for item in candidates if item.message_id == target_id), None)
    if target is None:
        return _ignore("Target is not an eligible candidate")
    rejected = skip_reason(target, context, identity, time.time() if now is None else now, max_age)
    if rejected:
        return _ignore(rejected)
    rejected = welcome_history_reason(target, recent_replies)
    if rejected:
        return _ignore(rejected)
    if type(text) is not str or not text.strip() or len(text) > 180:
        return _ignore("Invalid reply text")
    if emoji is not None and (type(emoji) is not str or emoji not in ALLOWED_EMOJI):
        return _ignore("Invalid emoji")
    text = _clean(text)
    # Permit punctuation cleanup, never destroy apostrophes or commas.
    text = text.replace("!", "").rstrip(". ")
    if is_direct_bot_question(target, identity):
        # This exception is deliberately fixed, truthful and language matched.
        text = bot_reply(target)
        emoji = None
    if not 3 <= len(_words(text)) <= max_words:
        return _ignore(f"Reply must contain 3–{max_words} words")
    embedded = [character for character in text if character in ALLOWED_EMOJI]
    if len(embedded) + (1 if emoji else 0) > 1:
        return _ignore("Too many emojis")
    # Disallow hidden controls, links, handles, formatting and unknown symbols.
    plain = text
    for item in ALLOWED_EMOJI:
        plain = plain.replace(item, "")
    if any(not (character.isalnum() or character in " ',?.-£$€₦") for character in plain):
        return _ignore("Unsupported symbol or formatting")
    if _GENERIC.search(text) or _FORMAL.search(text) or _ACK.fullmatch(text):
        return _ignore("Generic filler or formal wording")
    if _PERSONAL.search(text):
        return _ignore("Unsupported personal experience or identity")
    if _PROMOTION.search(text) or "@" in text:
        return _ignore("Reply contains promotion or member impersonation")
    if _PIDGIN.search(text) and not _PIDGIN.search(target.text):
        return _ignore("Forced Pidgin on an English message")
    if _PIDGIN.search(target.text) and not _PIDGIN.search(text):
        return _ignore("Reply does not match the selected message's Pidgin")
    welcome = is_welcome_notice(target)
    if welcome and not re.fullmatch(r"welcome to (?:this|the) (?:community|group)", text, re.I):
        return _ignore("Welcome reply must be a short arrival greeting")
    if any(_similar(text, previous) for previous in (recent_replies[-5:] if welcome else recent_replies)):
        return _ignore("Repeated or near-duplicate reply")
    if not welcome and _similar(text, target.text):
        return _ignore("Reply merely repeats the selected message")
    asking = is_question(text) or bool(re.match(r"^you mean\b", text, re.I))
    if "?" in text and not asking:
        return _ignore("Question punctuation does not establish a genuine question")
    if asking and (text.count("?") > 1 or any("?" in previous or is_question(previous) for previous in recent_replies[-3:])):
        return _ignore("Too many follow-up questions")
    relevant = _relevant_context(target, context)
    source = " ".join(item.text for item in [target, *relevant]).lower()
    source_numbers = set(re.findall(r"\d+(?:[.,]\d+)*", source))
    if set(re.findall(r"\d+(?:[.,]\d+)*", text)) - source_numbers:
        return _ignore("Reply introduces an unsupported number")
    if not welcome and not is_direct_bot_question(target, identity):
        if re.search(r"\b(?:i|i'm|i've|my|we|we're|we've|our|me)\b", text, re.I):
            return _ignore("Reply introduces an unsupported personal claim")
        supported = _content_words(source)
        content = _content_words(text) - _PIDGIN_FUNCTION_WORDS
        if not asking:
            # A question's suggested answer is not factual evidence. Require a
            # preceding declarative statement with the same substantive words
            # and polarity; this also prevents simple negation reversals.
            evidence = [*relevant]
            if not is_question(_question_text(target)):
                evidence.append(target)
            statements = [part.strip() for item in evidence if _substantive_answer(item)
                          for part in re.split(r"[.!?;]+", _clean(item.text)) if part.strip()]
            grounded = any(
                content <= _content_words(statement)
                and bool(_NEGATION.search(text)) == bool(_NEGATION.search(statement))
                for statement in statements
            )
            if not grounded:
                return _ignore("Declarative reply lacks supporting statement evidence")
        if asking:
            if content - supported - _CLARIFICATION_WORDS:
                return _ignore("Follow-up question introduces unsupported content")
            if not (_topic_words(text) & _topic_words(source)):
                return _ignore("Follow-up question is not grounded in the selected topic")
    return Decision("REPLY", target_id, text, emoji, _clean(reason)[:200])
