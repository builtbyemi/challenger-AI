"""Optional, context-backed topics. Silence alone is never a reason to post."""
import re

from .models import Decision
from .selection import _similar, _topic_words, _words, _PROMOTION, _PERSONAL


def choose_discussion(settings, messages, actions, recent, peer_ids, now):
    human = [m for m in messages if not m.outgoing and not m.is_bot and not m.forwarded
             and m.sender_id not in peer_ids and m.sender_id is not None
             and 0 <= now - m.sent_at <= 86400 and not _PROMOTION.search(m.text)]
    if not settings.discussion_topics or not human:
        return Decision(reason="No configured topic with recent human context")
    latest = max([m.sent_at for m in messages] + [a["activity_at"] for a in actions] + [0])
    if now - latest < settings.quiet_seconds:
        return Decision(reason="Group is not quiet")
    last_human = max(m.sent_at for m in human)
    if any(a["kind"] == "START_DISCUSSION" and a["activity_at"] >= last_human
           and a["status"] != "rejected" for a in actions):
        return Decision(reason="Previous starter still awaits human participation")
    topics = set().union(*(_topic_words(m.text) for m in human))
    for text in settings.discussion_topics:
        if (not 3 <= len(_words(text)) <= 5 or not text.endswith("?")
                or len(text) > 100 or _PROMOTION.search(text) or _PERSONAL.search(text)
                or not re.fullmatch(r"[\w ',?]+", text)
                or not re.match(r"^(what|which|how|why|wetin|shey)\b", text, re.I)
                or not topics & _topic_words(text)
                or any(_similar(text, previous) for previous in recent)):
            continue
        return Decision("START_DISCUSSION", None, text, None, "Configured topic relevant to recent human discussion")
    return Decision(reason="No fresh, relevant discussion topic")
