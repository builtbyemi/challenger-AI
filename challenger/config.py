"""Load existing environment names without modifying the user's .env."""
from dataclasses import dataclass
import os
from pathlib import Path
import re

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    chat_id: int
    groq_api_key: str
    groq_model: str
    account_id: str = "account_1"
    session_path: Path = Path("account_1")
    database_path: Path = Path("activity_account_1.sqlite3")
    auto_send: bool = False
    max_actions: int = 5
    batch_delay: float = 5
    batch_size: int = 20
    max_age: int = 720
    log_level: str = "INFO"
    # The runtime may copy this session with SQLite's backup API. Configuration
    # inspection remains read-only, and the original session is never moved.
    legacy_session_path: Path | None = None
    automation_disclosed: bool = False
    persona: str = "Casual Nigerian Pidgin, relaxed and funny; match the conversation's language"
    window_mode: str = "rolling"
    timezone: str = "Africa/Lagos"
    max_reply_words: int = 5
    quiet_seconds: int = 900
    discussion_topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplicationSettings:
    accounts: tuple[Settings, ...]
    coordinator_path: Path
    groq_concurrency: int = 1
    group_max_actions: int = 25
    restart_delay: int = 30
    group_automation_allowed: bool = False
    groq_rpm: int = 20
    groq_rpd: int = 1000
    groq_tpm: int = 10000
    groq_tpd: int = 200000


PERSONAS = (
    "Casual Nigerian Pidgin, relaxed and funny; match the conversation's language",
    "Friendly conversational English",
    "Playful and witty, occasional emojis",
    "Calm, thoughtful and observant",
    "Energetic and social, light slang",
)


def _window_mode() -> str:
    mode = os.getenv("HOURLY_WINDOW_MODE", "fixed").strip().lower()
    if mode not in {"rolling", "fixed"}:
        raise ValueError("HOURLY_WINDOW_MODE must be rolling or fixed")
    return mode


def _timezone() -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = os.getenv("REPORT_TIMEZONE", "Africa/Lagos").strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("REPORT_TIMEZONE must be an IANA timezone; install tzdata") from None
    return name


def load_settings(base: Path) -> Settings:
    """Compatibility loader retaining the original single-account path defaults."""
    load_dotenv(base / ".env", override=False)
    def number(name: str, default: str) -> int:
        try:
            return int(os.getenv(name, default))
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None

    def path(name: str, default: str) -> Path:
        value = Path(os.getenv(name, default)).expanduser()
        return value if value.is_absolute() else base / value

    account = os.getenv("ACCOUNT_ID", "account_1").strip()
    if not account or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in account):
        raise ValueError("ACCOUNT_ID must contain letters, numbers, hyphens or underscores")
    raw_send = os.getenv("AUTO_SEND", "false").strip().lower()
    if raw_send not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError("AUTO_SEND must be true or false")
    settings = Settings(
        api_id=number("API_ID", "0"),
        api_hash=os.getenv("API_HASH", "").strip(),
        chat_id=number("TARGET_CHAT_ID", "0"),
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip(),
        account_id=account,
        session_path=path("SESSION_PATH", account),
        database_path=path("DATABASE_PATH", f"activity_{account}.sqlite3"),
        auto_send=raw_send in {"true", "1", "yes"},
        max_actions=max(1, min(5, number("MAX_ACTIONS_PER_HOUR", "5"))),
        batch_delay=max(1, min(60, number("BATCH_DELAY_SECONDS", "5"))),
        batch_size=max(1, min(50, number("BATCH_SIZE", "20"))),
        max_age=max(60, min(720, number("MAX_PENDING_AGE_SECONDS", "720"))),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        window_mode=_window_mode(), timezone=_timezone(),
        quiet_seconds=max(300, number("QUIET_PERIOD_SECONDS", "900")),
        discussion_topics=tuple(t.strip() for t in os.getenv("DISCUSSION_TOPICS", "").split(";") if t.strip()),
    )
    if settings.api_id <= 0 or not settings.api_hash or not settings.chat_id:
        raise ValueError("Missing API_ID, API_HASH or TARGET_CHAT_ID in .env")
    if not settings.groq_api_key or not settings.groq_model:
        raise ValueError("Missing GROQ_API_KEY or GROQ_MODEL in .env")
    if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    return settings


def _number(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def _boolean(name: str, default: str) -> bool:
    value = os.getenv(name, default).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1", "yes"}


def _path(base: Path, name: str, default: str) -> Path:
    value = os.getenv(name, default).strip()
    if not value or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty file path")
    result = Path(value).expanduser()
    result = result if result.is_absolute() else base / result
    if result.is_dir():
        raise ValueError(f"{name} must identify a file, not a directory")
    return result


def _session_file(path: Path) -> Path:
    """Telethon appends .session unless that literal suffix is already present."""
    return path if str(path).endswith(".session") else Path(f"{path}.session")


def _canonical(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def load_application_settings(base: Path, *, login_accounts: tuple[str, ...] = ()) -> ApplicationSettings:
    """Read up to five independent slots without touching sessions or databases.

    Only slot one is enabled by default. Its legacy ACCOUNT_ID and database are
    retained so existing activity rows keep their account namespace. New session
    defaults live under sessions/; a legacy source is metadata for the runtime,
    never a request to migrate during this read-only operation.
    """
    load_dotenv(base / ".env", override=False)
    for name in os.environ:
        match = re.fullmatch(r"ACCOUNT_(\d+)_ENABLED", name)
        if match and not 1 <= int(match.group(1)) <= 5 and _boolean(name, "false"):
            raise ValueError("Only ACCOUNT_1 through ACCOUNT_5 can be enabled")
    enabled = [slot for slot in range(1, 6)
               if _boolean(f"ACCOUNT_{slot}_ENABLED", "true" if slot == 1 else "false")]
    for account in login_accounts:
        legacy_account = os.getenv("ACCOUNT_ID", "account_1").strip()
        if account != legacy_account and not re.fullmatch(r"account_[1-5]", account):
            raise ValueError("Login account must be account_1 through account_5")
        slot = 1 if account == legacy_account else int(account[-1])
        if slot not in enabled:
            enabled.append(slot)
    enabled.sort()
    if not enabled:
        raise ValueError("At least one ACCOUNT_1_ENABLED through ACCOUNT_5_ENABLED must be true")

    api_key = os.getenv("GROQ_API_KEY", "").strip()
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
    if not api_key or not model:
        raise ValueError("Missing GROQ_API_KEY or GROQ_MODEL in .env")
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    common = dict(
        groq_api_key=api_key, groq_model=model,
        auto_send=False if login_accounts else _boolean("AUTO_SEND", "false"),
        max_actions=max(1, min(5, _number("MAX_ACTIONS_PER_HOUR", "5"))),
        batch_delay=max(1, min(60, _number("BATCH_DELAY_SECONDS", "5"))),
        batch_size=max(1, min(50, _number("BATCH_SIZE", "20"))),
        max_age=max(60, min(720, _number("MAX_PENDING_AGE_SECONDS", "720"))),
        log_level=log_level,
        window_mode=_window_mode(), timezone=_timezone(), max_reply_words=5,
        quiet_seconds=max(300, _number("QUIET_PERIOD_SECONDS", "900")),
        discussion_topics=tuple(t.strip() for t in os.getenv("DISCUSSION_TOPICS", "").split(";") if t.strip()),
    )
    accounts = []
    for slot in enabled:
        prefix = f"ACCOUNT_{slot}_"
        account = os.getenv("ACCOUNT_ID", "account_1").strip() if slot == 1 else f"account_{slot}"
        if not account or not re.fullmatch(r"[A-Za-z0-9_-]+", account):
            raise ValueError("ACCOUNT_ID must contain letters, numbers, hyphens or underscores")

        # API credentials identify the application. Empty per-slot placeholders
        # inherit shared credentials; phone login still identifies each user.
        def key(suffix: str) -> str:
            specific = prefix + suffix
            if suffix in {"API_ID", "API_HASH"} and not os.getenv(specific, "").strip():
                return suffix
            return specific if specific in os.environ else suffix

        api_id_name, api_hash_name, chat_name = key("API_ID"), key("API_HASH"), key("TARGET_CHAT_ID")
        api_id = _number(api_id_name, "0")
        api_hash = os.getenv(api_hash_name, "").strip()
        chat_id = _number(chat_name, "0")
        if api_id <= 0:
            raise ValueError(f"{prefix}API_ID (or API_ID) must be a positive integer")
        if not api_hash:
            raise ValueError(f"Missing {prefix}API_HASH (or API_HASH) in .env")
        if not chat_id:
            raise ValueError(f"Missing {prefix}TARGET_CHAT_ID (or TARGET_CHAT_ID) in .env")

        session_name = prefix + "SESSION_PATH"
        explicit_session = session_name in os.environ
        if slot == 1 and not explicit_session and "SESSION_PATH" in os.environ:
            session_name = "SESSION_PATH"
            explicit_session = True
        session = _path(base, session_name, f"sessions/account_{slot}")
        if _session_file(session).is_dir():
            raise ValueError(f"{session_name} must identify a session file, not a directory")
        legacy_session = None
        if slot == 1 and not explicit_session and not _session_file(session).exists():
            previous = base / account
            if _session_file(previous).is_file():
                legacy_session = previous

        database_name = prefix + "DATABASE_PATH"
        database_default = f"data/activity_{account}.sqlite3"
        if slot == 1 and database_name not in os.environ:
            if "DATABASE_PATH" in os.environ:
                database_name = "DATABASE_PATH"
            elif (base / f"activity_{account}.sqlite3").is_file():
                database_default = f"activity_{account}.sqlite3"
        database = _path(base, database_name, database_default)
        accounts.append(Settings(
            api_id=api_id, api_hash=api_hash, chat_id=chat_id, account_id=account,
            session_path=session, database_path=database,
            legacy_session_path=legacy_session,
            persona=PERSONAS[slot - 1],
            automation_disclosed=_boolean(key("AUTOMATION_DISCLOSED"), "false"), **common,
        ))

    coordinator = _path(base, "COORDINATOR_DATABASE_PATH", "data/coordinator.sqlite3")
    identities: set[str] = set()
    occupied: dict[str, str] = {_canonical(coordinator): "COORDINATOR_DATABASE_PATH"}
    for account in accounts:
        if account.account_id in identities:
            raise ValueError("ACCOUNT_ID conflicts with another enabled account slot")
        identities.add(account.account_id)
        paths = [(account.database_path, f"{account.account_id} database"),
                 (_session_file(account.session_path), f"{account.account_id} session")]
        if account.legacy_session_path is not None:
            paths.append((_session_file(account.legacy_session_path), f"{account.account_id} legacy session"))
        for path, label in paths:
            canonical = _canonical(path)
            if canonical in occupied:
                raise ValueError(f"Separate paths required: {label} conflicts with {occupied[canonical]}")
            occupied[canonical] = label

    return ApplicationSettings(
        accounts=tuple(accounts), coordinator_path=coordinator,
        groq_concurrency=max(1, min(3, _number("GROQ_CONCURRENCY", "1"))),
        group_max_actions=max(1, min(25, _number("GROUP_MAX_ACTIONS_PER_HOUR", "25"))),
        restart_delay=max(30, _number("ACCOUNT_RESTART_DELAY_SECONDS", "30")),
        group_automation_allowed=_boolean("GROUP_AUTOMATION_ALLOWED", "false"),
        groq_rpm=max(1, _number("GROQ_REQUESTS_PER_MINUTE", "20")),
        groq_rpd=max(1, _number("GROQ_REQUESTS_PER_DAY", "1000")),
        groq_tpm=max(1, _number("GROQ_TOKENS_PER_MINUTE", "10000")),
        groq_tpd=max(1, _number("GROQ_TOKENS_PER_DAY", "200000")),
    )
