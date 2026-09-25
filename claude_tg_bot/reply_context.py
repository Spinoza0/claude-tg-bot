"""Reply quote context: feed the quoted message content (text + attachments) to Claude.

When the user replies to a message, we walk the quote chain upwards
(message.reply_to_message → its own reply_to_message → ...) and collect a
context block + the quoted attachments, so Claude sees both the user's question
and what it refers to.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pyrogram.types import Message

from . import i18n
from .access import _author
from .attach import (
    _attach_ext,
    _attach_type_name,
    _has_any_attach,
    _sniff_image_ext,
    _textual_attach_prompt,
)

# How far up the quote chain we follow. Hard-coded (not a config value): big
# enough for real conversations, but bounded so a long/cyclic chain can't flood
# the prompt or loop forever.
MAX_QUOTE_DEPTH = 20

# Truncate a quoted text/caption so a huge message doesn't flood the prompt.
QUOTE_TEXT_MAX_LEN = 4000


@dataclass
class ReplyContext:
    """The quoted context to feed Claude alongside the user's own text."""

    text_block: str = ""      # the ready prompt block (author + type + text + paths)
    image_paths: list = field(default_factory=list)  # downloaded quoted attachments
    has_reply: bool = False   # True when there was a reply_to_message


def _author_label(message: Message) -> str:
    """A human label for the quoted message's author (name, else @username, else id)."""
    u = message.from_user
    if u is not None:
        name = (u.first_name or "") + (" " + u.last_name if u.last_name else "")
        name = name.strip()
        if name:
            return name
        if u.username:
            return f"@{u.username}"
        return str(u.id)
    return str(message.chat.id if message.chat else "?")


def _type_kind(message: Message) -> Optional[str]:
    """A plain type word for the quote line ('text', 'photo', 'video', ...).

    Returns None if the message has no recognizable content.
    """
    if message.text or message.caption:
        return "text"
    if _has_any_attach(message):
        # Use the localized type name; fall back to a bare word if it's absent.
        return _attach_type_name(message)
    return None


def _truncate(text: str) -> str:
    if len(text) <= QUOTE_TEXT_MAX_LEN:
        return text
    return text[:QUOTE_TEXT_MAX_LEN].rstrip() + "…"


async def _download_quoted_attachment(msg: Message, attach_dir: Path) -> Optional[Path]:
    """Download the quoted message's attachment into attach_dir; returns the path."""
    photo = getattr(msg, "photo", None)
    if photo is not None:
        # Photo has no mime/name — download neutral, then refine by content.
        path = attach_dir / f"quote_photo_{time.time_ns()}.img"
        try:
            await msg.download(file_name=str(path))
        except Exception:
            return None
        real_ext = _sniff_image_ext(path)
        if real_ext != ".img":
            renamed = path.with_suffix(real_ext)
            try:
                path.rename(renamed)
                path = renamed
            except OSError:
                pass
        return path

    att = getattr(msg, "video", None) or getattr(msg, "video_note", None) \
        or getattr(msg, "audio", None) or getattr(msg, "voice", None) \
        or getattr(msg, "document", None) or getattr(msg, "animation", None) \
        or getattr(msg, "sticker", None)
    if att is None:
        return None
    ext = _attach_ext(att)
    if ext == ".bin" and getattr(msg, "video_note", None):
        ext = ".mp4"
    path = attach_dir / f"quote_{time.time_ns()}{ext}"
    try:
        await msg.download(file_name=str(path))
    except Exception:
        return None
    return path


async def collect_reply_context(message: Message, cwd: Path) -> ReplyContext:
    """Walk the quote chain and build the context block + quoted attachment paths.

    cwd — Claude's working directory (the project root); the quoted attachments
    are downloaded into its `.claude_tg_bot_attach` subfolder (same cleanup rules
    as the current-message attachments).
    """
    quoted = getattr(message, "reply_to_message", None)
    if quoted is None:
        return ReplyContext(has_reply=False)

    attach_dir = cwd / ".claude_tg_bot_attach"
    attach_dir.mkdir(parents=True, exist_ok=True)

    parts: list[str] = []
    image_paths: list[Path] = []
    seen: set[int] = set()
    msg: Optional[Message] = quoted
    depth = 0
    while msg is not None and depth < MAX_QUOTE_DEPTH:
        depth += 1
        # Guard against a cyclic quote chain (an unlikely Telegram edge case).
        mid = getattr(msg, "id", None)
        if mid is not None and mid in seen:
            break
        if mid is not None:
            seen.add(mid)

        kind = _type_kind(msg)
        body = (msg.text or msg.caption or "").strip()
        if body:
            body = _truncate(body)
        # File-less content (poll/geo/contact) — build a textual description.
        if not body and kind and kind != "text":
            textual = _textual_attach_prompt(msg)
            if textual:
                body = _truncate(textual)

        # Download the quoted attachment (if any) so Claude can read it as a file.
        path = await _download_quoted_attachment(msg, attach_dir)
        if path is not None:
            image_paths.append(path)
            path_mark = f" @{path}"
        else:
            path_mark = ""

        label = _author_label(msg)
        headline = f"[{kind or 'message'}] by {label}"
        detail = f": {body}" if body else ""
        parts.append(f"{headline}{detail}{path_mark}")

        msg = getattr(msg, "reply_to_message", None)

    header = i18n.t("reply.context_header")
    text_block = header + "\n" + "\n".join(parts) if parts else ""
    return ReplyContext(text_block=text_block, image_paths=image_paths, has_reply=True)