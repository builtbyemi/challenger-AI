"""One worker decides; event handlers only append to the durable queue."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import logging
import time
from typing import Protocol

from .config import Settings
from .groq_client import GroqBackoff, GroqConfigurationError
from .models import Identity, Message
from .selection import skip_reason, validate_decision
from .storage import Store

log = logging.getLogger(__name__)


class TelegramBackoff(Exception):
    def __init__(self, seconds: float):
        self.seconds = max(1.0, seconds)
        super().__init__("Telegram requested a wait")


@dataclass(frozen=True)
class Refresh:
    target: Message | None
    messages: list[Message]


class Gateway(Protocol):
    async def sync(self) -> list[Message]: ...
    async def refresh(self, target_id: int) -> Refresh: ...
    async def send(self, text: str, target_id: int) -> Message: ...


class Worker:
    def __init__(self, settings: Settings, store: Store, identity: Identity,
                 decider, gateway: Gateway, clock=time.time, coordinator=None):
        self.settings, self.store, self.identity = settings, store, identity
        self.decider, self.gateway, self.clock = decider, gateway, clock
        self.coordinator = coordinator
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.next_history_sync = 0.0
        self.next_status_log = 0.0

    def ingest(self, message: Message) -> None:
        if message.chat_id != self.settings.chat_id:
            return
        if self.coordinator is not None and message.sender_id in self.coordinator.peer_ids():
            self.coordinator.observe_outgoing(self.settings.account_id, message)
            if message.sender_id != self.identity.user_id:
                message = replace(message, is_bot=True)
        if message.outgoing or message.sender_id == self.identity.user_id:
            if message.text.strip():
                self.store.observe_outgoing(replace(message, outgoing=True))
        elif message.text.strip():
            if message.reply_to_id is not None and message.reply_to_sender_id is None:
                parent = self.store.get(message.reply_to_id)
                if parent is not None:
                    message = replace(message, reply_to_sender_id=parent.sender_id)
            changed = self.store.enqueue(message)
            writer = log.info if changed else log.debug
            writer("%s account=%s message=%s chat=%s", "QUEUED" if changed else "ALREADY STORED",
                   self.settings.account_id, message.message_id, message.chat_id)
        self.wake.set()

    def delay(self) -> float:
        now = self.clock()
        waits = [0, self.store.quota_wait(now), self.store.blocked_until("telegram") - now,
                 self.store.blocked_until("groq") - now]
        if self.coordinator is not None:
            waits.extend((self.coordinator.quota_wait(self.settings.chat_id, now),
                          self.coordinator.blocked_until("groq") - now))
        return max(waits)

    def recent_replies(self) -> list[str]:
        recent = self.store.recent_replies(self.clock())
        if self.coordinator is not None:
            shared = self.coordinator.recent_replies(self.settings.chat_id, self.clock())
            # The coordinator has the chronological history across accounts.
            # Prefix only local extras; placing own duplicates first would move
            # newer replies outside the validator's last-three style window.
            shared_texts = set(shared)
            recent = [text for text in recent if text not in shared_texts] + shared
        # Repeated manual text represents distinct, timestamped activity. Keep
        # its latest occurrence in the recent style window.
        return recent

    @asynccontextmanager
    async def group_lock(self):
        if self.coordinator is None:
            yield
        else:
            async with self.coordinator.group_lock(self.settings.chat_id):
                yield

    def _block_telegram(self, exc: TelegramBackoff) -> float:
        self.store.set_block("telegram", self.clock() + exc.seconds)
        log.warning("Telegram wait: %.0fs; queue retained", exc.seconds)
        return exc.seconds

    def log_quota(self, reason: str) -> None:
        remaining = self.store.quota_remaining(self.clock())
        group = (self.coordinator.quota_remaining(self.settings.chat_id, self.clock())
                 if self.coordinator is not None else remaining)
        log.info("QUOTA account=%s remaining=%s/%s group_remaining=%s wait=%.0fs reason=%s",
                 self.settings.account_id, remaining, self.settings.max_actions, group,
                 self.delay(), reason)

    async def process_once(self) -> float:
        """Process a bounded snapshot; return delay before next attempt."""
        async with self.lock:
            async with self.group_lock():
                return await self._process_once()

    async def _process_once(self) -> float:
        self.store.expire(self.clock())
        pause = self.delay()
        if self.clock() >= self.next_status_log:
            log.info("WORKER account=%s pending=%s wait=%.0fs mode=%s", self.settings.account_id,
                     len(self.store.pending(self.clock(), 1000)), pause,
                     "LIVE" if self.settings.auto_send else "PREVIEW")
            self.next_status_log = self.clock() + 60
            self.log_quota("hourly quota exhausted or provider backoff" if pause else "slots available; relevance required")
        if pause:
            return pause
        batch = self.store.pending(self.clock(), self.settings.batch_size)
        # Sync outgoing history BEFORE generation in both modes. If unavailable,
        # fail closed so a restart/manual send cannot bypass the local ledger.
        needs_parent = any(m.reply_to_id is not None and m.reply_to_sender_id is None for m in batch)
        if self.clock() >= self.next_history_sync or needs_parent:
            try:
                for message in await self.gateway.sync():
                    self.ingest(message)
                self.next_history_sync = self.clock() + 60
            except TelegramBackoff as exc:
                return self._block_telegram(exc)
            except Exception as exc:
                log.warning("Telegram history unavailable (%s); queue retained", type(exc).__name__)
                self.store.set_block("telegram", self.clock() + 60)
                return 60
        if self.delay():
            return self.delay()
        self.store.expire(self.clock())
        batch = self.store.pending(self.clock(), self.settings.batch_size)
        context = self.store.context(self.clock())
        candidates = []
        for message in batch:
            reason = skip_reason(message, context, self.identity, self.clock(), self.settings.max_age)
            if self.coordinator is not None:
                if message.sender_id in self.coordinator.peer_ids():
                    reason = "Configured automated account"
                elif self.coordinator.is_claimed(message.chat_id, message.message_id):
                    reason = "Another account already processed this target"
            if reason:
                self.store.mark([message], "reviewed", reason)
                log.info("FILTERED account=%s target=%s remaining=%s/%s reason=%s", self.settings.account_id,
                         message.message_id, self.store.quota_remaining(self.clock()), self.settings.max_actions, reason)
            else:
                candidates.append(message)
        if not candidates:
            return self.settings.batch_delay
        try:
            log.info("EVALUATING account=%s targets=%s", self.settings.account_id,
                     [m.message_id for m in candidates])
            raw = await self.decider.decide(candidates, context, self.recent_replies())
        except GroqBackoff as exc:
            self.store.set_block("groq", self.clock() + exc.seconds)
            log.warning("Groq wait: %.0fs; queue retained", exc.seconds)
            return exc.seconds
        except GroqConfigurationError:
            # Configuration failures require operator action, not endless API retries.
            log.error("Groq configuration rejected; check API key/model in .env")
            raise
        decision = validate_decision(raw, candidates, context, self.identity,
                                     self.recent_replies(),
                                     now=self.clock(), max_age=self.settings.max_age)
        if decision.action != "REPLY":
            self.store.mark(candidates, "reviewed", decision.reason)
            log.info("IGNORE account=%s batch=%s remaining=%s/%s reason=%s", self.settings.account_id,
                     len(candidates), self.store.quota_remaining(self.clock()), self.settings.max_actions, decision.reason)
            return self.settings.batch_delay
        target = next(m for m in candidates if m.message_id == decision.target_id)
        log.info("PROPOSED account=%s target=%s reason=%s; awaiting final checks",
                 self.settings.account_id, target.message_id, decision.reason)
        # Awaiting inference never locks ingestion. Re-fetch thread and target now,
        # then validate a second time against messages received during inference.
        try:
            refreshed = await self.gateway.refresh(target.message_id)
        except TelegramBackoff as exc:
            return self._block_telegram(exc)
        except Exception as exc:
            log.warning("Target refresh unavailable (%s); queue retained", type(exc).__name__)
            self.store.set_block("telegram", self.clock() + 60)
            return 60
        for message in refreshed.messages:
            self.ingest(message)
        if refreshed.target is None:
            self.store.mark([target], "reviewed", "Target deleted or unavailable")
            return self.settings.batch_delay
        if refreshed.target != target:
            self.ingest(refreshed.target)
            log.info("Target %s changed; decision discarded", target.message_id)
            return self.settings.batch_delay
        # Include all refreshed post-target messages, even if they exceed the
        # ordinary context window used to keep inference costs bounded.
        context_by_id = {m.message_id: m for m in self.store.context(self.clock())}
        for message in refreshed.messages:
            current = context_by_id.get(message.message_id)
            if current is None or (message.edited_at or 0) >= (current.edited_at or 0):
                context_by_id[message.message_id] = message
        latest_context = sorted(context_by_id.values(), key=lambda m: (m.sent_at, m.message_id))
        reason = skip_reason(target, latest_context, self.identity, self.clock(), self.settings.max_age)
        decision = validate_decision(raw, candidates, latest_context, self.identity,
                                     self.recent_replies(),
                                     now=self.clock(), max_age=self.settings.max_age)
        if reason or decision.action != "REPLY":
            self.store.mark([target], "reviewed", reason or decision.reason)
            log.info("IGNORE target=%s after refresh reason=%s", target.message_id, reason or decision.reason)
            return self.settings.batch_delay
        if self.delay():
            return self.delay()
        mode = "live" if self.settings.auto_send else "preview"
        reply = decision.text + (f" {decision.emoji}" if decision.emoji else "")
        claim_id = None
        if self.coordinator is not None:
            claim_id = self.coordinator.reserve(self.settings.account_id, target.chat_id,
                                                target.message_id, reply, mode, self.clock())
            if claim_id is None:
                rejected = ("target already claimed" if self.coordinator.is_claimed(target.chat_id, target.message_id)
                            else "group hourly quota exhausted" if self.coordinator.quota_wait(target.chat_id, self.clock())
                            else "provider backoff" if self.coordinator.blocked_until("groq") > self.clock()
                            else "repetitive reply rejected by group history")
                if rejected == "repetitive reply rejected by group history":
                    self.store.mark([target], "reviewed", rejected)
                self.log_quota(rejected)
                return max(self.settings.batch_delay, self.delay())
        action_id = self.store.reserve(target, reply, mode, self.clock())
        if action_id is None:
            if claim_id is not None:
                self.coordinator.finish(claim_id, "rejected", self.clock())
            log.info("Target/quota changed; decision discarded")
            return max(self.settings.batch_delay, self.delay())
        self.store.mark([m for m in candidates if m.message_id != target.message_id],
                        "reviewed", "Batch already selected another target")
        if not self.settings.auto_send:
            self.store.finish(action_id, "previewed", self.clock())
            if claim_id is not None:
                self.coordinator.finish(claim_id, "previewed", self.clock())
            log.info("SIMULATED REPLY account=%s target=%s text=%s; preview quota reserved, nothing sent",
                     self.settings.account_id, target.message_id, reply)
            self.log_quota("accepted simulated reply")
            return self.delay()
        try:
            sent = await self.gateway.send(reply, target.message_id)
        except TelegramBackoff as exc:
            self.store.finish(action_id, "rejected", self.clock())
            if claim_id is not None:
                self.coordinator.finish(claim_id, "rejected", self.clock())
            self.log_quota("Telegram rejected send; reservation released, FloodWait still enforced")
            return self._block_telegram(exc)
        except Exception as exc:
            # A timeout can mean Telegram accepted the message. Never resend it.
            self.store.finish(action_id, "uncertain", self.clock())
            if claim_id is not None:
                self.coordinator.finish(claim_id, "uncertain", self.clock())
            self.store.set_block("telegram", self.clock() + 60)
            log.error("Send outcome uncertain (%s); slot retained, no resend", type(exc).__name__)
            return max(60, self.delay())
        self.store.finish(action_id, "sent", max(self.clock(), sent.sent_at), sent.message_id)
        if claim_id is not None:
            self.coordinator.finish(claim_id, "sent", max(self.clock(), sent.sent_at), sent.message_id)
        log.info("SENT REPLY target=%s telegram_id=%s", target.message_id, sent.message_id)
        self.log_quota("confirmed sent reply")
        return self.delay()

    async def run(self) -> None:
        # Batch nearby arrivals; this debounce is not an inter-message cooldown.
        await asyncio.sleep(self.settings.batch_delay)
        while True:
            self.wake.clear()
            try:
                pause = await self.process_once()
            except GroqConfigurationError:
                raise
            except Exception:
                log.exception("Worker error; queue preserved")
                pause = 60
            if self.delay() > 0:
                await asyncio.sleep(min(60, max(pause, self.settings.batch_delay)))
            elif self.store.pending(self.clock(), 1):
                await asyncio.sleep(max(self.settings.batch_delay, pause))
            else:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                await asyncio.sleep(self.settings.batch_delay)
