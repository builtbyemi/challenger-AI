"""Startup stage tracking and diagnostics without exception bodies or locals."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3


class InstanceAlreadyRunning(RuntimeError):
    def __init__(self, scope="session"):
        self.scope = scope
        super().__init__("Another Challenger process holds this runtime lock")


@contextmanager
def startup_stage(name):
    try:
        yield
    except Exception as exc:
        if not hasattr(exc, "challenger_stage"):
            exc.challenger_stage = name
        raise


def failure_summary(exc, default_stage="cli_startup"):
    stage = getattr(exc, "challenger_stage", default_stage)
    if isinstance(exc, InstanceAlreadyRunning):
        detail = (f"{exc.scope} lock is held by another running instance. "
                  "Stop the existing Challenger terminal with Ctrl+C and wait for exit, then retry. "
                  "Do not delete lock files, sessions or databases.")
    elif stage == "live_validation":
        detail = ("Live mode requires GROUP_AUTOMATION_ALLOWED=true and each account's "
                  "AUTOMATION_DISCLOSED=true. Use --preview for safe preview; do not bypass group permission.")
    elif isinstance(exc, PermissionError):
        detail = "Access denied; check runtime directory permissions and file access."
    elif isinstance(exc, sqlite3.Error):
        detail = "SQLite initialization/access failed; check competing processes, disk space and database permissions. Preserve existing files."
    elif isinstance(exc, TimeoutError):
        detail = "Operation timed out; check network connectivity and retry after provider backoff."
    else:
        detail = "Operation failed; inspect the named stage and source location. Exception contents omitted for privacy."
    # Locations are useful even for unknown failures. Never include source lines,
    # exception messages, absolute paths or traceback locals.
    location = ""
    tb = exc.__traceback__
    while tb is not None:
        filename = Path(tb.tb_frame.f_code.co_filename).name
        if filename in {"main.py", "application.py", "telegram_app.py", "config.py", "auth.py",
                        "coordinator.py", "usage.py", "diagnostics.py", "groq_client.py"}:
            location = f" source={filename}:{tb.tb_lineno}"
        tb = tb.tb_next
    return f"stage={stage} error={type(exc).__name__}{location}; {detail}"
