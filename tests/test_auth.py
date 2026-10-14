from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError

from challenger.auth import AuthenticationRequired, authenticate, prepare_account, session_file
from challenger.config import Settings
from challenger.engine import TelegramBackoff
from challenger.telegram_app import instance_lock


def settings(base, account_id="account_1", **kwargs):
    return Settings(123, "fake-api-hash", -123, "fake-groq-key", "model",
                    account_id=account_id, session_path=base / "sessions" / account_id,
                    database_path=base / "data" / f"{account_id}.sqlite3", **kwargs)


def fake_client(user_id=99, username="testuser", authorized=True):
    return SimpleNamespace(
        connect=AsyncMock(), start=AsyncMock(), disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=authorized),
        get_me=AsyncMock(return_value=SimpleNamespace(id=user_id, username=username)),
    )


def create_session(path, token=b"synthetic-session-secret"):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("CREATE TABLE sessions (auth_key BLOB)")
            connection.execute("INSERT INTO sessions VALUES (?)", (token,))


class SessionPreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.base = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_session_filename_matches_telethon_suffix_rules(self):
        self.assertEqual(session_file(Path("account")), Path("account.session"))
        self.assertEqual(session_file(Path("account.session")), Path("account.session"))
        self.assertEqual(session_file(Path("account.custom")), Path("account.custom.session"))
        self.assertEqual(session_file(Path("account.SESSION")), Path("account.SESSION.session"))

    def test_legacy_session_copied_with_source_bytes_and_authorization_preserved(self):
        source = self.base / "old account.session"
        create_session(source)
        before = source.read_bytes()
        before_stat = source.stat()
        configured = settings(self.base, legacy_session_path=source)
        prepare_account(configured)
        destination = session_file(configured.session_path)
        self.assertTrue(destination.is_file())
        with closing(sqlite3.connect(destination)) as connection:
            token, = connection.execute("SELECT auth_key FROM sessions").fetchone()
        self.assertEqual(token, b"synthetic-session-secret")
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(source.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), stat.S_IMODE(before_stat.st_mode))

    def test_existing_destination_and_history_are_never_overwritten(self):
        source = self.base / "legacy.session"
        create_session(source, b"legacy-token")
        configured = settings(self.base, legacy_session_path=source)
        configured.session_path.parent.mkdir()
        configured.database_path.parent.mkdir()
        destination = session_file(configured.session_path)
        create_session(destination, b"existing-destination-token")
        configured.database_path.write_bytes(b"existing database history")
        original = destination.read_bytes()
        prepare_account(configured)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(configured.database_path.read_bytes(), b"existing database history")

    def test_failed_copy_removes_only_its_new_destination(self):
        source = self.base / "legacy.session"
        source.write_bytes(b"not a SQLite database")
        configured = settings(self.base, legacy_session_path=source)
        with self.assertRaises(sqlite3.DatabaseError):
            prepare_account(configured)
        self.assertEqual(source.read_bytes(), b"not a SQLite database")
        self.assertFalse(session_file(configured.session_path).exists())

    def test_preparation_respects_the_normalized_destination_lock(self):
        configured = replace(settings(self.base), session_path=self.base / "sessions" / "account.custom")
        destination = session_file(configured.session_path)
        with instance_lock(destination.with_suffix(".challenger.lock")):
            with self.assertRaises(RuntimeError):
                prepare_account(configured)
        self.assertFalse(destination.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits are not Windows ACLs")
    def test_private_created_paths_do_not_chmod_an_existing_shared_parent(self):
        self.base.chmod(0o755)
        configured = settings(self.base)
        prepare_account(configured)
        self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(configured.session_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(configured.database_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(session_file(configured.session_path).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(configured.database_path.stat().st_mode), 0o600)


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.settings = settings(self.base)

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_authorized_noninteractive_account_never_requests_login(self):
        client = fake_client(username=None)
        with patch("challenger.auth.TelegramClient", return_value=client) as factory, \
             patch("challenger.auth.getpass.getpass", side_effect=AssertionError("Unexpected prompt")):
            identity = await authenticate(self.settings)
        self.assertEqual(identity.user_id, 99)
        self.assertEqual(identity.username, "")
        client.connect.assert_awaited_once()
        client.is_user_authorized.assert_awaited_once()
        client.start.assert_not_awaited()
        client.disconnect.assert_awaited_once()
        self.assertEqual(factory.call_args.kwargs["request_retries"], 0)
        self.assertEqual(factory.call_args.kwargs["flood_sleep_threshold"], 0)
        self.assertFalse(factory.call_args.kwargs["catch_up"])

    async def test_unauthorized_noninteractive_account_requests_explicit_login(self):
        client = fake_client(authorized=False)
        with patch("challenger.auth.TelegramClient", return_value=client), \
             patch("challenger.auth.getpass.getpass", side_effect=AssertionError("Unexpected prompt")):
            with self.assertRaises(AuthenticationRequired) as caught:
                await authenticate(self.settings)
        self.assertEqual(caught.exception.account_id, "account_1")
        self.assertIn("--login account_1", str(caught.exception))
        client.start.assert_not_awaited()
        client.get_me.assert_not_awaited()
        client.disconnect.assert_awaited_once()

    async def test_explicit_login_uses_hidden_phone_code_and_2fa_callbacks(self):
        client = fake_client()
        received = []

        async def start(**callbacks):
            received.extend(callbacks[name]() for name in ("phone", "code_callback", "password"))

        client.start.side_effect = start
        output = io.StringIO()
        secrets = ["+234000000000", "12345", "synthetic-2fa-password"]
        with patch("challenger.auth.TelegramClient", return_value=client), \
             patch("challenger.auth.getpass.getpass", side_effect=secrets) as hidden_input, \
             redirect_stdout(output), redirect_stderr(output):
            identity = await authenticate(self.settings, interactive=True)
        self.assertEqual(identity.user_id, 99)
        self.assertEqual(received, secrets)
        self.assertEqual(hidden_input.call_count, 3)
        self.assertTrue(all("account_1" in call.args[0] for call in hidden_input.call_args_list))
        self.assertTrue(all(secret not in output.getvalue() for secret in secrets))
        client.connect.assert_not_awaited()
        client.disconnect.assert_awaited_once()

    async def test_multiple_accounts_use_independent_session_paths(self):
        first, second = fake_client(99), fake_client(100)
        second_settings = settings(self.base, account_id="account_2")
        with patch("challenger.auth.TelegramClient", side_effect=[first, second]) as factory:
            first_identity = await authenticate(self.settings)
            second_identity = await authenticate(second_settings)
        self.assertNotEqual(first_identity.user_id, second_identity.user_id)
        probed = [Path(call.args[0]) for call in factory.call_args_list]
        self.assertNotEqual(probed[0], probed[1])
        self.assertTrue(all(not path.exists() for path in probed))
        first.start.assert_not_awaited()
        second.start.assert_not_awaited()
        self.assertTrue(session_file(self.settings.session_path).exists())
        self.assertTrue(session_file(second_settings.session_path).exists())

    async def test_identity_probe_cannot_advance_persistent_update_state(self):
        prepare_account(self.settings)
        source = session_file(self.settings.session_path)
        from contextlib import closing
        with closing(sqlite3.connect(source)) as con:
            con.execute("CREATE TABLE update_state (pts INTEGER)")
            con.execute("INSERT INTO update_state VALUES (10)")
            con.commit()
        before = source.read_bytes()
        client = fake_client()
        probe_paths = []

        def factory(path, *args, **kwargs):
            probe_paths.append(Path(path))
            with closing(sqlite3.connect(path)) as con:
                con.execute("UPDATE update_state SET pts=999")
                con.commit()
            return client

        with patch("challenger.auth.TelegramClient", side_effect=factory):
            await authenticate(self.settings)
        self.assertEqual(source.read_bytes(), before)
        self.assertTrue(all(not path.exists() for path in probe_paths))

    async def test_floodwait_is_translated_and_client_is_disconnected(self):
        client = fake_client()
        client.connect.side_effect = FloodWaitError(None, capture=900)
        with patch("challenger.auth.TelegramClient", return_value=client):
            with self.assertRaises(TelegramBackoff) as caught:
                await authenticate(self.settings)
        self.assertEqual(caught.exception.seconds, 900)
        client.disconnect.assert_awaited_once()

    async def test_lookup_failure_disconnects_and_releases_lock(self):
        client = fake_client()
        client.get_me.side_effect = RuntimeError("Lookup failed")
        with patch("challenger.auth.TelegramClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "Lookup failed"):
                await authenticate(self.settings)
        client.disconnect.assert_awaited_once()
        with instance_lock(session_file(self.settings.session_path).with_suffix(".challenger.lock")):
            pass

    async def test_missing_identity_is_reported_as_authentication_required(self):
        client = fake_client()
        client.get_me.return_value = None
        with patch("challenger.auth.TelegramClient", return_value=client):
            with self.assertRaises(AuthenticationRequired):
                await authenticate(self.settings)
        client.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
