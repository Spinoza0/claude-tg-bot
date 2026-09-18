"""Shared retry mechanism with a growing backoff (issue #6).

Common for long retries (Telegram connection, Claude calls) and overridden by
short ones (message sending). The pause after the n-th failure grows as
base * multiplier^(n-1), capped at max.
"""

import asyncio

from . import config


def _retry_backoff_delay(n: int) -> float:
    """Pause before the n-th retry: base * multiplier^(n-1), capped at max.

    n is counted from 1 (first retry): after the first failure — base, after the
    second — base*multiplier, and so on up to max. Returns seconds.
    """
    delay = config.RETRY_BASE_DELAY * (config.RETRY_MULTIPLIER ** (n - 1))
    return min(delay, config.RETRY_MAX_DELAY)


async def _run_with_retry(
    action,
    *,
    limit: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    multiplier: float | None = None,
):
    """Shared retry: calls action(), on failure waits a pause.

    The pause after the n-th failure grows as base*multiplier^(n-1), capped at
    max (by default from config; see _retry_backoff_delay). After `limit`
    attempts (including the first) returns None. Does not catch
    KeyboardInterrupt/CancelledError — interruption must always work.

    The limit/base_delay/max_delay/multiplier parameters override the config
    values: for short message-send retries they are set in seconds with
    multiplier=1.0 (constant pause); for long retries, minutes with growth.
    """
    limit = limit if limit is not None else config.RETRY_LIMIT
    for attempt in range(1, limit + 1):
        try:
            result = await action()
            return result
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception:
            if attempt < limit:
                if base_delay is not None:
                    await asyncio.sleep(
                        min(base_delay * (multiplier or 1.0) ** (attempt - 1), max_delay or base_delay)
                    )
                else:
                    await asyncio.sleep(_retry_backoff_delay(attempt))
    return None