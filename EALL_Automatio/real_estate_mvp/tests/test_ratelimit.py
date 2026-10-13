"""Process-local rate limiter tests."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from real_estate_mvp.ratelimit import RateLimiter

TZ = ZoneInfo("Africa/Nairobi")


def test_normal_traffic_is_allowed():
    limiter = RateLimiter(max_per_hour=10, max_per_day=50)
    base = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)
    assert all(
        limiter.allow("buyer-1", now=base + timedelta(minutes=i))
        for i in range(10)
    )


def test_hourly_flood_is_blocked():
    limiter = RateLimiter(max_per_hour=3, max_per_day=50)
    base = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)
    for _ in range(3):
        assert limiter.allow("buyer-1", now=base)
    assert not limiter.allow("buyer-1", now=base)
    # A different sender is unaffected.
    assert limiter.allow("buyer-2", now=base)


def test_window_slides_forward():
    limiter = RateLimiter(max_per_hour=2, max_per_day=50)
    base = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)
    assert limiter.allow("buyer-1", now=base)
    assert limiter.allow("buyer-1", now=base)
    assert not limiter.allow("buyer-1", now=base)
    # Messages older than one hour fall out of the sliding window.
    assert limiter.allow("buyer-1", now=base + timedelta(hours=1, seconds=1))


def test_daily_limit_is_enforced():
    limiter = RateLimiter(max_per_hour=100, max_per_day=3)
    base = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)
    for index in range(3):
        assert limiter.allow("buyer-1", now=base + timedelta(minutes=index * 10))
    assert not limiter.allow("buyer-1", now=base + timedelta(minutes=50))


def test_zero_disables_a_limit():
    limiter = RateLimiter(max_per_hour=0, max_per_day=0)
    base = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)
    assert all(limiter.allow("buyer-1", now=base) for _ in range(100))


def test_reset_clears_sender_state():
    limiter = RateLimiter(max_per_hour=1, max_per_day=1)
    base = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)
    assert limiter.allow("buyer-1", now=base)
    assert not limiter.allow("buyer-1", now=base)
    limiter.reset("buyer-1")
    assert limiter.allow("buyer-1", now=base)
