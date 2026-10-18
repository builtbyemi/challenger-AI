"""Telegram Challenger CLI: checks are offline; login is always explicit."""
import argparse
import asyncio
import logging
from pathlib import Path
import signal
from dataclasses import replace

from challenger.config import load_application_settings


async def run_with_shutdown(operation, *, propagate_cancel=False):
    """SIGTERM from a service manager follows the same cleanup as Ctrl+C."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(operation)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(task.cancel))
        try:
            await task
        except asyncio.CancelledError:
            if propagate_cancel:
                raise
            return
    finally:
        signal.signal(signal.SIGTERM, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram Challenger: up to five independent accounts")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--check-config", action="store_true",
                          help="Validate configuration offline without changing files")
    commands.add_argument("--login", metavar="ACCOUNT_ID",
                          help="Interactive login for one account, including disabled slots; never sends group messages")
    commands.add_argument("--login-all", action="store_true",
                          help="Authenticate Accounts 2, 3, 4 and 5 sequentially; never starts group workers")
    commands.add_argument("--telemetry-check", action="store_true",
                          help="Publish one fresh dashboard snapshot without starting Telegram or Groq")
    commands.add_argument("--status", action="store_true", help="Read-only redacted account dashboard")
    commands.add_argument("--health", action="store_true", help="Offline health check; nonzero for stale enabled accounts")
    commands.add_argument("--import-points", metavar="JSON_FILE", help="Import operator-verified awards with evidence references")
    parser.add_argument("--account", default="account_1", help="Account for points import")
    parser.add_argument("--preview", action="store_true", help="Force preview, overriding AUTO_SEND")
    args = parser.parse_args()
    if args.telemetry_check:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    login_ids = (tuple(f"account_{slot}" for slot in range(2, 6)) if args.login_all
                 else (args.login,) if args.login else ())
    try:
        app = load_application_settings(Path(__file__).resolve().parent, login_accounts=login_ids)
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2
    if args.check_config:
        print(f"Configuration OK | enabled={len(app.accounts)}/5 | model={app.accounts[0].groq_model} "
              f"| group cap={app.group_max_actions}/{app.accounts[0].window_mode}-hour | no fixed gap")
        for account in app.accounts:
            print(f"  {account.account_id}: AUTO_SEND={account.auto_send} "
                  f"| cap={account.max_actions}/{account.window_mode}-hour")
        return 0
    if args.preview or args.telemetry_check:
        app = replace(app, accounts=tuple(replace(a, auto_send=False) for a in app.accounts))
    if args.telemetry_check:
        from challenger.telemetry import check_telemetry
        try:
            return asyncio.run(check_telemetry(app))
        except KeyboardInterrupt:
            print("Telemetry check cancelled; no Telegram worker started")
            return 130
    if args.status or args.health:
        from challenger.reporting import snapshot, cli_status
        data = snapshot(app)
        cli_status(data)
        return 1 if args.health and any(a["enabled"] and a["health"] != "connected" for a in data["accounts"]) else 0
    if args.import_points:
        from challenger.reporting import import_verified_points
        account = next((a for a in app.accounts if a.account_id == args.account), None)
        if account is None:
            print("Enable the selected account before importing awards")
            return 2
        try:
            import_verified_points(account, args.import_points)
        except Exception as exc:
            print(f"Award import rejected ({type(exc).__name__}); check the documented format and existing account database")
            return 2
        print("Imported operator-verified awards; no points inferred from messages")
        return 0
    logging.basicConfig(level=app.accounts[0].log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    from challenger.application import login_accounts, run_application
    from challenger.auth import LoginError
    try:
        operation = login_accounts(app, login_ids) if login_ids else run_application(app)
        asyncio.run(run_with_shutdown(operation, propagate_cancel=bool(login_ids)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Cancelled; existing sessions retained")
        return 130
    except LoginError as exc:
        print(f"Authentication failed: {exc}", flush=True)
        return 1
    except ValueError as exc:
        logging.error("Configuration rejected: %s", exc)
        return 2
    except Exception as exc:
        # Provider exceptions can contain request details; expose only the type.
        logging.error("Stopped (%s); inspect configuration and connection status", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
