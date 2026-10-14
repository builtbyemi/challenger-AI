"""Exercise configuration using temporary files and synthetic credentials only."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from challenger.config import load_settings


VALID_ENV = {
    "API_ID": "12345",
    "API_HASH": "test-api-hash",
    "TARGET_CHAT_ID": "-10012345",
    "GROQ_API_KEY": "test-groq-key",
    "GROQ_MODEL": "configured-test-model",
}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        environment = patch.dict(os.environ, VALID_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_default_preview_and_existing_account_paths(self):
        settings = load_settings(self.base)
        self.assertFalse(settings.auto_send)
        self.assertEqual(settings.account_id, "account_1")
        self.assertEqual(settings.session_path, self.base / "account_1")
        self.assertEqual(settings.database_path, self.base / "activity_account_1.sqlite3")
        self.assertEqual(settings.groq_model, VALID_ENV["GROQ_MODEL"])
        self.assertEqual(settings.max_actions, 5)
        self.assertFalse(hasattr(settings, "min_gap"))

    def test_loading_does_not_rewrite_env_or_existing_data(self):
        original = (
            "# Existing settings and comments must survive\r\n"
            "API_ID=12345\r\nAPI_HASH=test-api-hash\r\n"
            "TARGET_CHAT_ID=-10012345\r\nGROQ_API_KEY=test-groq-key\r\n"
            "GROQ_MODEL=custom-model\r\nAUTO_SEND=false\r\nMIN_GAP_SECONDS=480\r\n"
        ).encode("utf-8")
        env_path = self.base / ".env"
        env_path.write_bytes(original)
        session = self.base / "account_1.session"
        database = self.base / "activity_account_1.sqlite3"
        session.write_bytes(b"existing fake session")
        database.write_bytes(b"existing fake database")
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(self.base)
        self.assertFalse(hasattr(settings, "min_gap"))
        self.assertEqual(settings.groq_model, "custom-model")
        self.assertFalse(settings.auto_send)
        self.assertEqual(env_path.read_bytes(), original)
        self.assertEqual(session.read_bytes(), b"existing fake session")
        self.assertEqual(database.read_bytes(), b"existing fake database")

    def test_process_environment_has_priority_without_file_changes(self):
        original = b"GROQ_MODEL=file-model\nAUTO_SEND=true\n"
        (self.base / ".env").write_bytes(original)
        with patch.dict(os.environ, {"AUTO_SEND": "false"}):
            settings = load_settings(self.base)
        self.assertEqual(settings.groq_model, "configured-test-model")
        self.assertFalse(settings.auto_send)
        self.assertEqual((self.base / ".env").read_bytes(), original)

    def test_explicit_existing_live_setting_is_preserved(self):
        for value in ("true", "TRUE", "1", "yes"):
            with self.subTest(value=value), patch.dict(os.environ, {"AUTO_SEND": value}):
                self.assertTrue(load_settings(self.base).auto_send)
        for value in ("false", "0", "no"):
            with self.subTest(value=value), patch.dict(os.environ, {"AUTO_SEND": value}):
                self.assertFalse(load_settings(self.base).auto_send)

    def test_limits_cannot_be_relaxed_below_hard_safety_bounds(self):
        with patch.dict(os.environ, {
            "MAX_ACTIONS_PER_HOUR": "999", "MIN_GAP_SECONDS": "1", "BATCH_SIZE": "999",
            "BATCH_DELAY_SECONDS": "0", "MAX_PENDING_AGE_SECONDS": "9999",
        }):
            settings = load_settings(self.base)
        self.assertEqual(settings.max_actions, 5)
        self.assertEqual((settings.batch_size, settings.batch_delay, settings.max_age), (50, 1, 720))

    def test_stricter_user_limits_are_honoured(self):
        with patch.dict(os.environ, {
            "MAX_ACTIONS_PER_HOUR": "2", "MIN_GAP_SECONDS": "1800", "BATCH_SIZE": "4",
            "BATCH_DELAY_SECONDS": "12", "MAX_PENDING_AGE_SECONDS": "180",
        }):
            settings = load_settings(self.base)
        self.assertEqual(settings.max_actions, 2)
        self.assertEqual((settings.batch_size, settings.batch_delay, settings.max_age), (4, 12, 180))

    def test_lower_and_upper_processing_bounds(self):
        with patch.dict(os.environ, {
            "MAX_ACTIONS_PER_HOUR": "-5", "BATCH_SIZE": "-10",
            "BATCH_DELAY_SECONDS": "1000", "MAX_PENDING_AGE_SECONDS": "0",
        }):
            settings = load_settings(self.base)
        self.assertEqual((settings.max_actions, settings.batch_size, settings.batch_delay, settings.max_age), (1, 1, 60, 60))

    def test_custom_account_gets_separate_paths(self):
        with patch.dict(os.environ, {"ACCOUNT_ID": "future_account"}):
            settings = load_settings(self.base)
        self.assertEqual(settings.session_path, self.base / "future_account")
        self.assertEqual(settings.database_path, self.base / "activity_future_account.sqlite3")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_explicit_relative_and_absolute_paths_are_preserved(self):
        database = self.base / "separate" / "history.sqlite3"
        with patch.dict(os.environ, {"SESSION_PATH": "sessions/existing", "DATABASE_PATH": str(database)}):
            settings = load_settings(self.base)
        self.assertEqual(settings.session_path, self.base / "sessions" / "existing")
        self.assertEqual(settings.database_path, database)

    def test_missing_required_values_fail_with_useful_names(self):
        for name in ("API_ID", "API_HASH", "TARGET_CHAT_ID", "GROQ_API_KEY"):
            with self.subTest(name=name), patch.dict(os.environ, {}, clear=False):
                del os.environ[name]
                with self.assertRaisesRegex(ValueError, name):
                    load_settings(self.base)
        with patch.dict(os.environ, {"GROQ_MODEL": ""}):
            with self.assertRaisesRegex(ValueError, "GROQ_MODEL"):
                load_settings(self.base)

    def test_invalid_integer_values_name_the_setting(self):
        for name in ("API_ID", "TARGET_CHAT_ID", "MAX_ACTIONS_PER_HOUR", "BATCH_DELAY_SECONDS", "BATCH_SIZE", "MAX_PENDING_AGE_SECONDS"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "not-a-number"}):
                with self.assertRaisesRegex(ValueError, name + " must be an integer"):
                    load_settings(self.base)

    def test_invalid_account_boolean_and_logging_values_fail_closed(self):
        for name, value in (("ACCOUNT_ID", "../account"), ("ACCOUNT_ID", ""), ("AUTO_SEND", "sometimes"), ("AUTO_SEND", ""), ("LOG_LEVEL", "TRACE")):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                with self.assertRaisesRegex(ValueError, name):
                    load_settings(self.base)


if __name__ == "__main__":
    unittest.main()
