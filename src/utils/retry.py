"""Exponential backoff with jitter using tenacity."""

from __future__ import annotations

from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

import structlog

log = structlog.get_logger(__name__)


def _log_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    log.warning(
        "retry_attempt",
        attempt=retry_state.attempt_number,
        error=str(exc) if exc else None,
        fn=retry_state.fn.__name__ if retry_state.fn else "unknown",
    )


def exchange_retry(max_attempts: int = 3):
    """Decorator for exchange API calls: retry on network/timeout errors."""
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=0.5, max=30, jitter=5),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
        before_sleep=_log_retry,
        reraise=True,
    )
