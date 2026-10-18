# Preview startup repair — 2026-10-19

## Confirmed cause

The coordinator OS lock was already held while an existing `main.py --preview` process was running. At diagnosis, Windows showed launcher PID 14468 and child Python PID 4596, both started at 00:00:40 Lagos time. These are the launcher/child pair of the existing application, not two account identities.

Attempting the same coordinator lock reproduced the RuntimeError. The old CLI discarded its message, and the shared lock helper incorrectly called a coordinator conflict a Telegram session conflict. This was not caused by `AUTO_SEND=true` or live-readiness checks: all five accounts passed readiness validation after the preview override.

The singleton protection is necessary: two processes must not operate the same sessions and shared quotas. A leftover lock file alone is harmless; the OS releases the lock on process exit. Do not delete the lock file or recreate any database/session.

## Changes

- `main.py`: apply preview settings before all command branches, including `--check-config`; report sanitized startup stages, exception categories and source locations.
- `challenger/startup.py`: typed lock-conflict diagnostics and stage tracking without printing exception bodies, source lines, locals or private file paths.
- `challenger/application.py`: report each account's effective mode before startup; identify validation, lock, database, ledger, diagnostics, Groq and supervision stages. Close partially initialized resources when startup fails. Live group-permission/disclosure checks remain required.
- `challenger/telegram_app.py`: distinguish coordinator/session lock conflicts; refuse every send at the Telegram adapter when `auto_send=False`. The existing engine also simulates replies/starters without calling the adapter.
- `tests/test_startup_preview.py`: regression coverage for five-account CLI startup with live settings, inherited environment precedence, effective preview configuration, unchanged live gates, lock conflicts, sanitized failures, partial cleanup and direct adapter/repeated-send blocking.
- `tests/test_telegram_app.py`: existing mocked live-send tests now explicitly select live mode.

Root `.env`, all five identities, sessions, quota/history databases, group-wide limits, Groq configuration, Supabase and dashboard settings were not changed. The existing preview process was not stopped automatically.

## Minimal manual correction

In the terminal already running Challenger, press **Ctrl+C** and wait for it to exit. Stop a Task Scheduler/service-managed worker through its manager if that is how it was launched. Run only one worker process against these sessions/databases.

No configuration edits are required. `AUTO_SEND=true` may remain the normal configuration; explicit `--preview` overrides it in memory.

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai'
# Offline: all five accounts must show AUTO_SEND=False.
& .\.venv\Scripts\python.exe -X utf8 main.py --check-config --preview
# After the old worker has exited:
& .\.venv\Scripts\python.exe -X utf8 main.py --preview
```

Startup prints `STARTUP account=account_N mode=PREVIEW` for all five accounts. A later `Ready ... mode=PREVIEW` means that account connected and initialized; an initial STARTUP line alone does not claim a successful connection. If another worker still holds the lock, startup now reports `stage=coordinator_lock` with instructions instead of hiding the reason.

Run the offline regression suite:

Result on 2026-10-19: **270 tests run, 269 passed, one skipped** (POSIX permissions do not apply on Windows). No failures. Existing preview quota, discussion-starter, restart and live-send tests remain passing.

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

The diagnostic run exercised the real configuration and real held OS lock, with a guard preventing any database or network initialization if the lock unexpectedly became available. Five-account startup was exercised with fake Telegram/Groq dependencies and temporary SQLite. No real Telegram sends were performed. A new live Telegram connection cycle was deliberately not started alongside the existing worker.
