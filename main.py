"""Telegram Challenger CLI: checks are offline; login is always explicit."""
import argparse
import asyncio
import logging
from pathlib import Path
import signal

from challenger.config import load_application_settings


async def run_with_shutdown(operation):
    """SIGTERM from a service manager follows the same cleanup as Ctrl+C."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(operation)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(task.cancel))
        try:
            await task
        except asyncio.CancelledError:
            return
    finally:
        signal.signal(signal.SIGTERM, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram Challenger: up to five independent accounts")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--check-config", action="store_true",
                          help="Validate configuration offline without changing files")
    commands.add_argument("--login", metavar="ACCOUNT_ID",
                          help="Explicit interactive login for one enabled account; never sends group messages")
    args = parser.parse_args()
    try:
        app = load_application_settings(Path(__file__).resolve().parent)
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2
    if args.check_config:
        print(f"Configuration OK | enabled={len(app.accounts)}/5 | model={app.accounts[0].groq_model} "
              f"| group cap={app.group_max_actions}/rolling-hour | no fixed gap")
        for account in app.accounts:
            print(f"  {account.account_id}: AUTO_SEND={account.auto_send} "
                  f"| cap={account.max_actions}/rolling-hour")
        return 0
    logging.basicConfig(level=app.accounts[0].log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    from challenger.application import login_account, run_application
    try:
        operation = login_account(app, args.login) if args.login else run_application(app)
        asyncio.run(run_with_shutdown(operation))
    except KeyboardInterrupt:
        print("Stopped")
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
