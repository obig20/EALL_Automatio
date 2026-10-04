"""Process-local sliding-window rate limiting for inbound Telegram traffic.

This is deliberately simple flood protection, not a distributed rate
limiter: state lives in the running bot process and resets on restart.
It protects against one sender generating hundreds of inquiries
rapidly, founder-notification flooding, and accidental polling
reprocessing storms. Legitimate buyers sending a few messages per
hour are unaffected.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta

from .utils import BUSINESS_TZ


class RateLimiter:
    """Sliding-window limiter keyed by sender identifier."""

    def __init__(
        self,
        max_per_hour: int = 10,
        max_per_day: int = 50,
    ) -> None:
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day
        self._hourly: dict[str, deque[datetime]] = {}
        self._daily: dict[str, deque[datetime]] = {}
        self._lock = threading.Lock()

    def allow(self, sender_id: str, *, now: Optional[datetime] = None) -> bool:
        """Record a message from ``sender_id`` and report whether it is allowed."""
        timestamp = (now or datetime.now(BUSINESS_TZ)).astimezone(BUSINESS_TZ)
        with self._lock:
            hour_window = timestamp - timedelta(hours=1)
            day_window = timestamp - timedelta(days=1)
            hourly = self._hourly.setdefault(sender_id, deque())
            daily = self._daily.setdefault(sender_id, deque())
            while hourly and hourly[0] < hour_window:
                hourly.popleft()
            while daily and daily[0] < day_window:
                daily.popleft()
            allowed = True
            if self.max_per_hour and len(hourly) >= self.max_per_hour:
                allowed = False
            if self.max_per_day and len(daily) >= self.max_per_day:
                allowed = False
            if allowed:
                hourly.append(timestamp)
                daily.append(timestamp)
            return allowed

    def reset(self, sender_id: str) -> None:
        with self._lock:
            self._hourly.pop(sender_id, None)
            self._daily.pop(sender_id, None)

    def reset_all(self) -> None:
        with self._lock:
            self._hourly.clear()
            self._daily.clear()
