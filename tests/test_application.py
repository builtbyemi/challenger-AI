import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from challenger.application import login_account, run_application, supervise, validate_live_readiness
from challenger.auth import AuthenticationRequired
from challenger.config import ApplicationSettings, Settings
from challenger.coordinator import Coordinator
from challenger.groq_client import GroqConfigurationError
from challenger.models import Identity, Message
from challenger.selection import skip_reason
from main import run_with_shutdown


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.accounts = tuple(Settings(1, "private-hash", -100, "private-key", "test-model",
                                       account_id=f"account_{slot}",
                                       session_path=self.base / f"session_{slot}",
                                       database_path=self.base / f"database_{slot}.sqlite3")
                              for slot in (1, 2))
        self.app = ApplicationSettings(self.accounts, self.base / "coordinator.sqlite3", restart_delay=0.001)
        self.decider = type("Decider", (), {"aclose": AsyncMock()})()

    def tearDown(self):
        self.temp.cleanup()

    async def identify(self, account, interactive=False):
        return Identity(11 if account.account_id == "account_1" else 22)

    async def test_all_identities_registered_before_workers_and_shared_client_closed_once(self):
        seen = []

        async def account_run(account, **kwargs):
            self.assertEqual(kwargs["coordinator"].peer_ids(), {11, 22})
            self.assertIs(kwargs["shared_decider"], self.decider)
            self.assertTrue(kwargs["require_authorized"])
            seen.append(account.account_id)
            raise GroqConfigurationError()

        with patch("challenger.application.import_history", return_value=0), \
             patch("challenger.application.authenticate", side_effect=self.identify) as auth, \
             patch("challenger.application.SharedGroqDecider", return_value=self.decider) as factory, \
             patch("challenger.application.run_account", side_effect=account_run):
            await run_application(self.app)
        self.assertEqual(set(seen), {"account_1", "account_2"})
        self.assertTrue(all(call.kwargs == {"interactive": False} for call in auth.call_args_list))
        factory.assert_called_once()
        self.decider.aclose.assert_awaited_once()

    async def test_duplicate_actual_telegram_identity_stops_before_any_worker(self):
        with patch("challenger.application.import_history", return_value=0), \
             patch("challenger.application.authenticate", new=AsyncMock(return_value=Identity(11))), \
             patch("challenger.application.run_account", new=AsyncMock()) as runner:
            with self.assertRaises(ValueError):
                await run_application(self.app)
        runner.assert_not_called()

    async def test_unauthenticated_account_is_skipped_without_interactive_login(self):
        async def authenticate(account, interactive):
            self.assertFalse(interactive)
            if account.account_id == "account_2":
                raise AuthenticationRequired(account.account_id)
            return Identity(11)

        runner = AsyncMock(side_effect=GroqConfigurationError())
        with patch("challenger.application.import_history", return_value=0), \
             patch("challenger.application.authenticate", side_effect=authenticate), \
             patch("challenger.application.SharedGroqDecider", return_value=self.decider), \
             patch("challenger.application.run_account", runner):
            await run_application(self.app)
        self.assertEqual(runner.await_count, 1)
        self.assertEqual(runner.call_args.args[0].account_id, "account_1")

    async def test_connection_failure_retries_only_affected_account(self):
        counts = {"account_1": 0, "account_2": 0}

        async def run(account, **kwargs):
            counts[account.account_id] += 1
            if account.account_id == "account_1" and counts[account.account_id] == 1:
                raise ConnectionError("network unavailable")
            raise GroqConfigurationError()

        with patch("challenger.application.import_history", return_value=0), \
             patch("challenger.application.authenticate", side_effect=self.identify), \
             patch("challenger.application.SharedGroqDecider", return_value=self.decider), \
             patch("challenger.application.run_account", side_effect=run):
            await run_application(self.app)
        self.assertEqual(counts, {"account_1": 2, "account_2": 1})

    async def test_cancel_closes_all_account_tasks_and_shared_resources(self):
        started = asyncio.Event()
        running, closed = set(), set()

        async def run(account, **kwargs):
            running.add(account.account_id)
            if len(running) == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.add(account.account_id)

        with patch("challenger.application.import_history", return_value=0), \
             patch("challenger.application.authenticate", side_effect=self.identify), \
             patch("challenger.application.SharedGroqDecider", return_value=self.decider), \
             patch("challenger.application.run_account", side_effect=run):
            task = asyncio.create_task(run_application(self.app))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(closed, {"account_1", "account_2"})
        self.decider.aclose.assert_awaited_once()
        reopened = Coordinator(self.app.coordinator_path)
        self.assertEqual(reopened.peer_ids(), {11, 22})
        reopened.close()

    async def test_explicit_login_authenticates_only_selected_slot(self):
        with patch("challenger.application.authenticate", new=AsyncMock(return_value=Identity(22))) as auth, \
             patch("challenger.application.run_account", new=AsyncMock()) as runner, \
             patch("challenger.application.SharedGroqDecider") as factory:
            await login_account(self.app, "account_2")
        auth.assert_awaited_once_with(self.accounts[1], interactive=True)
        runner.assert_not_called()
        factory.assert_not_called()

    async def test_login_unknown_or_disabled_slot_does_not_authenticate(self):
        with patch("challenger.application.authenticate", new=AsyncMock()) as auth:
            with self.assertRaises(ValueError):
                await login_account(self.app, "account_5")
        auth.assert_not_called()

    async def test_live_requires_group_permission_and_disclosure_but_preview_does_not(self):
        validate_live_readiness(self.app)
        live_accounts = tuple(replace(a, auto_send=True) for a in self.accounts)
        live = replace(self.app, accounts=live_accounts)
        with self.assertRaises(ValueError):
            validate_live_readiness(live)
        with self.assertRaises(ValueError):
            validate_live_readiness(replace(live, group_automation_allowed=True))
        disclosed = tuple(replace(a, automation_disclosed=True) for a in live_accounts)
        validate_live_readiness(replace(live, accounts=disclosed, group_automation_allowed=True))

    async def test_service_shutdown_cancels_operation_and_runs_cleanup(self):
        handlers, cleaned = [], []

        async def operation():
            try:
                handlers[0](15, None)
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

        with patch("main.signal.signal", side_effect=lambda sig, handler: handlers.append(handler)):
            await run_with_shutdown(operation())
        self.assertEqual(cleaned, [True])
        self.assertEqual(len(handlers), 2)

    def test_score_farming_requests_are_ignored(self):
        for text in ("How can everyone boost our score?", "Can everyone farm points together?",
                     "Which leaderboard should everyone target?"):
            msg = Message(1, -100, 33, text, 1000)
            self.assertIn("leaderboard", skip_reason(msg, [], Identity(11), 1000))


if __name__ == "__main__":
    unittest.main()
