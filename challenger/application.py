"""Independent account supervision around one shared coordinator and Groq client."""
import asyncio
import logging
import time
import os
from dataclasses import replace
from contextlib import ExitStack

from .auth import AuthenticationRequired, LoginError, authenticate, prepare_account
from .config import ApplicationSettings, Settings
from .coordinator import Coordinator
from .engine import TelegramBackoff
from .groq_client import GroqConfigurationError, SharedGroqDecider
from .storage import Store
from .telegram_app import instance_lock, run as run_account
from .usage import UsageLedger
from .diagnostics import DiagnosticHandler
from .startup import startup_stage, failure_summary

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
                  max_actions=account.max_actions, max_age=account.max_age,
                  window_mode=account.window_mode, timezone=account.timezone)
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
            log.warning("Account %s failed; %s; reconnect deferred", account.account_id,
                        failure_summary(exc, "account_worker"))
        if time.monotonic() - started >= 300:
            delay = app.restart_delay
        await asyncio.sleep(delay)
        delay = min(300, max(app.restart_delay, delay * 2))


async def run_application(app: ApplicationSettings) -> None:
    for account in app.accounts:
        log.info("STARTUP account=%s mode=%s cap=%s/%s-hour", account.account_id,
                 "LIVE" if account.auto_send else "PREVIEW", account.max_actions, account.window_mode)
    with startup_stage("live_validation"):
        validate_live_readiness(app)
    with startup_stage("runtime_directory"):
        app.coordinator_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # All launch commands sharing this state must use one coordinator process.
    # Account locks additionally protect individual Telegram session files.
    with ExitStack() as resources:
        with startup_stage("coordinator_lock"):
            resources.enter_context(instance_lock(app.coordinator_path.with_suffix(".coordinator.lock")))
        with startup_stage("coordinator_database"):
            coordinator = Coordinator(app.coordinator_path, app.group_max_actions,
                                      app.accounts[0].window_mode, app.accounts[0].timezone)
            resources.callback(coordinator.close)
        with startup_stage("usage_ledger"):
            ledger = UsageLedger(app.coordinator_path, app.groq_rpm, app.groq_rpd, app.groq_tpm, app.groq_tpd)
            resources.callback(ledger.close)
        with startup_stage("diagnostics_database"):
            diagnostics = DiagnosticHandler(app.coordinator_path)
            resources.callback(diagnostics.close)
            logging.getLogger().addHandler(diagnostics)
            resources.callback(logging.getLogger().removeHandler, diagnostics)
        telemetry_task = None
        decider = None
        tasks = []
        try:
            if os.getenv("TELEMETRY_ENABLED", "false").lower() == "true":
                from .telemetry import Publisher
                try:
                    telemetry_task = asyncio.create_task(Publisher(app).run())
                except ValueError:
                    log.warning("Telemetry configuration incomplete; local worker continues")
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
                    log.warning("Account %s unavailable during discovery; %s; independent retry scheduled",
                                account.account_id, failure_summary(exc, "identity_discovery"))
                eligible.append(account)
            if not eligible:
                log.warning("No authenticated accounts available; no workers started")
                return
            first = app.accounts[0]
            with startup_stage("groq_initialization"):
                decider = SharedGroqDecider(first.groq_api_key, first.groq_model, coordinator,
                                           concurrency=app.groq_concurrency, ledger=ledger)
            with startup_stage("account_supervision"):
                tasks = [asyncio.create_task(supervise(account, app, coordinator, decider,
                                                      identity_known=account.account_id in known),
                                             name=f"challenger-{account.account_id}") for account in eligible]
                await asyncio.gather(*tasks)
        finally:
            if telemetry_task is not None:
                telemetry_task.cancel()
                await asyncio.gather(telemetry_task, return_exceptions=True)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if decider is not None:
                await decider.aclose()


async def login_account(app: ApplicationSettings, account_id: str) -> None:
    account = next((account for account in app.accounts if account.account_id == account_id), None)
    if account is None:
        raise ValueError("Enable the requested account slot in .env before explicitly logging it in")
    # This command authenticates only. It never starts group workers or Groq.
    identity = await authenticate(replace(account, auto_send=False), interactive=True)
    app.coordinator_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    coordinator = Coordinator(app.coordinator_path, app.group_max_actions)
    try:
        coordinator.register(account.account_id, identity.user_id)
    finally:
        coordinator.close()
    print(f"[{account.account_id}] Authentication successful; session saved. No group worker started.", flush=True)


async def login_accounts(app: ApplicationSettings, account_ids: tuple[str, ...]) -> None:
    """Authenticate in order; stop on failure so no further codes are requested."""
    for account_id in account_ids:
        try:
            await login_account(app, account_id)
        except LoginError as exc:
            raise LoginError(f"{account_id}: {exc}") from None
