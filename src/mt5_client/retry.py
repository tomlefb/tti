"""Exponential-backoff retry helper for transient MT5 failures.

Wraps a callable in N attempts with delays ``base_delay × 2^k``. Each
failed attempt is logged at WARNING level so the operator can inspect
the rotating log after the fact.

Used to wrap :meth:`MT5Client.fetch_ohlc` and
:meth:`MT5Client.get_recent_trades` per docs/04 §"Error handling":
"MT5 disconnects: retry with exponential backoff (1s, 2s, 4s, 8s, 16s,
give up)".

``connect()`` is **not** wrapped — the scheduler bootstrap decides
whether to abort on connect failure.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` with exponential-backoff retry.

    Sequence: attempt → on failure sleep ``base_delay × 2^attempt`` → retry.
    Defaults yield 1s, 2s, 4s, 8s, 16s = 31s total before giving up.

    Args:
        fn: zero-arg callable to invoke. Use ``functools.partial`` to bind
            arguments at the call site.
        max_attempts: total number of attempts (≥ 1). At ``max_attempts``
            the last raised exception propagates.
        base_delay: seconds before the first retry.
        sleep: injection point for tests — defaults to ``time.sleep``.

    Returns:
        Whatever ``fn`` returns on the first successful attempt.

    Raises:
        Whatever ``fn`` raised on its final attempt.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — caller controls scope
            last_exc = exc
            if attempt + 1 == max_attempts:
                logger.error(
                    "with_retry: giving up after %d attempts — last error: %r",
                    max_attempts,
                    exc,
                )
                raise
            delay = base_delay * (2**attempt)
            logger.warning(
                "with_retry: attempt %d/%d failed (%r) — retrying in %.1fs",
                attempt + 1,
                max_attempts,
                exc,
                delay,
            )
            sleep(delay)

    # Unreachable but keeps the type-checker happy.
    raise RuntimeError("with_retry exhausted without raising") from last_exc
