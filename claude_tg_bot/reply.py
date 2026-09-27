"""Sending bot messages: unified format + retries on transient Telegram errors."""

import logging
import re
import time
from contextvars import ContextVar
from pathlib import Path

from pyrogram.types import Message

from . import config, i18n
from .retry import _run_with_retry

logger = logging.getLogger("claude_tg_bot")

# Telegram caps one message at 4096 chars. Keep a margin and split on paragraph
# boundaries so we don't cut a thought mid-sentence.
_MSG_LIMIT = 4000

# Ids of messages the bot has sent (as replies or attachments), with the send
# time, used by the anti-loop filter (on_all_message): an incoming outgoing
# message whose id is here is the bot's own answer — not a request from the owner
# (who shares the same account in a userbot), so we must not process it.
# A TTL discards stale ids so the dict doesn't grow forever.
_BOT_SENT: "dict[int, float]" = {}
_SENT_TTL = 3600.0  # drop ids older than an hour


def register_sent(message: Message) -> None:
    """Remember an id of a message the bot just sent (for the anti-loop filter)."""
    mid = getattr(message, "id", None)
    if mid is not None:
        _prune_sent()
        _BOT_SENT[mid] = time.time()


def _prune_sent() -> None:
    """Drop ids older than _SENT_TTL (called on each register, keeps the dict small)."""
    now = time.time()
    stale = [i for i, ts in _BOT_SENT.items() if now - ts > _SENT_TTL]
    for i in stale:
        _BOT_SENT.pop(i, None)


def is_bot_message(message: Message) -> bool:
    """Whether a message was sent by the bot itself (its id is in the register)."""
    return getattr(message, "id", None) in _BOT_SENT


# The active working directory for path masking, set per-message by the handlers
# so every send (reply/attachment/error) hides this real path even when a call
# site doesn't pass `cwd` explicitly. A ContextVar scopes it to the message being
# handled so it never leaks into the next one.
_active_cwd: ContextVar[str] = ContextVar("active_cwd", default="")


def set_active_cwd(path: str) -> None:
    """Remember the working dir to mask in all sends while handling this message."""
    _active_cwd.set(path or "")


# Working directory of the current Claude run (a project or a sandbox project).
# A send helper hides the absolute path of that working dir when it appears in a
# message, so the user's home path never leaks into replies. `show_name` keeps
# just the project's name (for /status, /help); otherwise the path is dropped to
# "/". Both "<cwd>/..." and "<cwd>" at a fragment boundary are matched.
def _mask_roots() -> list[str]:
    """The broader filesystem roots also hidden in outbound messages.

    Besides the active working dir, a real home/projects/sandbox root must never
    leak: a path under one of them is shown relative to it (leading slash kept),
    so "/home/user/projects/wb/test/file" reads as "/wb/test/file". This covers
    paths outside the active project too.
    """
    out = []
    for r in (config.PROJECTS_ROOT, config.SANDBOX_ROOT, Path.home()):
        s = str(r)
        if s and s not in out:
            out.append(s)
    return out


def _mask_cwd(text: str, cwd: str, show_name: bool = False) -> str:
    """Hide the real filesystem prefix of a path, keeping only the project.

    cwd — the current working directory (a project or a sandbox project). A path
    that starts with "cwd/" is shown from the working dir: ".../helper/.claude_tg_bot_attach/a.ogg"
    -> "/.claude_tg_bot_attach/a.ogg"; with show_name it keeps the project name,
    -> "/helper/.claude_tg_bot_attach/a.ogg". The exact "cwd" value is matched both
    with a trailing "/" and at a fragment boundary (end of string, before a
    space/./newline), so a path at the end of a line is hidden too. The broader
    roots (projects/sandbox/home) are also replaced so no real path leaks.
    """
    if not cwd:
        return text
    root = str(cwd)
    # The replacement for "<cwd>": "/" hides the whole path; "/<name>" keeps the
    # project's name (for /status, /help).
    bare = f"/{Path(root).name}" if show_name else "/"
    # "<cwd>/X" -> "<bare>/X": a path under the working dir is shown from the dir
    # (or from the project name when show_name is set).
    under = bare + "/" if show_name else "/"
    text = re.sub(re.escape(root + "/"), under, text)
    text = re.sub(re.escape(root) + r"(?=$|[\s.\n])", bare, text)
    # Mask the broader roots (projects/sandbox/home): show a path relative to it.
    for r in _mask_roots():
        if not r or r == root:
            continue
        text = re.sub(re.escape(r + "/"), "/", text)
        text = re.sub(re.escape(r) + r"(?=$|[\s.\n])", "/", text)
    return text


async def _reply(message: Message, text: str, cwd: str = "", show_name: bool = False):
    """Send a message with the bot's 🤖-prefix, the common shape of all replies."""
    # If no cwd is passed, mask the active working dir of the message being
    # handled (set via set_active_cwd) — so every send hides the real path.
    if not cwd:
        cwd = _active_cwd.get()
    if cwd:
        text = _mask_cwd(text, cwd, show_name)
    sent = await message.reply_text(f"🤖 {text}")
    register_sent(sent)
    return sent


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


async def _send_split(message: Message, text: str, attempts: int | None = None, delay: float | None = None, cwd: str = ""):
    """Send text, splitting into several messages when it exceeds Telegram's limit.

    Used when Claude's answer is longer than the Telegram limit: instead of
    truncating, the full text is delivered in chunks.
    """
    for chunk in _split_text(text):
        await _send_with_retry(message, chunk, attempts, delay, cwd)


async def _send_with_retry(message: Message, text: str, attempts: int | None = None, delay: float | None = None, cwd: str = ""):
    """Send a reply, retrying on transient Telegram errors.

    Telegram sometimes answers with a data-center internal error
    (e.g. [500 INTERDC_X_CALL_ERROR] — an inter-DC call in chats outside the
    main DC when running through an MTProto proxy). Then `reply_text` raises and
    the result is lost. Here we use the shared retry mechanism with short pauses
    (seconds). If every attempt fails we return None (rather than raise), so the
    rest of the handling is not broken.
    """
    return await _run_with_retry(
        lambda: _reply(message, text, cwd),
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
# Telegram's voice message accepts only OGG/Opus — the rest of the audio is sent
# as a regular audio (music) file.
_VOICE_EXT = {".ogg", ".opus"}
_AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac"}


def _attachment_kind(path: str) -> str:
    """The send kind for a file by extension: 'photo', 'video', 'voice', 'audio', 'document'."""
    ext = Path(path).suffix.lower()
    if ext in _IMAGE_EXT:
        return "photo"
    if ext in _VIDEO_EXT:
        return "video"
    if ext in _VOICE_EXT:
        return "voice"
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
    last_err: Exception | None = None

    async def _once():
        nonlocal last_err
        try:
            return await _send_attachment_once(client, message, path, kind)
        except Exception as e:  # keep the real reason for the failure report
            last_err = e
            raise

    sent = await _run_with_retry(
        _once,
        limit=config.MESSAGE_RETRY_LIMIT,
        base_delay=config.MESSAGE_RETRY_DELAY,
        multiplier=1.0,
        max_delay=config.MESSAGE_RETRY_DELAY,
    )
    if sent is None:  # every attempt failed
        logger.warning("Attachment %s (kind=%s) failed to send: %r", path, kind, last_err)
        return i18n.t("handlers.attachment_send_failed")
    return None


async def _send_attachment_once(client, message: Message, path, kind: str):
    """The actual Pyrogram send for one attachment. Raises on error (retried)."""
    chat_id = message.chat.id
    if kind == "photo":
        sent = await client.send_photo(chat_id, str(path))
    elif kind == "video":
        sent = await client.send_video(chat_id, str(path))
    elif kind == "voice":
        sent = await client.send_voice(chat_id, str(path))
    elif kind == "audio":
        sent = await client.send_audio(chat_id, str(path))
    else:
        sent = await client.send_document(chat_id, str(path))
    register_sent(sent)
    return sent