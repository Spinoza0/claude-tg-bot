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


def reset_media_sessions(client) -> None:
    """Drop Pyrogram's cached media sessions so they reconnect fresh.

    A media session that failed to reach STARTED stays in client.media_sessions
    as a dead session; get_session() returns it as-is, so every download then
    times out with "Waited 15s ... is not started" until the bot restarts.
    Clearing the cache forces Pyrogram to create a new session on the next
    download.
    """
    sessions = getattr(client, "media_sessions", None)
    if not sessions:
        return
    for sess in list(sessions.values()):
        try:
            sess.stop()
        except Exception:
            pass
    sessions.clear()


async def download_media_with_retry(client, message, path):
    """Download message media, retrying on a transient connect failure.

    On a media-session connect failure (the session timed out reaching STARTED,
    usually a network/proxy hiccup on a lazy first download) the dead session is
    dropped so the next attempt reconnects. Short fixed pause between attempts.
    Returns None on success, else the last exception (for the failure message).
    """
    last_err = None
    for attempt in range(1, config.MESSAGE_RETRY_LIMIT + 1):
        try:
            await message.download(file_name=str(path))
            return None
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            last_err = e
            if attempt < config.MESSAGE_RETRY_LIMIT:
                reset_media_sessions(client)
                await asyncio.sleep(config.MESSAGE_RETRY_DELAY)
    return last_err