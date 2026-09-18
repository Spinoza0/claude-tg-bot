"""Sending bot messages: unified format + retries on transient Telegram errors."""

from pyrogram.types import Message

from . import config
from .retry import _run_with_retry

# Telegram caps one message at 4096 chars. Keep a margin and split on paragraph
# boundaries so we don't cut a thought mid-sentence.
_MSG_LIMIT = 4000


async def _reply(message: Message, text: str):
    """Send a message with the bot's 🤖-prefix, the common shape of all replies."""
    return await message.reply_text(f"🤖 {text}")


def _split_text(text: str) -> list[str]:
    """Split long text into chunks ≤ _MSG_LIMIT, preferring paragraph boundaries."""
    text = text.strip()
    if len(text) <= _MSG_LIMIT:
        return [text]
    chunks = []
    while len(text) > _MSG_LIMIT:
        cut = text[: _MSG_LIMIT]
        split_at = max(cut.rfind("\n"), cut.rfind(" "))
        if split_at <= 0:
            split_at = _MSG_LIMIT
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks


async def _send_split(message: Message, text: str, attempts: int | None = None, delay: float | None = None):
    """Send text, splitting into several messages when it exceeds Telegram's limit.

    Used when Claude's answer is longer than the Telegram limit: instead of
    truncating, the full text is delivered in chunks.
    """
    for chunk in _split_text(text):
        await _send_with_retry(message, chunk, attempts, delay)


async def _send_with_retry(message: Message, text: str, attempts: int | None = None, delay: float | None = None):
    """Send a reply, retrying on transient Telegram errors.

    Telegram sometimes answers with a data-center internal error
    (e.g. [500 INTERDC_X_CALL_ERROR] — an inter-DC call in chats outside the
    main DC when running through an MTProto proxy). Then `reply_text` raises and
    the result is lost. Here we use the shared retry mechanism with short pauses
    (seconds). If every attempt fails we return None (rather than raise), so the
    rest of the handling is not broken.
    """
    return await _run_with_retry(
        lambda: _reply(message, text),
        limit=attempts if attempts is not None else config.MESSAGE_RETRY_LIMIT,
        base_delay=delay if delay is not None else config.MESSAGE_RETRY_DELAY,
        # Sending is a transient error, so the pause stays constant (multiplier 1)
        # rather than growing; otherwise the reply would stall longer than needed.
        multiplier=1.0,
        max_delay=config.MESSAGE_RETRY_DELAY,
    )