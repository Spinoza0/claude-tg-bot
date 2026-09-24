"""Sending bot messages: unified format + retries on transient Telegram errors."""

from pathlib import Path

from pyrogram.types import Message

from . import config, i18n
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


# Extension → send method in Telegram. The 'document' fallback covers everything
# else (PDF, archives, spreadsheets, ...).
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
_AUDIO_EXT = {".mp3", ".ogg", ".opus", ".m4a", ".wav", ".flac"}


def _attachment_kind(path: str) -> str:
    """The send kind for a file by extension: 'photo', 'video', 'audio', 'document'."""
    ext = Path(path).suffix.lower()
    if ext in _IMAGE_EXT:
        return "photo"
    if ext in _VIDEO_EXT:
        return "video"
    if ext in _AUDIO_EXT:
        return "audio"
    return "document"


async def _send_attachment(client, message: Message, path):
    """Send a result file back to Telegram, choosing the method by extension.

    Uses the same retry mechanism as text (transient Telegram errors). Returns
    None on success; on an unrecoverable error returns an error string (so the
    caller can inform the user without breaking the rest of the handling).

    _run_with_retry returns the sent Message on success, or None when every
    attempt failed — so we map it to (success -> None, failure -> error text).
    """
    kind = _attachment_kind(str(path))
    sent = await _run_with_retry(
        lambda: _send_attachment_once(client, message, path, kind),
        limit=config.MESSAGE_RETRY_LIMIT,
        base_delay=config.MESSAGE_RETRY_DELAY,
        multiplier=1.0,
        max_delay=config.MESSAGE_RETRY_DELAY,
    )
    if sent is None:  # every attempt failed
        return i18n.t("handlers.attachment_send_failed")
    return None


async def _send_attachment_once(client, message: Message, path, kind: str):
    """The actual Pyrogram send for one attachment. Raises on error (retried)."""
    chat_id = message.chat.id
    if kind == "photo":
        return await client.send_photo(chat_id, str(path))
    if kind == "video":
        return await client.send_video(chat_id, str(path))
    if kind == "audio":
        return await client.send_audio(chat_id, str(path))
    return await client.send_document(chat_id, str(path))