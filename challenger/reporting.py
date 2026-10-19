"""Read-only, redacted diagnostics. No chat text or credentials leave the worker."""
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import PERSONAS
from .windows import window_start, day_start


def read_db(path):
    if not Path(path).is_file():
        return None
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    return db


def rows(db, sql, args=()):
    if db is None:
        return []
    try:
        return [dict(row) for row in db.execute(sql, args)]
    except sqlite3.OperationalError:
        return []


def account_snapshot(settings, now):
    db = read_db(settings.database_path)
    try:
        actions = rows(db, "SELECT id,activity_at,status,mode,kind,telegram_id,source_id FROM tc_actions WHERE account_id=? AND activity_at>=?",
                       (settings.account_id, now - 31 * 86400))
        metadata = {r["key"].split(":", 1)[-1]: r["value"] for r in rows(
            db, "SELECT key,value FROM tc_metadata WHERE key LIKE ?", (settings.account_id + ":%",))}
        blocks = rows(db, "SELECT scope,until FROM tc_blocks WHERE account_id=?", (settings.account_id,))
        points = rows(db, "SELECT awarded_at,points FROM tc_points WHERE account_id=? AND awarded_at>=?",
                      (settings.account_id, now - 31 * 86400))
    finally:
        if db is not None:
            db.close()
    start = window_start(now, settings.window_mode, settings.timezone)
    current = [a for a in actions if (a["activity_at"] > start if settings.window_mode == "rolling" else a["activity_at"] >= start)
               and a["kind"] in {"REPLY", "START_DISCUSSION"} and a["status"] != "rejected"]
    sent = [a for a in actions if a["status"] == "sent" and a["telegram_id"] is not None]
    manual = sum(a["mode"] == "observed" and a in sent for a in current)
    bot = sum(a["mode"] == "live" and a in sent for a in current)
    preview = sum(a["mode"] == "preview" for a in current)
    remaining = max(0, settings.max_actions - len(current))
    boundary = (sorted((a["activity_at"] for a in current), reverse=True)[settings.max_actions - 1] + 3600
                if not remaining and settings.window_mode == "rolling" else start + 3600 if not remaining else now)
    blocked = max([b["until"] for b in blocks if b["scope"] == "telegram"] + [0])
    last_sync = float(metadata.get("last_sync", 0))
    heartbeat = float(metadata.get("heartbeat", 0))
    health = metadata.get("health", "not_started") if now - heartbeat < 90 else "offline"
    today = day_start(now, settings.timezone)
    daily = {}
    hourly = {}
    for action in sent:
        date = datetime.fromtimestamp(action["activity_at"], ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
        daily[date] = daily.get(date, 0) + 1
        hour = int(action["activity_at"] // 3600) * 3600
        if hour >= now - 24 * 3600:
            hourly[hour] = hourly.get(hour, 0) + 1
    events = [{"id": f"{settings.account_id}:{a['id']}", "account_id": settings.account_id,
               "at": a["activity_at"], "status": a["status"], "kind": a["kind"],
               "origin": "manual" if a["mode"] == "observed" else a["mode"],
               "has_reply_target": a["source_id"] is not None}
              for a in sorted(actions, key=lambda a: a["activity_at"], reverse=True)[:100]]
    return dict(account_id=settings.account_id, enabled=True, persona=settings.persona,
                hourly_limit=settings.max_actions,
                window_start=start if settings.window_mode == "fixed" else None,
                resets_at=start + 3600 if settings.window_mode == "fixed" else None,
                health=health, mode="live" if settings.auto_send else "preview", window=settings.window_mode,
                manual=manual, bot=bot, preview=preview, uncertain=max(0, len(current) - manual - bot - preview),
                total=manual + bot, reserved_total=len(current), remaining=remaining,
                next_available=max(boundary, blocked), last_sync=last_sync, heartbeat=heartbeat,
                last_activity=max([a["activity_at"] for a in sent] + [0]),
                today=sum(a["activity_at"] >= today for a in sent),
                verified_today=sum(p["points"] for p in points if p["awarded_at"] >= today) if points else None,
                scoring_connected=bool(points), theoretical_daily=120, flood_until=blocked,
                daily=daily, hourly=hourly, events=events)


def snapshot(app, now=None):
    now = time.time() if now is None else now
    accounts = [account_snapshot(a, now) for a in app.accounts]
    for slot in range(1, 6):
        if not any(a["account_id"] == f"account_{slot}" for a in accounts) and len(accounts) < 5:
            accounts.append(dict(account_id=f"account_{slot}", enabled=False, persona=PERSONAS[slot - 1], health="disabled", theoretical_daily=120))
    db = read_db(app.coordinator_path)
    try:
        usage = rows(db, "SELECT at,account_id,purpose,tokens,reserved_tokens,status FROM groq_usage WHERE at>=?", (now - 31 * 86400,))
        errors = rows(db, "SELECT at,level,component,code,account_id FROM diagnostic_events ORDER BY at DESC LIMIT 100")
    finally:
        if db is not None:
            db.close()
    today = day_start(now, app.accounts[0].timezone)
    daily_usage = {}
    for u in usage:
        date = datetime.fromtimestamp(u["at"], ZoneInfo(app.accounts[0].timezone)).strftime("%Y-%m-%d")
        entry = daily_usage.setdefault(date, {"requests": 0, "tokens": 0, "unreported": 0})
        entry["requests"] += 1
        entry["tokens"] += u["tokens"] or 0
        entry["unreported"] += u["tokens"] is None
    for account in accounts:
        account['errors'] = [e for e in errors if e.get('account_id') == account['account_id']]
        account['reconnects'] = sum(e['code'] == 'reconnecting' for e in account['errors'])
    return dict(version=1, generated_at=now, timezone=app.accounts[0].timezone,
                hourly_window=app.accounts[0].window_mode, hourly_timezone="UTC",
                accounts=sorted(accounts, key=lambda a: a["account_id"]), theoretical_daily=600,
                group_cap=app.group_max_actions, scoring_window_confirmed=False,
                groq=dict(requests_today=sum(u["at"] >= today for u in usage),
                          tokens_today=sum(u["tokens"] or 0 for u in usage if u["at"] >= today),
                          unreported_today=sum(u["tokens"] is None for u in usage if u["at"] >= today),
                          daily=daily_usage, purpose="selection + generation (one combined request)",
                          limits=dict(rpm=app.groq_rpm, rpd=app.groq_rpd, tpm=app.groq_tpm, tpd=app.groq_tpd)),
                errors=errors)


def cli_status(data):
    accounts = data["accounts"]
    print("Metric".ljust(20) + "".join(a["account_id"].ljust(17) for a in accounts))
    for label, key in (("Connection", "health"), ("Manual / window", "manual"), ("Bot / window", "bot"),
                       ("Preview / window", "preview"), ("Uncertain", "uncertain"), ("Remaining", "remaining"),
                       ("Messages today", "today"), ("Potential points*", "today"), ("Verified points", "verified_today"), ("Daily ceiling", "theoretical_daily")):
        print(label.ljust(20) + "".join(str(a.get(key) if a.get(key) is not None else "unknown").ljust(17) for a in accounts))
    for label, key in (("Last activity", "last_activity"), ("History sync", "last_sync"), ("Next send slot", "next_available")):
        values = [datetime.fromtimestamp(a[key], ZoneInfo(data['timezone'])).strftime('%m-%d %H:%M:%S') if a.get(key) else 'unknown' for a in accounts]
        print(label.ljust(20) + ''.join(value.ljust(17) for value in values))
    print('* Potential points are an unverified estimate, not awards.')
    print(f"Timezone: {data['timezone']} | Combined theoretical ceiling: 600/day; no points inferred from sends")
    print(f"Groq today: {data['groq']['requests_today']} requests; {data['groq']['tokens_today']} reported tokens")


def import_verified_points(settings, path):
    """Import operator-verified awards with unique evidence references; never infer."""
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(items, list) or len(items) > 10000:
        raise ValueError("Points file must be a bounded JSON list")
    validated = []
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"reference", "awarded_at", "points"}
                or type(item["points"]) is not int or not 0 <= item["points"] <= 600
                or type(item["awarded_at"]) not in (int, float) or not 0 < item["awarded_at"] <= time.time()
                or not isinstance(item["reference"], str) or not 1 <= len(item["reference"]) <= 500):
            raise ValueError("Each award needs reference, awarded_at (Unix seconds), and nonnegative integer points")
        validated.append((settings.account_id, item["reference"], item["awarded_at"], item["points"]))
    # Require existing account state; importing awards must not reset history.
    db = sqlite3.connect(Path(settings.database_path).resolve().as_uri() + "?mode=rw", uri=True)
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS tc_points(account_id TEXT,reference TEXT,awarded_at REAL,
                      points INTEGER,PRIMARY KEY(account_id,reference))""")
        db.executemany("INSERT OR IGNORE INTO tc_points VALUES (?,?,?,?)", validated)
        db.commit()
    finally:
        db.close()
