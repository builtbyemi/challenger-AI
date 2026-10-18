"""Optional Supabase publishing with a coalescing SQLite outbox and bounded retries."""
import asyncio
import json
import logging
import os
import re
from pathlib import Path
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlparse

import httpx

from .reporting import snapshot

log = logging.getLogger(__name__)


def failure_details(exc):
    """Allowlisted metadata only: never log provider bodies, URLs or headers."""
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        stage = "auth" if response.request.url.path.startswith("/auth/") else "rpc"
        status = response.status_code
        try:
            body = response.json()
            code = body.get("code", body.get("error_code", "unknown")) if isinstance(body, dict) else "unknown"
        except ValueError:
            code = "unknown"
        if not isinstance(code, str) or not (re.fullmatch(r"(?:[0-9][A-Z0-9]{4}|PGRST[0-9]{3})", code)
                or code in {"P0001", "invalid_credentials", "email_not_confirmed", "over_request_rate_limit", "bad_jwt"}):
            code = "unknown"
        category, hint = "http_error", "Check Supabase service availability"
        if status == 429:
            category, hint = "rate_limit", "Wait for the retry delay"
        elif status >= 500:
            category, hint = "service_unavailable", "Supabase failed the request; retry with backoff"
        elif stage == "auth":
            category, hint = "authentication", "Check the dedicated worker login and project URL"
        elif code == "42501":
            category, hint = "publisher_binding", "Check worker user UUID + worker_id binding in this project; EXECUTE permission alone is insufficient"
        elif code == "PGRST202":
            category, hint = "rpc_missing", "Apply the SQL migrations and reload PostgREST schema"
        elif status == 401:
            category, hint = "token_expired", "Worker will authenticate again on retry"
        elif code in {"P0001", "22007", "22008", "22003", "22P02", "23502", "23514"}:
            category, hint = "payload_rejected", "Check snapshot version, account count and worker clock against Supabase"
        return f"stage={stage} http={status} code={code} category={category}; {hint}"
    if isinstance(exc, httpx.TimeoutException):
        return "category=network_timeout; check connectivity to Supabase"
    if isinstance(exc, httpx.RequestError):
        return "category=network_error; check DNS, TLS and connectivity to Supabase"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "category=invalid_response_or_payload; check snapshot/auth response format"
    if isinstance(exc, sqlite3.Error):
        return "category=local_database; check outbox access and disk space"
    return "category=internal_error; run the telemetry check and inspect local configuration"


def retry_delay(exc, delay):
    if isinstance(exc, httpx.HTTPStatusError):
        value = exc.response.headers.get("Retry-After", "")
        if value.isdigit():
            return max(delay, min(300, int(value)))
    return delay


class Outbox:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS telemetry_outbox(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)")

    def put(self, payload):
        encoded = json.dumps(payload, allow_nan=False)
        if len(encoded.encode()) > 1000000:
            raise ValueError("Telemetry snapshot exceeds bound")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO telemetry_outbox VALUES (1,?)", (encoded,))

    def get(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT payload FROM telemetry_outbox WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def ack(self, timestamp):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM telemetry_outbox WHERE json_extract(payload,'$.generated_at')=?", (timestamp,))


class Publisher:
    def __init__(self, app, http=None):
        self.app = app
        self.url = os.getenv("TELEMETRY_SUPABASE_URL", "").strip().rstrip("/")
        self.key = os.getenv("TELEMETRY_SUPABASE_PUBLISHABLE_KEY", "").strip()
        self.email = os.getenv("TELEMETRY_WORKER_EMAIL", "").strip()
        self.password = os.getenv("TELEMETRY_WORKER_PASSWORD", "")
        self.worker_id = os.getenv("TELEMETRY_WORKER_ID", "local-worker").strip()
        if not all((self.url, self.key, self.email, self.password)) or urlparse(self.url).scheme != "https":
            raise ValueError("Telemetry needs HTTPS Supabase URL, publishable key, and dedicated worker login")
        if not 1 <= len(self.worker_id) <= 80:
            raise ValueError("TELEMETRY_WORKER_ID must contain 1–80 characters")
        self.outbox = Outbox(app.coordinator_path.parent / "telemetry.sqlite3")
        self.http = http or httpx.AsyncClient(timeout=15)
        self.owned_http = http is None
        self.token = None
        self.expires = 0
        self.wake = asyncio.Event()

    async def capture(self):
        while True:
            try:
                payload = await asyncio.to_thread(snapshot, self.app)
                await asyncio.to_thread(self.outbox.put, payload)
                self.wake.set()
            except Exception as exc:
                log.warning("Telemetry snapshot unavailable; %s; worker continues", failure_details(exc))
            await asyncio.sleep(5)

    async def publish_once(self):
        payload = await asyncio.to_thread(self.outbox.get)
        if payload is None:
            return
        if self.token is None or time.time() >= self.expires:
            response = await self.http.post(self.url + "/auth/v1/token?grant_type=password",
                                            headers={"apikey": self.key},
                                            json={"email": self.email, "password": self.password})
            response.raise_for_status()
            auth = response.json()
            token = auth["access_token"]
            lifetime = auth.get("expires_in", 3600)
            if not isinstance(token, str) or not token or type(lifetime) not in (int, float):
                raise ValueError("Invalid authentication response")
            self.token = token
            self.expires = time.time() + max(1, lifetime - 60)
        response = await self.http.post(self.url + "/rest/v1/rpc/publish_dashboard_snapshot",
                                        headers={"apikey": self.key, "Authorization": f"Bearer {self.token}"},
                                        json={"p_worker": self.worker_id, "p_payload": payload})
        if response.status_code == 401:
            self.token = None
        response.raise_for_status()
        await asyncio.to_thread(self.outbox.ack, payload["generated_at"])
        log.debug("Telemetry snapshot published; http=%s", response.status_code)

    async def binding_status(self):
        """Optional diagnostic RPC sees only the current publisher's binding."""
        if not self.token:
            return "Binding check unavailable until worker authentication succeeds"
        try:
            response = await self.http.post(self.url + "/rest/v1/rpc/dashboard_publisher_status",
                headers={"apikey": self.key, "Authorization": f"Bearer {self.token}"},
                json={"p_worker": self.worker_id})
            response.raise_for_status()
            data = response.json()
            if data.get("binding_exists") is not True:
                return "No binding exists for the authenticated worker user in this project"
            if data.get("worker_id_matches") is not True:
                return "Worker user has a binding, but its worker_id differs from TELEMETRY_WORKER_ID"
            return "Worker binding matches; confirm the dashboard uses its assigned owner login"
        except Exception as exc:
            return "Binding diagnostic unavailable: " + failure_details(exc)

    async def run(self):
        capture = asyncio.create_task(self.capture())
        delay = 5
        try:
            while True:
                await self.wake.wait()
                self.wake.clear()
                try:
                    await self.publish_once()
                    delay = 5
                except Exception as exc:
                    wait = retry_delay(exc, delay)
                    log.warning("Telemetry upload deferred; local snapshot retained; %s; retry=%ss",
                                failure_details(exc), wait)
                    await asyncio.sleep(wait)
                    delay = min(60, delay * 2)
                    self.wake.set()
        finally:
            capture.cancel()
            await asyncio.gather(capture, return_exceptions=True)
            if self.owned_http:
                await self.http.aclose()


async def check_telemetry(app):
    """Publish one fresh read-only snapshot; never connect Telegram or Groq."""
    publisher = None
    try:
        publisher = Publisher(app)
        publisher.outbox.put(await asyncio.to_thread(snapshot, app))
        await publisher.publish_once()
        print("Telemetry publish succeeded. Sign in as the assigned owner to read dashboard_snapshots.")
        print("Publisher reads returning zero rows are expected under RLS.")
        return 0
    except Exception as exc:
        print("Telemetry check failed: " + failure_details(exc))
        if publisher is not None:
            print(await publisher.binding_status())
        return 1
    finally:
        if publisher is not None and publisher.owned_http:
            await publisher.http.aclose()
