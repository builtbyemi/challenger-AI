from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    message_id: int
    chat_id: int
    sender_id: int | None
    text: str
    sent_at: float
    reply_to_id: int | None = None
    reply_to_sender_id: int | None = None
    mentions: tuple[str, ...] = ()
    mentioned_ids: tuple[int, ...] = ()
    is_bot: bool = False
    outgoing: bool = False
    forwarded: bool = False
    edited_at: float | None = None


@dataclass(frozen=True)
class Identity:
    user_id: int
    username: str = ""


@dataclass(frozen=True)
class Decision:
    action: str = "IGNORE"
    target_id: int | None = None
    text: str | None = None
    emoji: str | None = None
    reason: str = "No worthwhile reply"
