"""Optional, context-backed topics. Silence alone is never a reason to post."""
import re

from .models import Decision
from .selection import _similar, _topic_words, _words, _PROMOTION, _PERSONAL, _FORMAL, _GENERIC

COMMUNITY = re.compile(r"\b(?:crypto|bitcoin|btc|meme|memecoins?|coins?|tokens?|ww3|markets?|trends?|trump|politics|elections?|economy|world|news|wallets?|blockchain)\b", re.I)


def discussion_context(settings, messages, actions, peer_ids, now):
    """Eligible human context; quietness and allowance never create relevance."""
    human = [m for m in messages if not m.outgoing and not m.is_bot and not m.forwarded
             and m.sender_id is not None and m.sender_id not in peer_ids
             and not m.mentions and not m.mentioned_ids and m.reply_to_id is None
             and 0 <= now-m.sent_at <= 86400 and not _PROMOTION.search(m.text)]
    if not human or not any(COMMUNITY.search(m.text) for m in human):
        return []
    latest = max([m.sent_at for m in messages]+[a['activity_at'] for a in actions]+[0])
    if now-latest < settings.quiet_seconds:
        return []
    last_human = max(m.sent_at for m in human)
    if any(a['kind']=='START_DISCUSSION' and a['status']!='rejected' and a['activity_at']>=last_human for a in actions):
        return []
    return sorted(human,key=lambda m:(m.sent_at,m.message_id))[-20:]


def validate_starter(raw, context, recent):
    if (not isinstance(raw,dict) or set(raw)!={'action','target_id','text','emoji','reason'}
            or raw.get('action')!='START_DISCUSSION' or raw.get('target_id') is not None
            or raw.get('emoji') is not None or not isinstance(raw.get('reason'),str)):
        return Decision(reason='No valid discussion starter')
    text=raw.get('text')
    if (not isinstance(text,str) or not 4<=len(_words(text))<=10 or len(text)>120
            or not re.fullmatch(r"[\w ',?-]+\?",text)
            or not re.match(r'^(what|which|how|why|wetin|shey)\b',text,re.I)
            or _PROMOTION.search(text) or _PERSONAL.search(text) or _FORMAL.search(text) or _GENERIC.search(text)
            or re.search(r"\b(?:i|my|we|our|did|breaking|announced|confirmed|surged|crashed|invaded|guaranteed|launched)\b",text,re.I)):
        return Decision(reason='Unsuitable discussion question')
    source=' '.join(m.text for m in context)
    if (not COMMUNITY.search(text) or not _topic_words(text)&_topic_words(source)
            or set(re.findall(r'\d+',text))-set(re.findall(r'\d+',source))
            or any(_similar(text,old) for old in [*recent,*(m.text for m in context)])):
        return Decision(reason='Unsupported or repeated discussion')
    # Do not introduce named entities absent from the supplied conversation.
    names=re.findall(r'\b[A-Z][\w-]*\b',text)[1:]
    if any(name.lower() not in source.lower() for name in names):
        return Decision(reason='Unsupported named topic')
    return Decision('START_DISCUSSION',None,text,None,'Fresh question grounded in recent human context')


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
