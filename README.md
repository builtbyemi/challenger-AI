# Telegram Challenger

A conservative Telethon participant using Groq and persistent SQLite state, with support for up to five independently authenticated Telegram accounts. Python 3.11 or later is required. Only the existing account is enabled by default. `AUTO_SEND=false` remains the default; adding credentials does not itself log in another account or send messages.

Your existing `.env`, credentials, target group and database history are preserved. **Do not copy `.env.example` over an existing `.env`.** The example documents settings to add selectively. No additional accounts were authenticated and no Telegram messages were sent as part of this refactor.

## Install, validate and preview in Windows PowerShell

Stop the old script before starting this version. Run:

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\telegram-challenger'
if (-not (Test-Path -LiteralPath '.\.venv\Scripts\python.exe')) {
    py -3 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:AUTO_SEND = 'false'
& .\.venv\Scripts\python.exe -X utf8 main.py --check-config
& .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe -X utf8 main.py
```

No environment activation or PowerShell execution-policy change is necessary. `--check-config` reads configuration without connecting to Telegram/Groq, creating a database or copying a session. Tests use temporary storage and mocked Telegram/Groq services. Press Ctrl+C to stop a foreground run.

The original entry point remains supported and starts the same application:

```powershell
& .\.venv\Scripts\python.exe -X utf8 telegram_challenger_v07.py --check-config
& .\.venv\Scripts\python.exe -X utf8 telegram_challenger_v07.py
```

Preview connects already authenticated accounts to Telegram and can send conversation context to Groq, but sends no group replies. A successful preview reserves the same persistent rolling-hour quota as a live send. There is no fixed inter-message cooldown. Restarting, switching modes or selecting another account cannot reset shared group limits. Silence is expected when no worthwhile messages are available.

## Authenticate an additional account

Each slot uses a different Telegram user session and activity database. API credentials identify a Telegram application; each interactive phone/code login determines the Telegram user. The application rejects duplicate connected Telegram identities and conflicting session/database paths.

1. Stop the running application. Keep `AUTO_SEND=false`.
2. Edit the existing `.env` locally. Preserve existing lines and secrets. Add `ACCOUNT_2_ENABLED=true`. If account 2 needs different API credentials or a different group, set `ACCOUNT_2_API_ID`, `ACCOUNT_2_API_HASH` and `ACCOUNT_2_TARGET_CHAT_ID` to its actual values. Otherwise omit those overrides to inherit `API_ID`, `API_HASH` and `TARGET_CHAT_ID`. An explicitly empty override is invalid.
3. Validate configuration, then explicitly authenticate that slot:

   ```powershell
   Set-Location 'C:\Users\EMMANUEL\Desktop\telegram-challenger'
   $env:AUTO_SEND = 'false'
   & .\.venv\Scripts\python.exe -X utf8 main.py --check-config
   & .\.venv\Scripts\python.exe -X utf8 main.py --login account_2
   ```

4. Enter that account's phone number, Telegram verification code and two-step verification password when requested. Code and password entry are hidden; they are not written to application logs. This command authenticates the account and exits without starting reply workers.
5. Start preview with `& .\.venv\Scripts\python.exe -X utf8 main.py`. Inspect selected target IDs and preview text. Other authorized accounts continue independently if one account disconnects or encounters a recoverable error.

Repeat with slots 3 through 5 only when you explicitly want to add those accounts. They remain disabled otherwise. For a new installation with no existing first-account session, use `main.py --login account_1`. Normal startup never opens an interactive login or silently creates an authenticated account. If a session expires, stop and explicitly log in again.

New sessions default to `sessions/account_N.session`; new histories default to `data/activity_account_N.sqlite3`. Optional `ACCOUNT_N_SESSION_PATH` and `ACCOUNT_N_DATABASE_PATH` override them. Avoid setting these unnecessarily: changing a database path loses access to that account's existing queue and activity ledger.

## Enable live operation deliberately

Live operation requires `AUTO_SEND=true`, `GROUP_AUTOMATION_ALLOWED=true`, and `ACCOUNT_N_AUTOMATION_DISCLOSED=true` for each enabled account. The permission flag attests that automation is allowed in the configured groups. The disclosure flag attests that you have identified each account as automated through its profile or the group's agreed disclosure method. These settings do not edit profiles or post notices automatically.

After arranging permission and disclosure, stop preview and explicitly opt in. For just the existing account:

```powershell
$env:GROUP_AUTOMATION_ALLOWED = 'true'
$env:ACCOUNT_1_AUTOMATION_DISCLOSED = 'true'
$env:AUTO_SEND = 'true'
& .\.venv\Scripts\python.exe -X utf8 main.py
```

If other slots are enabled, set their corresponding disclosure flags too. Stop the application and restore `$env:AUTO_SEND = 'false'` for preview. PowerShell environment changes apply only to that shell and its child processes; service and Task Scheduler runs read their own environment and the local `.env`. Keep `.env` in preview mode until you deliberately change the scheduled application's configuration.

## Configuration and architecture

| Setting | Behavior |
| --- | --- |
| `ACCOUNT_1_ENABLED` | Defaults to `true`; slots 2–5 default to `false` |
| `ACCOUNT_N_API_ID`, `ACCOUNT_N_API_HASH`, `ACCOUNT_N_TARGET_CHAT_ID` | Per-slot overrides; absent values inherit shared Telegram settings |
| `GROQ_API_KEY`, `GROQ_MODEL` | One shared Groq integration using the existing key/model |
| `GROQ_CONCURRENCY` | Defaults to 1; bounded to 1–3 simultaneous requests |
| `MAX_ACTIONS_PER_HOUR` | Per-account ceiling; at most 5 messages per rolling hour |
| `GROUP_MAX_ACTIONS_PER_HOUR` | Aggregate ceiling across accounts; at most 5 messages per rolling hour |
| `COORDINATOR_DATABASE_PATH` | Shared ledger, default `data/coordinator.sqlite3`; preserve across restarts and deployments |
| `ACCOUNT_RESTART_DELAY_SECONDS` | Supervisor retry baseline; at least 30 seconds, subject to longer backoff |
| `BATCH_DELAY_SECONDS`, `BATCH_SIZE` | Defaults to 5 seconds and 20 incoming candidates |
| `MAX_PENDING_AGE_SECONDS` | Defaults to 720 seconds; stale messages are discarded from pending work |
| `GROUP_AUTOMATION_ALLOWED`, `ACCOUNT_N_AUTOMATION_DISCLOSED` | Explicit operator attestations required for live mode; default `false` |

The group cap is **five total messages, not five multiplied by the number of accounts**. Stricter hourly caps are allowed. No account is required to fill its available slots. The obsolete `MIN_GAP_SECONDS` and `GROUP_MIN_GAP_SECONDS` variables are ignored, even if inherited from an old environment. Group coordination suppresses replies to the other configured accounts, duplicate targets and repeated wording. Selection rejects leaderboard/score-farming requests; the system does not coordinate accounts to inflate activity scores.

The project keeps its existing package structure:

```text
telegram-challenger/
  main.py                         Application entry point and explicit login
  telegram_challenger_v07.py       Compatible original command
  challenger/
    config.py                     Shared and independent account configuration
    application.py                Independent account lifecycle and supervision
    auth.py                       Explicit login and safe legacy-session copying
    coordinator.py                Persistent group limits, claims and identities
    telegram_app.py               Telethon adapter and session locks
    engine.py                     Durable queue processing and final validation
    groq_client.py                Shared asynchronous Groq HTTP integration
    selection.py                  Eligibility, style and grounding checks
    storage.py                    Account queues, activity and persistent waits
    models.py                     Message, identity and decision records
  tests/                          Mocked network and temporary-storage tests
  sessions/                       Private Telegram sessions, created when needed
  data/                           New account histories and shared coordinator
  .env                            Existing private configuration
  .env.example                    Reference configuration for five slots
  requirements.txt
```

Each account has its own client, worker, queue, identity and activity history. One failed/disconnected account does not stop healthy accounts. They share a bounded Groq client and central coordinator. Model-format errors trigger validated compatibility fallbacks; transient errors use bounded backoff, and provider rate-limit delays are honored. A provider-wide failure defers requests centrally instead of having each account retry aggressively. Telegram FloodWait is persisted for the affected account.

## Selection and conversational style

IGNORE is the default. Deterministic filters reject generic observations, acknowledgments, fragments, reactions, conversations addressed to other members, stale questions and questions already answered. Bot/spam keywords alone never require a response. Clear questions and substantive statements in an active open discussion can become candidates; a question or mention is not required. Statements need recent topic-related participation from another member, and still pass grounding, repetition and private-exchange checks. Quotas are ceilings, never targets.

One selected message ID is validated against the eligible batch. The worker fetches that target and newer messages again before reserving a preview or sending with the exact `reply_to` ID. Incoming events continue being written to SQLite while Groq is processing. Excessively busy or ambiguous final history checks defer work.

Replies usually contain 3–8 words, with a hard maximum of 10, use simple casual English or Nigerian Pidgin, and allow at most one supported emoji. Useful apostrophes and commas survive; exclamation marks and ending periods are removed. Generic filler, repetitive text, member impersonation, invented personal experiences and unsupported claims are rejected. Grounded clarification questions are occasional rather than habitual. Direct questions about automation receive a truthful, brief disclosure, subject to the same quotas and repetition checks.

At `LOG_LEVEL=INFO`, `RECEIVED` confirms event reception, `QUEUED` confirms storage, `FILTERED` explains rejection, and `EVALUATING` identifies a Groq batch. `PROPOSED` means final checks are still pending; `SIMULATED REPLY` records a preview and consumes quota; `SENT REPLY` follows Telegram confirmation. A `WORKER` heartbeat reports queue size and remaining wait at least once per minute while processing normally. The resolved group ID is checked before workers start. If no messages are worthwhile, filters and heartbeat logs remain visible without forced inference or replies.

Startup identity checks use a temporary session copy so they cannot advance the real session's saved update position and skip offline messages. Five messages per hour is a ceiling, not a guaranteed output target: meaningful participation and stale-message checks take precedence over filling slots.

## Existing files and persistent scheduling

The original `.env` is never rewritten. Existing legacy `ACCOUNT_ID`, `SESSION_PATH` and `DATABASE_PATH` still configure slot 1. Its legacy account ID remains the database namespace, and collisions with other enabled slot IDs are rejected.

If paths are not explicitly overridden and the root `account_1.session` exists, the first normal run or explicit login makes a consistent SQLite backup copy into `sessions/account_1.session`. The original stays untouched. An existing dedicated session is never overwritten. `--check-config` does not copy anything. Stop older instances before migration; do not run the old root session and the copied session concurrently.

An existing root `activity_account_1.sqlite3` remains in use. Existing custom database paths remain in use. The original single-account schema migration, when needed, creates a `.pre-challenger-v1.bak` backup, adds `tc_*` tables and retains original `actions`/`candidates` history. Legacy pending rows lacking thread information remain skipped history.

Per-account and shared coordinator reservations are committed before sending or logging a successful preview. An uncertain send or crash retains its reservation; an interrupted cross-database reservation may conservatively consume a slot without a message. Target claims remain permanent so restart cannot resend to an old target. This deliberately favors missed replies over duplicate messages.

The rolling window is `(now - 3600, now]`. With fewer than five counted messages, another relevant reply may be sent immediately after normal batch processing; no ten-minute check applies. At the cap, sending waits until a timestamp leaves the window. Confirmed sends, simulated sends and observed outgoing text share account quotas. In-flight and uncertain reservations temporarily count conservatively to prevent over-sending; an explicit Telegram rejection releases its reservation but still enforces the full FloodWait. Historical ambiguous failures remain conservatively counted, and reaction-only history does not consume text-message quota. Never delete/reset a database or change its path to reset the hourly quota. Keep the coordinator alongside every account history when moving the application. Stop the old instance before starting on another machine.

`QUOTA` logs report remaining account and group slots, wait duration, and the reason for any block. `PROPOSED` reports the model's acceptance reason; `FILTERED` and `IGNORE` identify rejection reasons. A slot reservation is not proof of delivery: `SENT REPLY` means confirmed, `SIMULATED REPLY` means preview, and uncertain outcomes retain their reservation. Batch debouncing, duplicate prevention, shared inference limits and provider backoff remain active. The scheduler never creates activity to use spare slots.

## Run continuously on Windows

Authenticate all enabled accounts interactively first, finish preview testing, and configure the intended mode in `.env`. In Windows Task Scheduler, create one task under the same Windows user that owns the project:

- Trigger: At startup, with a short delay for networking, or At log on.
- Program/script: `C:\Users\EMMANUEL\Desktop\telegram-challenger\.venv\Scripts\python.exe`
- Add arguments: `-X utf8 -u "C:\Users\EMMANUEL\Desktop\telegram-challenger\main.py"`
- Start in: `C:\Users\EMMANUEL\Desktop\telegram-challenger`
- If already running: Do not start a new instance.
- Disable the default setting that stops a task after a fixed number of days. Set restart on failure to one minute with a bounded retry count.

Do not place credentials in task arguments. A background task cannot answer login prompts; use `--login` beforehand. Account supervisors recover individual connections within the running process. For a clean manual stop during development, use the foreground PowerShell command and Ctrl+C. Abrupt process termination still retains conservative send reservations.

## Run continuously on a Linux server

Use a dedicated unprivileged service user, a persistent project directory such as `/opt/telegram-challenger`, Python 3.11+, and a virtual environment with `requirements.txt` installed. Transfer the existing `.env`, sessions, all activity databases and the coordinator together after stopping the old instance. Protect the directory and private files so only the service user can read them; do not put them in an ephemeral container filesystem. Authenticate enabled accounts interactively as that same user before enabling the service.

Example `/etc/systemd/system/telegram-challenger.service`:

```ini
[Unit]
Description=Telegram Challenger
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=telegram-challenger
Group=telegram-challenger
WorkingDirectory=/opt/telegram-challenger
ExecStart=/opt/telegram-challenger/.venv/bin/python -u /opt/telegram-challenger/main.py
Environment=PYTHONUTF8=1
Restart=on-failure
RestartSec=60
KillSignal=SIGTERM
TimeoutStopSec=60
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

After installing the unit, use `sudo systemctl daemon-reload` and `sudo systemctl enable --now telegram-challenger`. Inspect logs with `sudo journalctl -u telegram-challenger -f`; stop cleanly with `sudo systemctl stop telegram-challenger`. SIGTERM and Ctrl+C request task cancellation and cleanup. Retain databases and sessions during service restarts and deployment updates.

## Validation and limitations

The test suite exercises account configuration, separate paths, session preservation, independent and shared scheduling, preview quotas, cross-account suppression, target/reply duplication, malformed Groq output, busy queues, disconnections, persistent history and cleanup. Tests use fake Telegram clients and HTTP transports and never require real messages or a Groq call.

Conversation intent, factual correctness and dialect matching cannot be guaranteed perfectly. Conservative checks intentionally skip some useful conversations; the participant does not browse for current facts. Unknown identities while accounts are disconnected, unavailable Telegram history, and a reply/edit racing the final network request can limit complete coordination. The coordinator controls this application's actions, not messages sent manually or by unrelated clients; observed manual outgoing text is reconciled when available.

Keep the system clock synchronized. A large forward clock change can distort elapsed-time limits. History and permanent target claims grow with traffic; plan persistent disk space and private backups. Logs contain decisions, target IDs and previews; protect logs along with credentials, sessions, databases and backups. Real authorization, group access, provider availability and model access require a deliberate network run and are not proven by mocked tests.
