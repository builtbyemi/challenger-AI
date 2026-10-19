# Challenger Ai — Telegram Challenger

This is the existing project, upgraded in place. `telegram_challenger_v07.py` remains a compatible entry point. Account 1's session and SQLite history are retained. The folder is now `C:\Users\EMMANUEL\Desktop\Challenger Ai`.

The worker supports five separately authenticated accounts, optional relevant discussion starters, persistent manual/automated message budgets, and a private React dashboard. Five messages per account is a ceiling, never a posting target. No generated filler is used to reach a quota.

Supabase uploads failing with `Telemetry upload deferred`: see [the telemetry repair guide](TELEMETRY_FIX.md) for the verified causes, administrator SQL repair and `python main.py --telemetry-check`.

## Windows setup

Startup reporting `Stopped (RuntimeError)`, or a second worker refusing to start: see [STARTUP_FIX.md](STARTUP_FIX.md). Preview is an explicit runtime override; `--check-config --preview` displays its effective settings. Stop the existing worker with Ctrl+C before launching another instance.

Use Python 3.11+ and Node.js 24 LTS. Run these commands in PowerShell; activation is unnecessary, including after the folder rename:

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai'
# Only if the virtual environment is absent:
if (-not (Test-Path '.\.venv\Scripts\python.exe')) { py -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -X utf8 main.py --check-config
```

Keep the existing `.env`. Do not copy `.env.example` over it. The example is a reference for additional settings. The eight `ACCOUNT_2_API_ID` / `ACCOUNT_2_API_HASH` through Account 5 placeholders have been added without clearing existing credentials. Blank optional API credentials inherit shared `API_ID` / `API_HASH`; each Telegram user still requires its own phone login. Never paste login codes or passwords into source files.

Authenticate Accounts 2–5 sequentially with this single PowerShell command from the project folder. Login forces `AUTO_SEND=false`, works with disabled account slots, and never starts group workers or Groq:

```powershell
& .\.venv\Scripts\python.exe -X utf8 -u main.py --login-all
```

Each account shows connection progress, then asks for its phone number (including country code), Telegram verification code, and 2FA password if required. Phone and code input are visible; the password is hidden and its prompt says so. Existing authorized sessions are reused without prompting. Account 1 is not included in `--login-all`. To authenticate one account, use `python main.py --login account_2` (likewise Accounts 3–5).

Network steps time out after 30 seconds; disconnect cleanup is limited to 5 seconds. Typing has no deadline; Ctrl+C cancels safely. Invalid codes/passwords allow at most three attempts. Expired codes and Telegram flood limits stop login with an explanation instead of retrying aggressively. The sequence stops on failure; retry the individual account, or rerun `--login-all` to reuse successful sessions and continue. Use an interactive PowerShell terminal, not an IDE Output pane or a background task.

Login does not edit `.env`, credentials, account enable flags, or Account 1's session/history. Keep `AUTO_SEND=false` in `.env`. After authentication, set `ACCOUNT_2_ENABLED=true` through `ACCOUNT_5_ENABLED=true` only when ready to run those accounts. The normal worker reports missing logins and skips those accounts while others continue.

```powershell
# Preview connects to Telegram and Groq, but never sends group messages.
& .\.venv\Scripts\python.exe -X utf8 main.py --preview
# Stop with Ctrl+C before changing mode or starting another instance.

# Read-only local diagnostics, usable in another terminal:
& .\.venv\Scripts\python.exe -X utf8 main.py --status
& .\.venv\Scripts\python.exe -X utf8 main.py --health
```

For an explicitly authorized live run, after checking group permission and disclosing each account's automation:

```powershell
$env:GROUP_AUTOMATION_ALLOWED = 'true'
1..5 | ForEach-Object { Set-Item -Path "Env:ACCOUNT_$($_)_AUTOMATION_DISCLOSED" -Value 'true' }
$env:AUTO_SEND = 'true'
New-Item -ItemType Directory -Force .\logs | Out-Null
& .\.venv\Scripts\python.exe -X utf8 -u main.py 2>&1 | Tee-Object -FilePath .\logs\worker.log -Append
# Separate terminal:
Get-Content .\logs\worker.log -Tail 50 -Wait
```

`--preview` always overrides live configuration. Templates default to `AUTO_SEND=false`. Tests use fake Telegram transports, never a live send. PowerShell environment values override `.env` and affect only that shell. Stop old workers before relaunching.

## Scheduling, manual messages and persistence

- Each account has its own session, SQLite history and strict cap of five counted messages. The combined group ceiling defaults to 25 and can be made stricter with `GROUP_MAX_ACTIONS_PER_HOUR`. There is no ten-minute minimum gap.
- `HOURLY_WINDOW_MODE=fixed` is the default and the configured mode. Account counters (maximum 5) and group counters (maximum 25) reset together at HH:00 UTC, even when allowance remains unused. `REPORT_TIMEZONE` only controls daily reporting; it does not move hourly reset boundaries. History is retained and counts are derived from persisted timestamps. Mentions never bypass the limits. Legacy `rolling` remains available only when explicitly configured.
- Dashboard account cards show confirmed sends out of 5 and a UTC reset countdown. On rollover, old counts are cleared and marked as updating until a fresh snapshot arrives; the dashboard does not invent new-hour availability from stale data. Preview/uncertain reservations still consume allowance independently of confirmed sends. No posting is forced to fill the quota.
- All internal counter constructors now default to fixed UTC hours as well. At 01:18 UTC, records before 01:00 UTC are excluded without clearing history. The dashboard requires matching current-hour snapshot timestamps and bounds; old rolling payloads are marked as awaiting a current UTC snapshot. Restart an older worker to load the updated counting code and refresh the dashboard.
- Outgoing updates from phones, Desktop and other sessions count as manual activity. Recent outgoing history is reconciled at startup, after reconnects, during periodic history syncs and before sends. Today's history and the preceding rolling-hour tail are included. History scanning is bounded at 300 messages and fails closed on overflow.
- `remaining = max(0, 5 - manual - bot)` for confirmed sends. Preview and pending/uncertain reservations also consume allowance conservatively. The dashboard displays those separately; previews never appear as actual messages or points.
- Returned Telegram IDs merge echoed outgoing updates without double-counting. Exact text/target matches help reconcile an echo arriving before the send returns. Ambiguous timeouts retain a reservation; explicit Telegram rejections refund it and persist FloodWait. A simultaneous identical manual message can remain ambiguous until the send result is known.
- SQLite transactions and OS session locks guard concurrent workers. Shared claims prevent several owned accounts answering the same target or responding recursively to one another. Manual activity in other groups is outside this configured competition group's accounting.

The application cannot stop a human sending a sixth message through another Telegram client, or atomically lock that client's send while making its own network request. It uses every observed update and final history check to prevent automated oversending; cross-device events arriving after the final check are a Telegram/network race, not an enforceable global lock. Keep the system clock synchronized.

Account 1's legacy database stays in use. If its root session needs migration, the runtime copies it with SQLite backup into `sessions/account_1.session`, retaining the original. Existing dedicated sessions are never overwritten. Do not run original and copied sessions at the same time. Preserve `data/coordinator.sqlite3` and every account DB/session when moving the folder or hosting the worker elsewhere. Existing legacy tables remain intact.

## Participation and personalities

The queue, deterministic relevance filters, Groq selection, output validation and final target refresh remain in place. The default decision is IGNORE. Open discussions and clearly shared reply threads can be considered without a mention. Private exchanges, generic filler, stale/already-answered questions, bot accusations not addressed to this account, promotions and repetitive wording are filtered.

Accounts use distinct style preferences: relaxed Nigerian Pidgin; friendly English; playful wit; calm observation; energetic light slang. Replies must contain 3–5 words, preserve useful commas/apostrophes, and match the selected message's language. A style preference cannot justify invented facts, personal experiences or human identity claims. Direct automation questions receive a truthful short disclosure. Naturalness, dialect and relevance remain heuristic and cannot be guaranteed perfectly.

Plain text welcome announcements may receive an occasional short welcome. They are skipped when another member already greeted the arrival or a welcome appears among the last five recorded replies. Native join service events without a text welcome are not supported.

`START_DISCUSSION` is optional and uses curated, context-matched topics without extra Groq requests. Set semicolon-separated 3–5 word questions appropriate to the group, for example:

```dotenv
QUIET_PERIOD_SECONDS=900
DISCUSSION_TOPICS=What makes transfers reliable?;Which fees affect transfers?
```

A topic must overlap recent human conversation. The group must be quiet, allowance must remain, the topic must not repeat recent contributions, and an unanswered previous starter cannot trigger another. A shared quiet-episode claim prevents all accounts starting at once. Empty `DISCUSSION_TOPICS` disables starters. Silence or unused allowance alone never creates activity.

## Groq budgets

One shared client uses the existing `GROQ_API_KEY` and `GROQ_MODEL` (example: `openai/gpt-oss-20b`). `GROQ_CONCURRENCY` is bounded to 1–3. Configure request/token limits from your Groq account's current limits:

```dotenv
GROQ_REQUESTS_PER_MINUTE=20
GROQ_REQUESTS_PER_DAY=1000
GROQ_TOKENS_PER_MINUTE=10000
GROQ_TOKENS_PER_DAY=200000
```

These defaults are local budgets, not a claim about your provider plan. Admission is persisted across restarts, including retries. Token reservations use a conservative input-size bound plus maximum completion tokens; provider-reported totals replace estimates when available. Missing usage stays reserved and is marked unreported. The day budget is a conservative rolling 24 hours, while dashboard daily totals use the reporting timezone.

Selection and reply generation are intentionally combined in one `selection_generation` request, rather than doubling API calls. The ledger records that purpose explicitly. Repeated identical recent analyses are cached for 30 seconds, input snapshots are bounded, 429 backoff is shared, and malformed structured output uses validated compatibility fallbacks. A budget too small for one request prevents that request rather than overspending.

## Private web dashboard and Supabase

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full Supabase setup, owner/worker authorization, Cloudflare deployment and smoke-test checklist. The worker and frontend deploy independently. No Telegram/Groq secret, session, group message text, phone number, sender identity or Telegram message ID is published. Message Activity contains redacted delivery metadata. Settings is read-only; the website cannot send Telegram messages or change worker credentials.

The worker publishes a fresh snapshot every five seconds with a one-row durable SQLite outbox. New snapshots replace old pending snapshots, keeping retries bounded and current totals intact. Upload failures do not block Telegram. Supabase Realtime updates the browser with a 15-second polling fallback. Snapshots older than 20 seconds are stale and older than 90 seconds are offline. A fresh fetch of an old snapshot does not mark the worker live.

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai\dashboard'
npm ci
if (-not (Test-Path .env.local)) { Copy-Item .env.example .env.local }
# Fill the two public Supabase values in .env.local.
npm run dev
# Open http://127.0.0.1:5173
```

Without Supabase configuration, an explicitly labeled empty layout is available. It contains no fabricated activity. With Supabase, sign in using the dashboard owner account, not the worker publisher account.

## Points and analytics

Confirmed outgoing messages, previews, uncertain reservations and awarded points are different data. Potential points are labeled as an unverified estimate of one per confirmed message. Theoretical maxima are 5/hour and 120/day per account, 25/hour and 600/day combined; neither sending nor awards are guaranteed. No contest URL/scoring API was supplied, so no scoring window or award was independently verified.

If you obtain an organizer's verified award export, import it explicitly into existing account state:

```powershell
& .\.venv\Scripts\python.exe main.py --import-points .\points-private.json --account account_1
```

File format: `[{"reference":"organizer-export-row-17","awarded_at":1792328400,"points":1}]`. `awarded_at` is a nonfuture Unix timestamp. References are unique per account and imports are idempotent. The command trusts your verification; it does not independently validate the contest or expose evidence references to the dashboard. Without an award import, verified points remain unknown.

## Tests and health

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai'
& .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
Set-Location .\dashboard
npm test
npm run build
npx playwright test
npm audit
```

The browser test runs synthetic five-account workers through real SQLite and the telemetry publisher, executes the publisher RPC and owner read against embedded PostgreSQL, then renders the resulting data in Microsoft Edge on Windows. Telegram, Groq decisions and Supabase HTTP/Auth are mocked; it sends nothing to Telegram. For non-Windows machines, install Chromium using `npx playwright install chromium`. Database policy tests execute the actual migration in embedded PostgreSQL (PGlite), with a minimal Supabase auth-role fixture. They exercise owner isolation, publisher authorization, anonymous denial and timestamp validation. Hosted Supabase Auth/Realtime and Cloudflare deployment still require configured project access and a separate deployment smoke test.

For Windows Task Scheduler, use one task with program `C:\Users\EMMANUEL\Desktop\Challenger Ai\.venv\Scripts\python.exe`, arguments `-X utf8 -u "C:\Users\EMMANUEL\Desktop\Challenger Ai\main.py"`, and Start in `C:\Users\EMMANUEL\Desktop\Challenger Ai`. Choose "Do not start a new instance". Authenticate first; background tasks cannot answer login prompts. Ctrl+C or SIGTERM closes clients and leaves persistent reservations intact.

## Changed areas

`challenger/config.py`, `storage.py`, `coordinator.py`, `engine.py`, `telegram_app.py`, `groq_client.py`, `selection.py`, `application.py` and `main.py` extend the existing worker. New modules are `windows.py`, `discussions.py`, `usage.py`, `reporting.py`, `diagnostics.py` and `telemetry.py`. `dashboard/` contains React/Vite/Tailwind, tests and deployment headers; `supabase/migrations/` contains the database policy and publisher RPC. Templates, requirements, tests and documentation are updated. Existing authentication and session-preservation code is retained.
