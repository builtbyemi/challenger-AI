"""Explicit account authentication and non-destructive session preparation.

Importing this module never opens a session or contacts Telegram. Login prompts
are used only when the caller explicitly requests ``interactive=True``.
"""

import asyncio
import getpass
import logging
import os
import re
from pathlib import Path
import sqlite3
import tempfile
import threading

from telethon import TelegramClient
from telethon.errors import (FloodWaitError, PhoneNumberInvalidError, PhoneCodeInvalidError,
                             PhoneCodeExpiredError, SessionPasswordNeededError,
                             PasswordHashInvalidError, RPCError)

from .config import Settings
from .engine import TelegramBackoff
from .models import Identity
from .telegram_app import instance_lock


class AuthenticationRequired(Exception):
    def __init__(self, account_id: str):
        self.account_id = account_id
        super().__init__(f"Account {account_id} requires authentication; run --login {account_id}")


class LoginError(Exception):
    """A safe, actionable login error containing no submitted credentials."""


LOGIN_TIMEOUT = 30
DISCONNECT_TIMEOUT = 5


async def _login_step(operation, stage: str):
    try:
        return await asyncio.wait_for(operation, timeout=LOGIN_TIMEOUT)
    except TimeoutError:
        raise LoginError(f"Timed out while {stage} after {LOGIN_TIMEOUT}s. Check your connection and retry login.") from None


def _prompt(account_id: str, label: str, *, secret: bool = False) -> str:
    # A flushed stdout prompt works in PowerShell even with buffered Python output.
    # Phone and code use normal console input; only the 2FA password is hidden.
    print(f"[{account_id}] {label}: ", end="", flush=True)
    try:
        return getpass.getpass("") if secret else input().strip()
    except EOFError:
        raise LoginError("No terminal input available. Run login in an interactive PowerShell terminal.") from None


async def _login_prompt(account_id: str, label: str, *, secret: bool = False) -> str:
    # Keep Telethon's event loop responsive while the user types. A daemon reader
    # also lets Ctrl+C exit without asyncio waiting for a blocked executor thread.
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def deliver(value, error):
        if not result.done():
            if error is None:
                result.set_result(value)
            else:
                result.set_exception(error)

    def read():
        value, error = None, None
        try:
            value = _prompt(account_id, label, secret=secret)
        except KeyboardInterrupt:
            error = LoginError("Login cancelled; existing sessions retained.")
        except Exception as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:
            pass  # The process was cancelled while console input was pending.

    threading.Thread(target=read, name="telegram-login-input", daemon=True).start()
    return await result


async def _interactive_login(client, account_id: str) -> None:
    print(f"[{account_id}] Connecting to Telegram (up to {LOGIN_TIMEOUT}s); AUTO_SEND=false", flush=True)
    await _login_step(client.connect(), "connecting to Telegram")
    if await _login_step(client.is_user_authorized(), "checking the existing session"):
        print(f"[{account_id}] Existing authorized session retained", flush=True)
        return
    phone = await _login_prompt(account_id, "Phone number with country code (e.g. +234...)")
    phone = re.sub(r"[\s()-]", "", phone)
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
        raise LoginError("Enter a phone number with + and country code, then retry login.")
    print(f"[{account_id}] Requesting a Telegram verification code...", flush=True)
    sent = await _login_step(client.send_code_request(phone), "requesting the verification code")
    print(f"[{account_id}] Check Telegram's delivery instructions/app for your code", flush=True)
    for attempt in range(3):
        code = await _login_prompt(account_id, "Telegram verification code")
        if not code:
            print("Code cannot be empty", flush=True)
            continue
        try:
            await _login_step(client.sign_in(phone=phone, code=code,
                                            phone_code_hash=sent.phone_code_hash), "verifying the code")
            return
        except PhoneCodeInvalidError:
            print(f"Incorrect verification code ({attempt + 1}/3)", flush=True)
        except PhoneCodeExpiredError:
            raise LoginError("Verification code expired. Run login again to request a new code.") from None
        except SessionPasswordNeededError:
            for password_attempt in range(3):
                password = await _login_prompt(account_id, "2FA password (input hidden)", secret=True)
                if not password:
                    print("Password cannot be empty", flush=True)
                    continue
                try:
                    await _login_step(client.sign_in(password=password), "verifying the 2FA password")
                    return
                except PasswordHashInvalidError:
                    print(f"Incorrect 2FA password ({password_attempt + 1}/3)", flush=True)
            raise LoginError("Three unsuccessful 2FA attempts. Login stopped.")
    raise LoginError("Three unsuccessful verification-code attempts. Login stopped.")


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
                        request_retries=2, flood_sleep_threshold=0,
                        raise_last_call_error=True, catch_up=False,
                        **({"connection_retries": 0, "timeout": 10} if interactive else {}))
            if interactive:
                await _interactive_login(client, settings.account_id)
            else:
                await client.connect()
                if not await client.is_user_authorized():
                    raise AuthenticationRequired(settings.account_id)
            me = (await _login_step(client.get_me(), "confirming the account")
                  if interactive else await client.get_me())
            if me is None:
                raise AuthenticationRequired(settings.account_id)
            return Identity(me.id, me.username or "")
        except FloodWaitError as exc:
            if interactive:
                raise LoginError(f"Telegram requires a wait of {exc.seconds}s. Login stopped; retry after that wait.") from None
            raise TelegramBackoff(exc.seconds) from None
        except PhoneNumberInvalidError:
            raise LoginError("Telegram rejected the phone number. Check its country code and digits.") from None
        except (RPCError, OSError) as exc:
            if interactive:
                raise LoginError(f"Telegram login failed ({type(exc).__name__}). Check credentials/connection and retry.") from None
            raise
        finally:
            try:
                if client is not None:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=DISCONNECT_TIMEOUT)
                    except (TimeoutError, OSError):
                        logging.getLogger(__name__).warning("Telegram disconnect did not finish cleanly; session retained")
            finally:
                try:
                    if probe is not None:
                        probe.cleanup()
                finally:
                    _private_file(destination)
                    _private_file(Path(settings.database_path))
