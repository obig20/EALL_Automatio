"""Small shared formatting and time helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("Africa/Nairobi")


def now_iso() -> str:
    return datetime.now(BUSINESS_TZ).isoformat(timespec="seconds")


def format_duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    seconds = max(0, int(round(seconds)))
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {remaining_seconds}s"
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def clip_text(value: str, limit: int = 3500) -> str:
    """Keep Telegram messages within a safe length while marking truncation."""
    if len(value) <= limit:
        return value
    return value[: limit - 18].rstrip() + "\n… [truncated]"