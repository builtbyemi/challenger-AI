# Supabase telemetry repair

Live checks on 2026-10-18 found:

- Worker password authentication: HTTP 200.
- Publishing the real redacted five-account snapshot: HTTP 403, SQLSTATE `42501`, `Worker not authorized`.
- The deployed function requires **both** the authenticated publisher UUID and `TELEMETRY_WORKER_ID` to match one binding. `local-worker` exists, but that alone does not authorize the logged-in publisher. EXECUTE grants do not bypass this check. Without administrator database access, the exact contents of the mismatching binding cannot be inspected.
- `dashboard/.env.local` used worker-only `TELEMETRY_*` names. These were renamed to the required `VITE_SUPABASE_*` names. Its public key returned HTTP 401; it was replaced with the verified public/anon worker project key, which returned HTTP 200. Both URLs identify the same project. No worker password or private key was placed in the frontend.

## Required administrator step

In the Supabase **SQL Editor for the project configured in the root `.env`**:

1. Run `supabase/migrations/202610180002_publisher_diagnostics.sql`. It is additive/idempotent and lets a publisher diagnose only its own binding. It does not change RLS or grant table writes.
2. Open `supabase/repair_worker_binding.sql`. Replace `REPLACE_WITH_WORKER_LOGIN_EMAIL` with the email in root `.env`'s `TELEMETRY_WORKER_EMAIL`; replace `REPLACE_WITH_DASHBOARD_OWNER_EMAIL` with the separate user you sign into the dashboard with. Run the edited SQL in the SQL Editor. **Do not put passwords/API keys in SQL.**

The repair resolves UUIDs from `auth.users`, creates/corrects the `local-worker` mapping, and can safely be repeated. It rejects missing users, identical owner/publisher, reassignment from another owner, or two conflicting existing publisher bindings. If it reports conflicting bindings, inspect them as administrator before making changes; no conflicting record is silently deleted. Existing snapshots, Telegram sessions, quota/history databases and account configuration are preserved.

Copy files into the SQL Editor without a CLI deployment:

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai'
Get-Content -Raw .\supabase\migrations\202610180002_publisher_diagnostics.sql | Set-Clipboard
# Paste/run in Supabase, then copy the repair and replace its two placeholders:
Get-Content -Raw .\supabase\repair_worker_binding.sql | Set-Clipboard
```

## Verify without starting Telegram

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai'
& .\.venv\Scripts\python.exe -X utf8 -u main.py --telemetry-check
```

This reads existing account history, retains a fresh redacted snapshot in the telemetry outbox, authenticates the publisher and calls the RPC once. It never starts Telegram or Groq, sends group messages, or changes quotas. Exit 0 means the publish RPC succeeded; exit 1 includes safe HTTP/SQL error metadata. After the diagnostic migration, failures distinguish a missing binding from a worker-ID mismatch. HTTP 404 / PGRST202 on the diagnostic means that additive migration has not been applied yet.

In Supabase SQL Editor, confirm reception without dumping payloads:

```sql
select worker_id, received_at, generated_at,
       jsonb_array_length(payload->'accounts') as accounts
from public.dashboard_snapshots
where worker_id = 'local-worker';
```

The restricted publisher intentionally cannot SELECT the owner's snapshots. An empty SELECT under that publisher login is expected; check as administrator or sign into the dashboard as the assigned **owner**.

## Start the local worker and dashboard

Keep `AUTO_SEND=false`. Worker terminal:

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai'
& .\.venv\Scripts\python.exe -X utf8 -u main.py --preview
```

Dashboard terminal (restart Vite to load the corrected environment):

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai\dashboard'
npm run dev
```

Open the URL Vite prints (normally `http://127.0.0.1:5173`) and sign in as the assigned owner. If necessary, sign out of a cached login first. Account status, message activity, Groq usage and sanitized logs come from the same snapshot. Polling runs every 15 seconds even if Realtime is unavailable. No Cloudflare/GitHub/Railway deployment is necessary. Preview may call Groq and reserves simulated quota; the one-shot telemetry check does neither.

## Verification boundary

Final local results: **264 Python tests (263 passed, one Windows/POSIX skip); 15 frontend/database tests passed; one browser integration test passed; production Vite build passed.** The browser test exercises synthetic workers through SQLite, publishing, embedded PostgreSQL/RLS and the dashboard with mocked external HTTP. Separate frontend tests verify account status, message activity, usage and sanitized diagnostics. The administrator repair was applied twice in embedded PostgreSQL and corrected a deliberately wrong publisher UUID without deleting snapshots.

The worker/Supabase clock difference was under one second during the live check, and all five configured accounts still have `AUTO_SEND=false`.

Changed files: `challenger/telemetry.py`, `main.py`, local ignored `dashboard/.env.local`, `dashboard/src/App.jsx`, `dashboard/src/data.js`, `dashboard/src/App.test.jsx`, `dashboard/src/database.test.js`, `tests/test_telemetry.py`, `tests/test_upgrade.py` (standalone test imports), `tests/test_auth.py` (preserves the existing two-retry setting), `supabase/migrations/202610180002_publisher_diagnostics.sql`, `supabase/repair_worker_binding.sql`, `README.md`, `DEPLOYMENT.md`, `VERIFICATION.md`, and this guide. No root `.env` credentials, sessions, quota/history databases or account identities were changed. Only the telemetry outbox was updated by the one-shot check.

Live auth and the failed publication were reproduced; the frontend public key is now validated. Administrator SQL access and the owner's authenticated browser session are not available to the agent. Successful **live** publication and owner-visible rows remain unverified until the administrator repair and one-shot check above succeed. Local tests use fake HTTP transports and embedded PostgreSQL to exercise successful publication, RLS separation, binding repair, retries and frontend loading without real Telegram messages.
