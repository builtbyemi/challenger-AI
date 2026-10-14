"""Independent account supervision around one shared coordinator and Groq client."""
import asyncio
import logging
import time

from .auth import AuthenticationRequired, authenticate, prepare_account
from .config import ApplicationSettings, Settings
from .coordinator import Coordinator
from .engine import TelegramBackoff
from .groq_client import GroqConfigurationError, SharedGroqDecider
from .storage import Store
from .telegram_app import instance_lock, run as run_account

log = logging.getLogger(__name__)


def validate_live_readiness(settings: ApplicationSettings) -> None:
    live = [account for account in settings.accounts if account.auto_send]
    if live and not settings.group_automation_allowed:
        raise ValueError("Live mode requires GROUP_AUTOMATION_ALLOWED=true after checking the group's rules")
    for account in live:
        if not account.automation_disclosed:
            raise ValueError(f"Live mode requires automation disclosure confirmation for {account.account_id}")


def import_history(account: Settings, coordinator: Coordinator) -> float:
    prepare_account(account)
    store = Store(account.database_path, account.account_id, account.chat_id,
                  max_actions=account.max_actions, max_age=account.max_age)
    try:
        coordinator.import_activity(account.account_id, account.chat_id, store.export_actions(time.time()))
        return store.blocked_until("telegram")
    finally:
        store.close()


def save_telegram_wait(account: Settings, seconds: float) -> None:
    store = Store(account.database_path, account.account_id, account.chat_id)
    try:
        store.set_block("telegram", time.time() + seconds)
    finally:
        store.close()


async def supervise(account: Settings, app: ApplicationSettings, coordinator: Coordinator,
                    decider, *, identity_known: bool) -> None:
    """A failed connection affects only its account. Cancellation stops promptly."""
    delay = app.restart_delay
    while True:
        started = time.monotonic()
        try:
            until = import_history(account, coordinator)
            if until > time.time():
                await asyncio.sleep(until - time.time())
            if not identity_known:
                identity = await asyncio.wait_for(authenticate(account, interactive=False), timeout=45)
                coordinator.register(account.account_id, identity.user_id)
                identity_known = True
            await run_account(account, shared_decider=decider, coordinator=coordinator,
                              require_authorized=True)
            log.warning("Account disconnected: %s; reconnecting in %ss", account.account_id, delay)
        except AuthenticationRequired:
            log.error("Account %s requires explicit login: python main.py --login %s",
                      account.account_id, account.account_id)
            return
        except GroqConfigurationError:
            log.error("Groq configuration unavailable; stopped account %s", account.account_id)
            return
        except ValueError:
            log.error("Account %s has incompatible identity or group configuration; stopped", account.account_id)
            return
        except TelegramBackoff as exc:
            save_telegram_wait(account, exc.seconds)
            delay = max(delay, exc.seconds)
            log.warning("Account %s deferred by Telegram for %.0fs", account.account_id, exc.seconds)
        except Exception as exc:
            log.warning("Account %s failed (%s); reconnect deferred", account.account_id, type(exc).__name__)
        if time.monotonic() - started >= 300:
            delay = app.restart_delay
        await asyncio.sleep(delay)
        delay = min(300, max(app.restart_delay, delay * 2))


async def run_application(app: ApplicationSettings) -> None:
    validate_live_readiness(app)
    app.coordinator_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # All launch commands sharing this state must use one coordinator process.
    # Account locks additionally protect individual Telegram session files.
    with instance_lock(app.coordinator_path.with_suffix(".coordinator.lock")):
        coordinator = Coordinator(app.coordinator_path, app.group_max_actions)
        decider = None
        tasks = []
        try:
            known = set()
            eligible = []
            # Sequential, noninteractive identity discovery completes before any
            # worker runs, so configured peers cannot initially respond to peers.
            for account in app.accounts:
                try:
                    until = import_history(account, coordinator)
                    if until > time.time():
                        raise TelegramBackoff(until - time.time())
                    identity = await asyncio.wait_for(authenticate(account, interactive=False), timeout=45)
                    coordinator.register(account.account_id, identity.user_id)
                    known.add(account.account_id)
                except AuthenticationRequired:
                    log.warning("Account %s is not authenticated; use --login %s explicitly",
                                account.account_id, account.account_id)
                    continue
                except ValueError:
                    # Identity aliasing can bypass individual quotas; fail the
                    # complete configuration before starting any account worker.
                    raise ValueError("Account identities conflict; each slot needs a distinct Telegram user") from None
                except TelegramBackoff as exc:
                    save_telegram_wait(account, exc.seconds)
                    log.warning("Account %s has a persistent Telegram wait", account.account_id)
                except Exception as exc:
                    log.warning("Account %s unavailable during discovery (%s); independent retry scheduled",
                                account.account_id, type(exc).__name__)
                eligible.append(account)
            if not eligible:
                log.warning("No authenticated accounts available; no workers started")
                return
            first = app.accounts[0]
            decider = SharedGroqDecider(first.groq_api_key, first.groq_model, coordinator,
                                       concurrency=app.groq_concurrency)
            tasks = [asyncio.create_task(supervise(account, app, coordinator, decider,
                                                  identity_known=account.account_id in known),
                                         name=f"challenger-{account.account_id}") for account in eligible]
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if decider is not None:
                    await decider.aclose()
            finally:
                coordinator.close()


async def login_account(app: ApplicationSettings, account_id: str) -> None:
    account = next((account for account in app.accounts if account.account_id == account_id), None)
    if account is None:
        raise ValueError("Enable the requested account slot in .env before explicitly logging it in")
    # This command authenticates only. It never starts group workers or Groq.
    identity = await authenticate(account, interactive=True)
    app.coordinator_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    coordinator = Coordinator(app.coordinator_path, app.group_max_actions)
    try:
        coordinator.register(account.account_id, identity.user_id)
    finally:
        coordinator.close()
    log.info("Authenticated %s; no group worker started", account.account_id)
