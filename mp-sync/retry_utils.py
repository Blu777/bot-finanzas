"""Small retry helper for transient external-service failures."""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


def call_with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    log: logging.Logger | None = None,
    label: str = "operation",
) -> T:
    """Run a callable with bounded exponential backoff and jitter."""
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_exceptions as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.25)
            if log:
                log.warning(
                    "%s failed (attempt %s/%s), retrying in %.1fs: %s",
                    label,
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
            time.sleep(delay)
    assert last_error is not None
    raise last_error
