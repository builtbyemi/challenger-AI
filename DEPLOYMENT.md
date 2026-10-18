# Private dashboard deployment

The Python worker runs on Windows. Cloudflare Pages hosts only the static React frontend. Supabase stores redacted telemetry and handles owner authentication. The frontend has no API for sending Telegram messages or changing worker credentials.

For `42501 / Worker not authorized` uploads and local frontend configuration, follow [TELEMETRY_FIX.md](TELEMETRY_FIX.md). It includes the diagnostic migration, administrator binding repair, and a Telegram-free `--telemetry-check` command. Cloud hosting is optional for local use.

## 1. Supabase project and roles

1. Create a Supabase project under your account. Disable public signup in Authentication settings; create users administratively.
2. Create two separate confirmed email/password users: a **dashboard owner** and a **worker publisher**. Use different strong passwords. No service-role key is needed by the worker or browser.
3. In the SQL editor, run `supabase/migrations/202610180001_dashboard.sql` from this project. The migration creates an owner-scoped telemetry table with RLS and a narrowly authorized publisher RPC.
4. Copy the two users' UUIDs from Authentication → Users. Bind the publisher to the owner using this SQL, substituting the UUIDs:

```sql
insert into challenger_private.worker_bindings(worker_user_id, owner_id, worker_id)
values ('WORKER_USER_UUID', 'OWNER_USER_UUID', 'local-worker');
```

The table rejects using the same user as owner and worker. The browser owner can only SELECT its snapshots. The worker cannot read those snapshots or write tables directly; its authenticated RPC call can publish only to its assigned owner/worker pair. Anonymous reads and RPC execution are denied. An authenticated outsider sees no owner's telemetry. There are no browser-exposed mutation settings or service credentials.

5. Confirm `dashboard_snapshots` is in the `supabase_realtime` publication. The migration adds it. Restrict Supabase Auth's Site URL and permitted redirects to your deployment and localhost development URL. The app uses email/password sign-in, not a public registration form.

References: [Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security), [Postgres Changes and authorization](https://supabase.com/docs/guides/realtime/postgres-changes).

## 2. Worker configuration

Add the following to the existing **root** `.env`; preserve all Telegram and Groq settings:

```dotenv
TELEMETRY_ENABLED=true
TELEMETRY_SUPABASE_URL=https://YOUR_PROJECT.supabase.co
TELEMETRY_SUPABASE_PUBLISHABLE_KEY=YOUR_PUBLISHABLE_KEY
TELEMETRY_WORKER_EMAIL=YOUR_WORKER_EMAIL
TELEMETRY_WORKER_PASSWORD=YOUR_WORKER_PASSWORD
TELEMETRY_WORKER_ID=local-worker
```

The worker logs in using the restricted publisher account. Passwords/tokens are not logged. HTTPS is required. Upload failures retry with bounded exponential backoff, while a single-row local SQLite outbox retains the newest snapshot. Telemetry collection and network I/O run independently of Telegram work. Disable telemetry by setting `TELEMETRY_ENABLED=false`; local accounting still works.

Start safely in preview:

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai'
& .\.venv\Scripts\python.exe -X utf8 main.py --preview
```

Preview makes real history reads and Groq calls but sends no Telegram messages. It consumes simulated allowance in persistent SQLite. The dashboard labels previews separately from confirmed sends. Use the README's explicit live commands only after completing authentication and checking group rules.

## 3. Frontend locally

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai\dashboard'
npm ci
if (-not (Test-Path .env.local)) { Copy-Item .env.example .env.local }
```

Edit **dashboard/.env.local**:

```dotenv
VITE_SUPABASE_URL=https://YOUR_PROJECT.supabase.co
VITE_SUPABASE_PUBLISHABLE_KEY=YOUR_PUBLISHABLE_KEY
```

Both values are public project identifiers. Never place the worker password, Telegram API hashes, Groq key, session files or a service-role key in a `VITE_` variable. The browser rejects recognizable Supabase secret/service-role keys. Only files under `dashboard/` are frontend build inputs.

```powershell
npm run dev
# http://127.0.0.1:5173
```

Sign in with the **owner** user. Realtime notices trigger a refetch through the RLS-protected endpoint; polling every 15 seconds remains active as a fallback. The age indicator uses the worker snapshot timestamp, not the browser's latest fetch. Without configured credentials, “View empty dashboard layout” is a visibly empty preview, not live data.

## 4. Cloudflare Pages

Build and deploy only the `dashboard/dist` directory. The CLI will require your Cloudflare login; no deployment credentials are included in the project.

```powershell
Set-Location 'C:\Users\EMMANUEL\Desktop\Challenger Ai\dashboard'
npm ci
npm test
npm run build
npx wrangler login
# First deployment only; choose your intended production branch:
npx wrangler pages project create telegram-challenger --production-branch main
npm run deploy
```

The Cloudflare **project name** `telegram-challenger` is a deployment identifier, not a Windows filesystem path; renaming the local folder to `Challenger Ai` does not require changing it. You may choose a different Pages project name and update the package's deploy script to match. For Git deployment, set root directory `dashboard`, build command `npm ci && npm run build`, output `dist`, Node version 24, and configure the two `VITE_` variables in Pages settings. Frontend variables are embedded at build time; rebuild after changing them.

`public/_headers` applies CSP, no framing, no-referrer and content-type protections on Pages. Its connection allowlist supports standard `*.supabase.co` projects. If using a custom Supabase domain, update the HTTPS/WebSocket allowlist deliberately before building. The app does not use server routes, so no SPA rewrite is required.

Reference: [Cloudflare Pages static deployment](https://developers.cloudflare.com/pages/framework-guides/deploy-anything/).

## 5. Deployment smoke test

- Verify anonymous visitors cannot retrieve `dashboard_snapshots` with the public project key.
- Sign in as the owner and confirm only that owner's worker snapshot is visible. A separate unrelated authenticated user must see no rows.
- Start the local worker with `--preview`, confirm a current heartbeat, then stop it. Data must turn stale after 20 seconds and offline after 90 seconds even if polling succeeds.
- Confirm Realtime reports subscribed; temporarily block WebSocket connections and verify polling continues to update the page.
- Compare manual, bot, preview and remaining counters against `python main.py --status`. No verified points should appear without an explicitly imported award record.
- Do not send live Telegram messages as part of deployment testing. Automated tests cover synthetic manual/outgoing events and mocked sends. A real cross-device manual-message test needs a separate user-authorized test conversation.

Hosted Auth, Realtime and Pages deployment cannot be certified by local mocks alone. Until these steps run against your configured projects, those external integrations remain deployment-dependent.

## Backups and operation

Keep `.env`, the entire `sessions/` directory, legacy account files, per-account databases and `data/coordinator.sqlite3` together in a private backup. Stop the worker for ordinary file copies or use SQLite's backup API; do not discard WAL files from an active database. Move existing state instead of creating new DB paths to reset quotas. Protect the Windows account and filesystem containing sessions and logs.

Use `main.py --health` for a local exit-code health check and `--status` for read-only diagnostics. The dashboard Settings page is informational. Account errors/reconnections are stored as sanitized diagnostic categories; original conversation bodies and exceptions remain outside public telemetry. Retain local logs privately and rotate them according to hosting disk limits.
