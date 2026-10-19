"""Fixed quota hours align to UTC; daily reports retain their own timezone."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo


def window_start(now: float, mode: str = "fixed", timezone: str = "Africa/Lagos") -> float:
    if mode == "rolling":
        return now - 3600
    if mode != "fixed":
        raise ValueError("Unknown hourly window mode")
    return math.floor(now / 3600) * 3600


def day_start(now: float, timezone: str) -> float:
    return datetime.fromtimestamp(now, ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
