import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon import types
from telethon.errors import FloodWaitError

from challenger.config import Settings
from challenger.engine import TelegramBackoff
from challenger.models import Identity
from challenger.storage import Store
from challenger.telegram_app import TelethonGateway, convert_message, instance_lock, run


def message(message_id=42, text="Which amount was listed for the fare?", *, sender_id=7,
            reply_to_id=None, outgoing=False, when=None, entities=None):
    return SimpleNamespace(
        id=message_id, message=text, raw_text=text, sender_id=sender_id,
        reply_to_msg_id=reply_to_id, out=outgoing,
        date=datetime.fromtimestamp(time.time() if when is None else when, timezone.utc),
        edit_date=None, entities=entities or [], sender=SimpleNamespace(bot=False),
        via_bot_id=None, fwd_from=None,
    )


def settings(base=Path(".")):
    return Settings(123, "fake-hash", -123, "fake-key", "test-model",
                    session_path=base / "test-account",
                    database_path=base / "history.sqlite3")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalization_fetches_missing_parent_and_preserves_mentions(self):
        client = SimpleNamespace(get_messages=AsyncMock(return_value=[message(41, sender_id=99)]))
        gateway = TelethonGateway(client, settings(), Identity(99, "our_bot"))
        original = message(text="@our_bot are you automated?", reply_to_id=41,
                           entities=[types.MessageEntityMentionName(0, 8, 99)])
        converted, = await gateway.normalize([original])
        client.get_messages.assert_awaited_once_with(-123, ids=[41])
        self.assertEqual(converted.reply_to_id, 41)
        self.assertEqual(converted.reply_to_sender_id, 99)
        self.assertEqual(converted.mentions, ("our_bot",))
        self.assertEqual(converted.mentioned_ids, (99,))

    async def test_conversion_with_unknown_parent_is_local_and_conservative(self):
        converted = convert_message(message(reply_to_id=20), -123)
        self.assertEqual(converted.reply_to_id, 20)
        self.assertIsNone(converted.reply_to_sender_id)

    async def test_missing_parent_remains_unknown(self):
        client = SimpleNamespace(get_messages=AsyncMock(return_value=[None]))
        gateway = TelethonGateway(client, settings(), Identity(99))
        converted, = await gateway.normalize([None, message(reply_to_id=20)])
        self.assertIsNone(converted.reply_to_sender_id)

    async def test_send_targets_exact_message_without_formatting(self):
        client = SimpleNamespace(send_message=AsyncMock(return_value=message(
            90, "It's listed there, check again", outgoing=True, reply_to_id=42)))
        gateway = TelethonGateway(client, replace(settings(), auto_send=True), Identity(99))
        sent = await gateway.send("It's listed there, check again", 42)
        client.send_message.assert_awaited_once_with(
            -123, "It's listed there, check again", reply_to=42,
            link_preview=False, parse_mode=None)
        self.assertEqual(sent.message_id, 90)
        self.assertEqual(sent.reply_to_id, 42)

    async def test_floodwait_translated_without_retry_for_each_network_operation(self):
        client = SimpleNamespace(
            get_messages=AsyncMock(side_effect=FloodWaitError(None, capture=900)),
            send_message=AsyncMock(side_effect=FloodWaitError(None, capture=900)),
        )
        gateway = TelethonGateway(client, replace(settings(), auto_send=True), Identity(99))
        for operation in (gateway.sync, lambda: gateway.refresh(42),
                          lambda: gateway.send("A short useful reply", 42)):
            with self.assertRaises(TelegramBackoff) as caught:
                await operation()
            self.assertEqual(caught.exception.seconds, 900)
        self.assertEqual(client.get_messages.await_count, 2)
        self.assertEqual(client.send_message.await_count, 1)

    async def test_refresh_keeps_all_following_messages_beyond_context_window(self):
        target = message()
        following = [message(index, reply_to_id=42) for index in range(43, 123)]

        async def iterate(chat_id, **kwargs):
            if "min_id" in kwargs:
                self.assertEqual(kwargs, {"min_id": 42, "limit": 201})
                for item in following:
                    yield item

        client = SimpleNamespace(get_messages=AsyncMock(return_value=target), iter_messages=iterate)
        result = await TelethonGateway(client, settings(), Identity(99)).refresh(42)
        self.assertEqual(result.target.message_id, 42)
        self.assertEqual(len(result.messages), 81)
        self.assertEqual(result.messages[1].reply_to_sender_id, 7)

    async def test_refresh_overflow_fails_closed(self):
        async def iterate(chat_id, **kwargs):
            for index in range(43, 244):
                yield message(index)

        client = SimpleNamespace(get_messages=AsyncMock(return_value=message()), iter_messages=iterate)
        with self.assertRaises(TelegramBackoff):
            await TelethonGateway(client, settings(), Identity(99)).refresh(42)

    async def test_deleted_target_is_unavailable(self):
        client = SimpleNamespace(get_messages=AsyncMock(return_value=None))
        result = await TelethonGateway(client, settings(), Identity(99)).refresh(42)
        self.assertIsNone(result.target)
        self.assertEqual(result.messages, [])

    async def test_outgoing_history_overflow_disables_sending(self):
        async def iterate(chat_id, **kwargs):
            for index in range(300):
                yield message(index, outgoing=True)

        client = SimpleNamespace(iter_messages=iterate)
        with self.assertRaises(RuntimeError):
            await TelethonGateway(client, settings(), Identity(99)).outgoing()


class LockTests(unittest.TestCase):
    def test_same_session_is_exclusive_and_lock_releases(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.lock"
            with instance_lock(path):
                with self.assertRaises(RuntimeError):
                    with instance_lock(path):
                        self.fail("Second holder acquired the session lock")
            with instance_lock(path):
                self.assertTrue(path.exists())


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.settings = settings(self.base)
        self.stores = []
        self.decider = SimpleNamespace(aclose=AsyncMock())
        self.worker = SimpleNamespace(run=AsyncMock(), ingest=MagicMock(),
                                      _block_telegram=MagicMock())
        self.client = SimpleNamespace(
            start=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=99, username="our_bot")),
            get_entity=AsyncMock(return_value=SimpleNamespace()),
            add_event_handler=MagicMock(), run_until_disconnected=AsyncMock(), disconnect=AsyncMock(),
        )

    async def asyncTearDown(self):
        self.directory.cleanup()

    def make_store(self, *args, **kwargs):
        store = Store(*args, **kwargs)
        self.stores.append(store)
        return store

    async def invoke(self, *, sync=None, decider_factory=None, client_factory=None):
        gateway = SimpleNamespace(sync=sync or AsyncMock(return_value=[]))
        with patch("challenger.telegram_app.Store", side_effect=self.make_store), \
             patch("challenger.telegram_app.TelegramClient", client_factory or MagicMock(return_value=self.client)), \
             patch("challenger.telegram_app.GroqDecider", decider_factory or MagicMock(return_value=self.decider)), \
             patch("challenger.telegram_app.Worker", return_value=self.worker), \
             patch("challenger.telegram_app.TelethonGateway", return_value=gateway):
            await run(self.settings)

    def assert_store_closed(self):
        import sqlite3
        self.assertEqual(len(self.stores), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.stores[0].get(42)

    async def test_early_startup_events_are_durable_before_worker_exists(self):
        async def start():
            self.assertEqual(self.client.add_event_handler.call_count, 3)
            callback = self.client.add_event_handler.call_args_list[0].args[0]
            await callback(SimpleNamespace(is_group=True, message=message()))
            await callback(SimpleNamespace(is_group=True, message=message(
                43, "Earlier outgoing text", outgoing=True, sender_id=99)))

        self.client.start.side_effect = start
        await self.invoke()
        self.assert_store_closed()
        store = Store(self.settings.database_path, "account_1", -123)
        try:
            self.assertEqual(store.get(42).sender_id, 7)
            self.assertEqual(store.status(42), "pending")
            self.assertTrue(store.get(43).outgoing)
            self.assertEqual(store.quota_wait(time.time()), 0)
            self.assertEqual(store.quota_remaining(time.time()), 4)
        finally:
            store.close()

    async def test_startup_history_failure_is_deferred_and_resources_close(self):
        await self.invoke(sync=AsyncMock(side_effect=TimeoutError()))
        self.worker.run.assert_awaited_once()
        self.decider.aclose.assert_awaited_once()
        self.client.disconnect.assert_awaited_once()
        self.assert_store_closed()
        store = Store(self.settings.database_path, "account_1", -123)
        try:
            self.assertGreater(store.blocked_until("telegram"), time.time() + 55)
        finally:
            store.close()

    async def test_received_event_logs_queue_and_rejects_wrong_chat(self):
        async def start():
            callback = self.client.add_event_handler.call_args_list[0].args[0]
            await callback(SimpleNamespace(is_group=True, chat_id=-123, message=message(42)))
            await callback(SimpleNamespace(is_group=True, chat_id=-999, message=message(43)))

        self.client.start.side_effect = start
        with self.assertLogs("challenger.telegram_app", level="INFO") as logged:
            await self.invoke()
        output = "\n".join(logged.output)
        self.assertIn("RECEIVED", output)
        self.assertIn("QUEUED startup", output)
        self.assertIn("group mismatch", output)
        store = Store(self.settings.database_path, "account_1", -123)
        try:
            self.assertIsNotNone(store.get(42))
            self.assertIsNone(store.get(43))
        finally:
            store.close()

    async def test_client_constructor_failure_closes_store(self):
        with self.assertRaisesRegex(RuntimeError, "constructor failed"):
            await self.invoke(client_factory=MagicMock(side_effect=RuntimeError("constructor failed")))
        self.assert_store_closed()

    async def test_decider_constructor_failure_disconnects_client_and_closes_store(self):
        with self.assertRaisesRegex(RuntimeError, "decider failed"):
            await self.invoke(decider_factory=MagicMock(side_effect=RuntimeError("decider failed")))
        self.client.disconnect.assert_awaited_once()
        self.assert_store_closed()

    async def test_disconnect_failure_still_closes_decider_and_store(self):
        self.client.disconnect.side_effect = RuntimeError("disconnect failed")
        with self.assertRaisesRegex(RuntimeError, "disconnect failed"):
            await self.invoke()
        self.decider.aclose.assert_awaited_once()
        self.assert_store_closed()


if __name__ == "__main__":
    unittest.main()
