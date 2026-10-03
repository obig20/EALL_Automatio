"""Bounded retry policy for clearly transient network/service failures."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from tenacity import (
    AsyncRetrying,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

T = TypeVar("T")


def is_transient_error(error: BaseException) -> bool:
    if isinstance(
        error,
        (
            TimeoutError,
            ConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ),
    ):
        return True
    if hasattr(error, "retry_after"):
        return True
    status = getattr(error, "status_code", None) or getattr(error, "code", None)
    try:
        status = int(status)
    except (TypeError, ValueError):
        return error.__class__.__name__.lower() in {
            "servererror",
            "resourceexhausted",
            "networkerror",
            "timedout",
        }
    return status in {408, 425, 429, 500, 502, 503, 504}


def with_transient_retry(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    wait_min_seconds: int = 4,
    wait_max_seconds: int = 10,
) -> T:
    """Retry only transient failures, with a bounded exponential delay."""
    retryer = Retrying(
        retry=retry_if_exception(is_transient_error),
        wait=wait_exponential(
            multiplier=1, min=wait_min_seconds, max=wait_max_seconds
        ),
        stop=stop_after_attempt(attempts),
        reraise=True,
    )
    return retryer(operation)


async def with_transient_retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    wait_min_seconds: int = 4,
    wait_max_seconds: int = 10,
) -> T:
    """Async counterpart used for Telegram network operations."""
    retryer = AsyncRetrying(
        retry=retry_if_exception(is_transient_error),
        wait=wait_exponential(
            multiplier=1, min=wait_min_seconds, max=wait_max_seconds
        ),
        stop=stop_after_attempt(attempts),
        reraise=True,
    )
    return await retryer(operation)