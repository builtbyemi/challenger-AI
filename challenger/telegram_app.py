"""Telethon adapter; no network access or credential reads at import time."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import time

from telethon import TelegramClient, events, types, utils
from telethon.errors import FloodWaitError

from .config import Settings
from .engine import Refresh, TelegramBackoff, Worker
from .groq_client import GroqDecider, PersonaDecider
from .models import Identity, Message
from .storage import Store
from .windows import day_start

log = logging.getLogger(__name__)


def timestamp(value: datetime | None) -> float:
    if value is None:
        return 0.0
    return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()


def convert_message(message, chat_id: int, parents: dict | None = None) -> Message:
    parent_id = getattr(message, "reply_to_msg_id", None)
    parent = (parents or {}).get(parent_id)
    text = getattr(message, "raw_text", "") or ""
    if not text.strip() and getattr(message, "out", False):
        text = "[Non-text message]"
    mentioned_ids = tuple(entity.user_id for entity in (getattr(message, "entities", None) or [])
                          if isinstance(entity, types.MessageEntityMentionName))
    return Message(
        message_id=message.id, chat_id=chat_id, sender_id=getattr(message, "sender_id", None),
        text=text, sent_at=timestamp(getattr(message, "date", None)),
        reply_to_id=parent_id, reply_to_sender_id=getattr(parent, "sender_id", None),
        mentions=tuple(re.findall(r"(?<!\w)@([A-Za-z0-9_]{3,32})", text)),
        mentioned_ids=mentioned_ids,
        is_bot=bool(getattr(getattr(message, "sender", None), "bot", False) or getattr(message, "via_bot_id", None)),
        outgoing=bool(getattr(message, "out", False)), forwarded=bool(getattr(message, "fwd_from", None)),
        edited_at=timestamp(message.edit_date) if getattr(message, "edit_date", None) else None,
    )


class TelethonGateway:
    def __init__(self, client, settings: Settings, identity: Identity):
        self.client, self.settings, self.identity = client, settings, identity

    async def normalize(self, messages) -> list[Message]:
        messages = [m for m in messages if m is not None and getattr(m, "message", None) is not None]
        parents = {m.id: m for m in messages}
        missing = {m.reply_to_msg_id for m in messages if m.reply_to_msg_id and m.reply_to_msg_id not in parents}
        if missing:
            fetched = await self.client.get_messages(self.settings.chat_id, ids=list(missing))
            parents.update({m.id: m for m in fetched if m is not None})
        return [convert_message(m, self.settings.chat_id, parents) for m in messages]

    async def outgoing(self):
        result = []
        scanned = 0
        # Reconcile today's manual activity as well as the rolling-hour tail
        # across midnight, so daily totals include offline-device sends.
        since = min(time.time() - 3600, day_start(time.time(), self.settings.timezone))
        async for message in self.client.iter_messages(self.settings.chat_id, from_user=self.identity.user_id, limit=300):
            scanned += 1
            if timestamp(message.date) < since:
                break
            result.append(message)
        if scanned >= 300:
            raise RuntimeError("Outgoing history could not be fully verified")
        return result

    async def _sync(self) -> list[Message]:
        recent = await self.client.get_messages(self.settings.chat_id, limit=100)
        outgoing = await self.outgoing()
        unique = {m.id: m for m in list(recent) + outgoing}
        return await self.normalize(list(unique.values()))

    async def sync(self) -> list[Message]:
        try:
            return await asyncio.wait_for(self._sync(), timeout=45)
        except FloodWaitError as exc:
            raise TelegramBackoff(exc.seconds) from None

    async def _refresh(self, target_id: int) -> Refresh:
        target = await self.client.get_messages(self.settings.chat_id, ids=target_id)
        if target is None or not getattr(target, "raw_text", ""):
            return Refresh(None, [])
        # Fetch ALL newer messages up to a safe bound. Overflow fails closed so an
        # answer in a busy thread cannot hide outside the ordinary context window.
        following = [m async for m in self.client.iter_messages(self.settings.chat_id, min_id=target_id, limit=201)]
        if len(following) > 200:
            raise TelegramBackoff(60)
        own = await self.outgoing()
        unique = {m.id: m for m in [target] + following + own}
        normalized = await self.normalize(list(unique.values()))
        return Refresh(next(m for m in normalized if m.message_id == target_id), normalized)

    async def refresh(self, target_id: int) -> Refresh:
        try:
            return await asyncio.wait_for(self._refresh(target_id), timeout=45)
        except FloodWaitError as exc:
            raise TelegramBackoff(exc.seconds) from None

    async def send(self, text: str, target_id: int | None) -> Message:
        try:
            sent = await asyncio.wait_for(self.client.send_message(
                self.settings.chat_id, text, reply_to=target_id,
                link_preview=False, parse_mode=None), timeout=45)
            return convert_message(sent, self.settings.chat_id)
        except FloodWaitError as exc:
            raise TelegramBackoff(exc.seconds) from None


@contextmanager
def instance_lock(path: Path):
    """OS lock releases on crashes; prevents concurrent use of one session."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("This Telegram session is already running") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


async def run(settings: Settings, *, shared_decider=None, coordinator=None,
              require_authorized: bool = False) -> None:
    from .auth import session_file
    with instance_lock(session_file(settings.session_path).with_suffix(".challenger.lock")):
        store = Store(settings.database_path, settings.account_id, settings.chat_id,
                      max_actions=settings.max_actions, max_age=settings.max_age,
                      window_mode=settings.window_mode, timezone=settings.timezone)
        client = decider = worker = None
        worker_task = disconnect_task = health_task = None
        try:
            client = TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash,
                                    request_retries=0, flood_sleep_threshold=0,
                                    raise_last_call_error=True, catch_up=True)
            decider = shared_decider or GroqDecider(settings.groq_api_key, settings.groq_model)
            if coordinator is not None:
                coordinator.import_activity(settings.account_id, settings.chat_id, store.export_actions(time.time()))

            async def on_message(event):
                actual_chat = getattr(event, "chat_id", settings.chat_id)
                log.info("RECEIVED account=%s message=%s chat=%s group=%s",
                         settings.account_id, event.message.id, actual_chat, event.is_group)
                if actual_chat != settings.chat_id or not event.is_group:
                    log.info("FILTERED event account=%s: group mismatch", settings.account_id)
                    return
                if event.is_group:
                    # Register before connection: catch-up can dispatch during
                    # start/get_me, before our own identity or worker is ready.
                    # All paths persist immediately without network or inference.
                    message = convert_message(event.message, settings.chat_id)
                    if coordinator is not None:
                        coordinator.observe_outgoing(settings.account_id, message)
                    if worker is not None:
                        worker.ingest(message)
                    elif message.outgoing:
                        store.observe_outgoing(message)
                    elif message.text.strip():
                        store.enqueue(message)
                        log.info("QUEUED startup account=%s message=%s", settings.account_id, message.message_id)
                    else:
                        log.info("FILTERED empty text account=%s message=%s", settings.account_id, message.message_id)

            async def on_deleted(event):
                for message_id in event.deleted_ids:
                    message = store.get(message_id)
                    if message is not None:
                        store.mark([message], "deleted", "Telegram deletion event")

            client.add_event_handler(on_message, events.NewMessage(chats=settings.chat_id))
            client.add_event_handler(on_message, events.MessageEdited(chats=settings.chat_id))
            client.add_event_handler(on_deleted, events.MessageDeleted(chats=settings.chat_id))
            wait = store.blocked_until("telegram") - time.time()
            if wait > 0:
                log.warning("Persisted Telegram wait: %.0fs", wait)
                while time.time() < store.blocked_until("telegram"):
                    await asyncio.sleep(max(0, min(60, store.blocked_until("telegram") - time.time())))
            if require_authorized:
                await client.connect()
                if not await client.is_user_authorized():
                    from .auth import AuthenticationRequired
                    raise AuthenticationRequired(settings.account_id)
            else:
                await client.start()
            me = await client.get_me()
            identity = Identity(me.id, me.username or "")
            if coordinator is not None:
                coordinator.register(settings.account_id, identity.user_id)
            chat = await client.get_entity(settings.chat_id)
            if isinstance(chat, types.User) or (isinstance(chat, types.Channel) and not chat.megagroup):
                raise ValueError("TARGET_CHAT_ID must identify a group")
            if isinstance(chat, (types.Chat, types.Channel)):
                actual_chat = utils.get_peer_id(chat)
                if actual_chat != settings.chat_id:
                    log.error("Group ID mismatch account=%s configured=%s resolved=%s; use the resolved TARGET_CHAT_ID",
                              settings.account_id, settings.chat_id, actual_chat)
                    raise ValueError("TARGET_CHAT_ID must match the Telegram marked group ID")
            gateway = TelethonGateway(client, settings, identity)
            worker = Worker(settings, store, identity, PersonaDecider(decider, settings), gateway, coordinator=coordinator)
            try:
                for message in await gateway.sync():
                    worker.ingest(message)
                store.set_state("last_sync", time.time())
            except TelegramBackoff as exc:
                worker._block_telegram(exc)
            except Exception as exc:
                store.set_block("telegram", time.time() + 60)
                log.warning("Startup history unavailable (%s); retry deferred, queue retained",
                            type(exc).__name__)
            log.info("Ready account=%s mode=%s model=%s cap=%s/%s-hour (no fixed gap)",
                     settings.account_id, "LIVE" if settings.auto_send else "PREVIEW",
                     settings.groq_model, settings.max_actions, settings.window_mode)
            async def health_loop():
                connected_before = True
                while True:
                    connected = client.is_connected()
                    store.set_state("heartbeat", time.time())
                    store.set_state("health", "connected" if connected else "reconnecting")
                    if connected and not connected_before:
                        # No generation after reconnect until history is reconciled.
                        worker.next_history_sync = 0
                        worker.wake.set()
                    connected_before = connected
                    await asyncio.sleep(5)
            health_task = asyncio.create_task(health_loop())
            worker_task = asyncio.create_task(worker.run())
            disconnect_task = asyncio.create_task(client.run_until_disconnected())
            done, _ = await asyncio.wait([worker_task, disconnect_task], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except FloodWaitError as exc:
            store.set_block("telegram", time.time() + exc.seconds)
            log.error("Telegram requires %.0fs wait; saved across restarts", exc.seconds)
        finally:
            for task in (worker_task, disconnect_task, health_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(t for t in (worker_task, disconnect_task, health_task) if t is not None), return_exceptions=True)
            store.set_state("health", "offline")
            store.set_state("heartbeat", time.time())
            try:
                if client is not None:
                    await client.disconnect()
            finally:
                try:
                    if decider is not None and shared_decider is None:
                        await decider.aclose()
                finally:
                    store.close()
