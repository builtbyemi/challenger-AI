"""Explicit account authentication and non-destructive session preparation.

Importing this module never opens a session or contacts Telegram. Login prompts
are used only when the caller explicitly requests ``interactive=True``.
"""

import getpass
import os
from pathlib import Path
import sqlite3
import tempfile

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from .config import Settings
from .engine import TelegramBackoff
from .models import Identity
from .telegram_app import instance_lock


class AuthenticationRequired(Exception):
    def __init__(self, account_id: str):
        self.account_id = account_id
        super().__init__(f"Account {account_id} requires authentication; run --login {account_id}")


def session_file(path: Path | str) -> Path:
    """Mirror Telethon's literal, case-sensitive .session suffix handling."""
    path = Path(path)
    return path if str(path).endswith(".session") else Path(f"{path}.session")


def _private_file(path: Path) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(0o600)


def _make_parents(path: Path) -> None:
    """Restrict directories we create without changing an existing shared parent."""
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        if parent == parent.parent:
            break
        parent = parent.parent
    for parent in reversed(missing):
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            # Another account can create a common parent concurrently.
            continue
        if os.name == "posix":
            parent.chmod(0o700)


def _create_private(path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    os.close(descriptor)
    return True


def _copy_legacy(source: Path, destination: Path) -> None:
    # Reserve the new destination exclusively. In particular, never overwrite a
    # session that already contains a different account's authorization.
    if not _create_private(destination):
        return
    try:
        original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            copied = sqlite3.connect(str(destination))
            try:
                original.backup(copied)
            finally:
                copied.close()
        finally:
            original.close()
    except BaseException:
        # Only this newly-created destination is removed on a failed copy.
        destination.unlink(missing_ok=True)
        raise


def prepare_account(settings: Settings) -> None:
    """Create private runtime paths and copy an existing legacy authorization.

    Existing session/database contents and the legacy source are preserved. The
    destination's session lock prevents readers observing a partially copied DB.
    This function must be called before taking the account's lifetime lock.
    """
    destination = session_file(settings.session_path)
    database = Path(settings.database_path)
    _make_parents(destination)
    _make_parents(database)
    lock = destination.with_suffix(".challenger.lock")
    with instance_lock(lock):
        _private_file(lock)
        legacy = getattr(settings, "legacy_session_path", None)
        if not destination.exists() and legacy is not None and session_file(legacy).is_file():
            _copy_legacy(session_file(legacy), destination)
        else:
            _create_private(destination)
        _create_private(database)
        _private_file(destination)
        _private_file(database)


async def authenticate(settings: Settings, interactive: bool = False) -> Identity:
    prepare_account(settings)
    destination = session_file(settings.session_path)
    with instance_lock(destination.with_suffix(".challenger.lock")):
        client = None
        probe = None
        try:
            session = str(settings.session_path)
            if not interactive:
                # Identity probing must not advance the worker's persisted update
                # state and discard its offline backlog before handlers exist.
                probe = tempfile.TemporaryDirectory(prefix="challenger-auth-")
                probe_file = Path(probe.name) / "probe.session"
                _copy_legacy(destination, probe_file)
                session = str(probe_file)
            client = TelegramClient(session, settings.api_id, settings.api_hash,
                                    request_retries=0, flood_sleep_threshold=0,
                                    raise_last_call_error=True, catch_up=False)
            if interactive:
                await client.start(
                    phone=lambda: getpass.getpass(f"Phone for {settings.account_id}: "),
                    code_callback=lambda: getpass.getpass(f"Login code for {settings.account_id}: "),
                    password=lambda: getpass.getpass(f"2FA password for {settings.account_id}: "),
                )
            else:
                await client.connect()
                if not await client.is_user_authorized():
                    raise AuthenticationRequired(settings.account_id)
            me = await client.get_me()
            if me is None:
                raise AuthenticationRequired(settings.account_id)
            return Identity(me.id, me.username or "")
        except FloodWaitError as exc:
            raise TelegramBackoff(exc.seconds) from None
        finally:
            try:
                if client is not None:
                    await client.disconnect()
            finally:
                try:
                    if probe is not None:
                        probe.cleanup()
                finally:
                    _private_file(destination)
                    _private_file(Path(settings.database_path))
