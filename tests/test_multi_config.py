"""Multi-account configuration is read-only and uses only synthetic secrets."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from challenger.config import load_application_settings


VALID_ENV = {
    "API_ID": "12345", "API_HASH": "synthetic-telegram-secret",
    "TARGET_CHAT_ID": "-10012345", "GROQ_API_KEY": "synthetic-groq-secret",
    "GROQ_MODEL": "configured-model",
}


class MultiConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        environment = patch.dict(os.environ, VALID_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_defaults_enable_only_first_account_without_creating_files(self):
        app = load_application_settings(self.base)
        self.assertEqual(len(app.accounts), 1)
        account = app.accounts[0]
        self.assertEqual(account.account_id, "account_1")
        self.assertEqual(account.session_path, self.base / "sessions/account_1")
        self.assertEqual(account.database_path, self.base / "data/activity_account_1.sqlite3")
        self.assertIsNone(account.legacy_session_path)
        self.assertFalse(account.auto_send)
        self.assertFalse(account.automation_disclosed)
        self.assertFalse(app.group_automation_allowed)
        self.assertEqual(app.coordinator_path, self.base / "data/coordinator.sqlite3")
        self.assertEqual((app.groq_concurrency, app.group_max_actions, app.restart_delay),
                         (1, 25, 30))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_legacy_session_only_marked_for_copy_and_database_stays_in_place(self):
        session = self.base / "account_1.session"
        database = self.base / "activity_account_1.sqlite3"
        session.write_bytes(b"existing fake session")
        database.write_bytes(b"existing fake history")
        account = load_application_settings(self.base).accounts[0]
        self.assertEqual(account.session_path, self.base / "sessions/account_1")
        self.assertEqual(account.legacy_session_path, self.base / "account_1")
        self.assertEqual(account.database_path, database)
        self.assertEqual(session.read_bytes(), b"existing fake session")
        self.assertEqual(database.read_bytes(), b"existing fake history")
        self.assertEqual(set(self.base.iterdir()), {session, database})

    def test_existing_dedicated_session_is_never_marked_for_overwriting(self):
        (self.base / "sessions").mkdir()
        (self.base / "sessions/account_1.session").write_bytes(b"dedicated session")
        (self.base / "account_1.session").write_bytes(b"legacy session")
        self.assertIsNone(load_application_settings(self.base).accounts[0].legacy_session_path)

    def test_dotenv_loaded_once_with_process_environment_priority(self):
        env_path = self.base / ".env"
        original = b"# Keep comments\r\nAUTO_SEND=true\r\nGROQ_MODEL=file-model\r\n"
        env_path.write_bytes(original)
        with patch.dict(os.environ, {"AUTO_SEND": "false"}), patch(
            "challenger.config.load_dotenv", wraps=__import__("dotenv").load_dotenv
        ) as loader:
            app = load_application_settings(self.base)
        loader.assert_called_once_with(env_path, override=False)
        self.assertFalse(app.accounts[0].auto_send)
        self.assertEqual(app.accounts[0].groq_model, "configured-model")
        self.assertEqual(env_path.read_bytes(), original)

    def test_per_account_credentials_and_target_override_shared_defaults(self):
        with patch.dict(os.environ, {
            "ACCOUNT_2_ENABLED": "true", "ACCOUNT_2_API_ID": "67890",
            "ACCOUNT_2_API_HASH": "other-synthetic-secret", "ACCOUNT_2_TARGET_CHAT_ID": "-10099999",
        }):
            first, second = load_application_settings(self.base).accounts
        self.assertEqual((first.api_id, first.api_hash, first.chat_id),
                         (12345, VALID_ENV["API_HASH"], -10012345))
        self.assertEqual((second.api_id, second.api_hash, second.chat_id),
                         (67890, "other-synthetic-secret", -10099999))
        self.assertEqual((first.groq_api_key, first.groq_model), (second.groq_api_key, second.groq_model))
        self.assertEqual(second.session_path, self.base / "sessions/account_2")
        self.assertEqual(second.database_path, self.base / "data/activity_account_2.sqlite3")

    def test_all_five_accounts_have_independent_paths_and_shared_limits(self):
        with patch.dict(os.environ, {f"ACCOUNT_{slot}_ENABLED": "true" for slot in range(1, 6)}):
            app = load_application_settings(self.base)
        self.assertEqual(len(app.accounts), 5)
        self.assertEqual(len({account.session_path for account in app.accounts}), 5)
        self.assertEqual(len({account.database_path for account in app.accounts}), 5)
        self.assertTrue(all(account.api_id == 12345 for account in app.accounts))
        self.assertTrue(all(not account.auto_send for account in app.accounts))
        self.assertEqual(app.group_max_actions, 25)

    def test_disabled_slots_do_not_require_or_validate_credentials(self):
        with patch.dict(os.environ, {
            "ACCOUNT_2_API_ID": "invalid", "ACCOUNT_2_API_HASH": "",
            "ACCOUNT_2_TARGET_CHAT_ID": "invalid", "ACCOUNT_2_SESSION_PATH": "",
        }):
            self.assertEqual(len(load_application_settings(self.base).accounts), 1)

    def test_first_account_may_be_disabled_and_shared_telegram_credentials_omitted(self):
        env = {
            "ACCOUNT_1_ENABLED": "false", "ACCOUNT_3_ENABLED": "true",
            "ACCOUNT_3_API_ID": "67890", "ACCOUNT_3_API_HASH": "synthetic-secret",
            "ACCOUNT_3_TARGET_CHAT_ID": "-10099999", "GROQ_API_KEY": "synthetic-groq-secret",
        }
        with patch.dict(os.environ, env, clear=True):
            app = load_application_settings(self.base)
        self.assertEqual([account.account_id for account in app.accounts], ["account_3"])

    def test_explicit_legacy_paths_are_preserved_without_copy_metadata(self):
        with patch.dict(os.environ, {
            "SESSION_PATH": "old/session", "DATABASE_PATH": "old/history.sqlite3",
        }):
            account = load_application_settings(self.base).accounts[0]
        self.assertEqual(account.session_path, self.base / "old/session")
        self.assertEqual(account.database_path, self.base / "old/history.sqlite3")
        self.assertIsNone(account.legacy_session_path)

    def test_slot_specific_paths_override_legacy_paths(self):
        with patch.dict(os.environ, {
            "SESSION_PATH": "old/session", "DATABASE_PATH": "old/history.sqlite3",
            "ACCOUNT_1_SESSION_PATH": str(self.base / "selected/session"),
            "ACCOUNT_1_DATABASE_PATH": "selected/history.sqlite3",
        }):
            account = load_application_settings(self.base).accounts[0]
        self.assertEqual(account.session_path, self.base / "selected/session")
        self.assertEqual(account.database_path, self.base / "selected/history.sqlite3")

    def test_legacy_account_id_preserves_database_namespace_and_migration_source(self):
        history = self.base / "activity_existing_label.sqlite3"
        history.write_bytes(b"history")
        (self.base / "existing_label.session").write_bytes(b"session")
        with patch.dict(os.environ, {"ACCOUNT_ID": "existing_label"}):
            account = load_application_settings(self.base).accounts[0]
        self.assertEqual(account.account_id, "existing_label")
        self.assertEqual(account.database_path, history)
        self.assertEqual(account.legacy_session_path, self.base / "existing_label")
        self.assertEqual(account.session_path, self.base / "sessions/account_1")

    def test_conflicting_account_ids_are_rejected(self):
        with patch.dict(os.environ, {"ACCOUNT_ID": "account_2", "ACCOUNT_2_ENABLED": "true"}):
            with self.assertRaisesRegex(ValueError, "ACCOUNT_ID conflicts"):
                load_application_settings(self.base)

    def test_session_alias_with_or_without_suffix_is_rejected(self):
        with patch.dict(os.environ, {
            "ACCOUNT_2_ENABLED": "true", "ACCOUNT_1_SESSION_PATH": "sessions/same",
            "ACCOUNT_2_SESSION_PATH": "sessions/same.session",
        }):
            with self.assertRaisesRegex(ValueError, "Separate paths required"):
                load_application_settings(self.base)

    def test_database_alias_is_rejected(self):
        with patch.dict(os.environ, {
            "ACCOUNT_2_ENABLED": "true", "ACCOUNT_1_DATABASE_PATH": "data/shared.sqlite3",
            "ACCOUNT_2_DATABASE_PATH": "data/../data/shared.sqlite3",
        }):
            with self.assertRaisesRegex(ValueError, "Separate paths required"):
                load_application_settings(self.base)

    def test_coordinator_cannot_overwrite_account_history_or_session(self):
        for name, value in (("ACCOUNT_1_DATABASE_PATH", "data/coordinator.sqlite3"),
                            ("ACCOUNT_1_SESSION_PATH", "data/shared")):
            override = {name: value}
            if "SESSION" in name:
                override["COORDINATOR_DATABASE_PATH"] = "data/shared.session"
            with self.subTest(name=name), patch.dict(os.environ, override):
                with self.assertRaisesRegex(ValueError, "Separate paths required"):
                    load_application_settings(self.base)

    def test_another_slot_cannot_use_migration_source_session(self):
        (self.base / "account_1.session").write_bytes(b"session")
        with patch.dict(os.environ, {
            "ACCOUNT_2_ENABLED": "true", "ACCOUNT_2_SESSION_PATH": "account_1.session",
        }):
            with self.assertRaisesRegex(ValueError, "Separate paths required"):
                load_application_settings(self.base)

    def test_more_than_five_accounts_and_empty_application_are_rejected(self):
        for name in ("ACCOUNT_0_ENABLED", "ACCOUNT_6_ENABLED", "ACCOUNT_25_ENABLED"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "true"}):
                with self.assertRaisesRegex(ValueError, "Only ACCOUNT_1 through ACCOUNT_5"):
                    load_application_settings(self.base)
        with patch.dict(os.environ, {"ACCOUNT_1_ENABLED": "false"}):
            with self.assertRaisesRegex(ValueError, "At least one"):
                load_application_settings(self.base)

    def test_enabled_booleans_are_validated(self):
        with patch.dict(os.environ, {"ACCOUNT_2_ENABLED": "maybe"}):
            with self.assertRaisesRegex(ValueError, "ACCOUNT_2_ENABLED must be true or false"):
                load_application_settings(self.base)

    def test_missing_credentials_name_settings_without_exposing_secrets(self):
        for name, value in (("ACCOUNT_2_API_ID", "bad-private-value"),
                            ("ACCOUNT_2_TARGET_CHAT_ID", "0")):
            with self.subTest(name=name), patch.dict(os.environ, {
                "ACCOUNT_2_ENABLED": "true", name: value,
            }):
                with self.assertRaises(ValueError) as captured:
                    load_application_settings(self.base)
                self.assertIn(name, str(captured.exception))
                for secret in ("bad-private-value", VALID_ENV["API_HASH"], VALID_ENV["GROQ_API_KEY"]):
                    self.assertNotIn(secret, str(captured.exception))

    def test_global_and_group_limits_cannot_be_relaxed(self):
        with patch.dict(os.environ, {
            "MAX_ACTIONS_PER_HOUR": "100", "MIN_GAP_SECONDS": "1",
            "GROUP_MAX_ACTIONS_PER_HOUR": "100", "GROUP_MIN_GAP_SECONDS": "1",
            "GROQ_CONCURRENCY": "100", "ACCOUNT_RESTART_DELAY_SECONDS": "1",
        }):
            app = load_application_settings(self.base)
        self.assertEqual(app.accounts[0].max_actions, 5)
        self.assertEqual((app.group_max_actions, app.groq_concurrency, app.restart_delay),
                         (25, 3, 30))

    def test_stricter_group_limits_and_retry_settings_are_honored(self):
        with patch.dict(os.environ, {
            "GROUP_MAX_ACTIONS_PER_HOUR": "2", "GROUP_MIN_GAP_SECONDS": "1800",
            "GROQ_CONCURRENCY": "2", "ACCOUNT_RESTART_DELAY_SECONDS": "120",
        }):
            app = load_application_settings(self.base)
        self.assertEqual((app.group_max_actions, app.groq_concurrency, app.restart_delay),
                         (2, 2, 120))

    def test_lower_bounds_and_numeric_validation(self):
        with patch.dict(os.environ, {"GROUP_MAX_ACTIONS_PER_HOUR": "0", "GROQ_CONCURRENCY": "0"}):
            app = load_application_settings(self.base)
        self.assertEqual((app.group_max_actions, app.groq_concurrency), (1, 1))
        for name in ("GROUP_MAX_ACTIONS_PER_HOUR", "GROQ_CONCURRENCY",
                     "ACCOUNT_RESTART_DELAY_SECONDS"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "invalid"}):
                with self.assertRaisesRegex(ValueError, name + " must be an integer"):
                    load_application_settings(self.base)

    def test_explicit_live_mode_applies_to_all_enabled_accounts(self):
        with patch.dict(os.environ, {"AUTO_SEND": "true", "ACCOUNT_2_ENABLED": "true"}):
            self.assertTrue(all(account.auto_send for account in load_application_settings(self.base).accounts))

    def test_empty_paths_and_directories_are_rejected(self):
        for name in ("ACCOUNT_1_SESSION_PATH", "ACCOUNT_1_DATABASE_PATH", "COORDINATOR_DATABASE_PATH"):
            for value in ("", str(self.base)):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    with self.assertRaisesRegex(ValueError, name):
                        load_application_settings(self.base)

    def test_group_and_disclosure_attestations_are_explicit_per_account(self):
        with patch.dict(os.environ, {
            "ACCOUNT_2_ENABLED": "true", "AUTOMATION_DISCLOSED": "true",
            "ACCOUNT_2_AUTOMATION_DISCLOSED": "false", "GROUP_AUTOMATION_ALLOWED": "yes",
        }):
            app = load_application_settings(self.base)
        self.assertTrue(app.group_automation_allowed)
        self.assertTrue(app.accounts[0].automation_disclosed)
        self.assertFalse(app.accounts[1].automation_disclosed)

    def test_attestations_validate_boolean_values_but_do_not_block_config_loading(self):
        with patch.dict(os.environ, {"AUTO_SEND": "true"}):
            app = load_application_settings(self.base)
        self.assertTrue(app.accounts[0].auto_send)
        self.assertFalse(app.group_automation_allowed)
        self.assertFalse(app.accounts[0].automation_disclosed)
        for name in ("GROUP_AUTOMATION_ALLOWED", "AUTOMATION_DISCLOSED", "ACCOUNT_1_AUTOMATION_DISCLOSED"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "perhaps"}):
                with self.assertRaisesRegex(ValueError, name + " must be true or false"):
                    load_application_settings(self.base)


if __name__ == "__main__":
    unittest.main()
