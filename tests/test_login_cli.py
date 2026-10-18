"""Exercise the real CLI and console input with synthetic Telegram clients only."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import main
from challenger.auth import LoginError
from challenger.config import load_application_settings
from challenger.application import login_accounts


ENV = {"API_ID": "123", "API_HASH": "fake-hash", "TARGET_CHAT_ID": "-123",
       "GROQ_API_KEY": "fake-key", "AUTO_SEND": "true"}
ROOT = Path(__file__).resolve().parents[1]


class LoginCliTests(unittest.TestCase):
    def test_sequence_stops_at_failure_without_requesting_next_accounts(self):
        import asyncio
        with patch("challenger.application.login_account", new=AsyncMock(side_effect=LoginError("retry later"))) as login:
            with self.assertRaisesRegex(LoginError, "account_2: retry later"):
                asyncio.run(login_accounts(None, ("account_2", "account_3")))
        login.assert_awaited_once_with(None, "account_2")

    def test_login_loads_disabled_slots_without_enabling_or_sending(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV, clear=True):
            base = Path(directory)
            app = load_application_settings(base, login_accounts=("account_2", "account_3", "account_4", "account_5"))
            self.assertEqual([a.account_id for a in app.accounts], [f"account_{i}" for i in range(1, 6)])
            self.assertTrue(all(not a.auto_send for a in app.accounts))
            self.assertEqual(len(load_application_settings(base).accounts), 1)
            self.assertEqual(list(base.iterdir()), [])

    def test_single_login_and_failure_exit_code_never_start_worker(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV, clear=True):
            app = load_application_settings(Path(directory), login_accounts=("account_2",))
            with patch.object(main, "load_application_settings", return_value=app) as loader, \
                 patch("sys.argv", ["main.py", "--login", "account_2"]), \
                 patch("challenger.application.login_accounts", new=AsyncMock(side_effect=LoginError("Connection timed out"))) as login, \
                 patch("challenger.application.run_application") as worker, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main.main(), 1)
            self.assertEqual(loader.call_args.kwargs["login_accounts"], ("account_2",))
            login.assert_awaited_once_with(app, ("account_2",))
            worker.assert_not_called()
            self.assertIn("Authentication failed: Connection timed out", output.getvalue())

    def test_real_cli_input_for_four_accounts_is_sequential_and_preserves_account_one(self):
        # Run the actual entry point in a subprocess. Only Telegram transport and
        # configuration root are replaced; input(), CLI, auth and SQLite are real.
        harness = r'''
import os, runpy, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from challenger.config import load_application_settings
base = Path(sys.argv[1])
import challenger.auth
ids = iter(range(102, 106))
def client(*args, **kwargs):
    identity = next(ids)
    return SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=False),
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="fake-hash")),
        sign_in=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=identity, username="")))
def config(_, **kwargs):
    return load_application_settings(base, **kwargs)
sys.argv = ["main.py", "--login-all"]
with patch.dict(os.environ, {"API_ID":"123", "API_HASH":"fake", "TARGET_CHAT_ID":"-123",
                            "GROQ_API_KEY":"fake", "AUTO_SEND":"true"}, clear=True), \
     patch("challenger.config.load_application_settings", side_effect=config), \
     patch("challenger.auth.TelegramClient", side_effect=client):
    runpy.run_path("main.py", run_name="__main__")
'''
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "sessions").mkdir()
            (base / "data").mkdir()
            first_session = base / "sessions/account_1.session"
            first_history = base / "data/activity_account_1.sqlite3"
            first_session.write_bytes(b"untouched existing authorization")
            first_history.write_bytes(b"untouched existing history")
            result = subprocess.run([sys.executable, "-c", harness, directory], cwd=ROOT,
                                    input="+234000000000\n12345\n" * 4, capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            previous = -1
            for i in range(2, 6):
                for label in ("Connecting", "Phone number", "Telegram verification code", "Authentication successful"):
                    position = result.stdout.index(f"[account_{i}] {label}")
                    self.assertGreater(position, previous)
                    previous = position
            self.assertEqual(result.stdout.count("AUTO_SEND=false"), 4)
            self.assertNotIn("[account_1]", result.stdout)
            self.assertEqual(first_session.read_bytes(), b"untouched existing authorization")
            self.assertEqual(first_history.read_bytes(), b"untouched existing history")


if __name__ == "__main__":
    unittest.main()
