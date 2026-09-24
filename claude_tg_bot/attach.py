"""Attachment handling: type detection, size, deletion, textual descriptions.

Only pure functions over files and attachment objects. The attachment handlers
(on_photo/on_video etc.) live in handlers.py because they launch Claude.
"""

import shutil
import subprocess
from pathlib import Path

from . import config, i18n


# Extension by mime_type (for objects without file_name — e.g. Voice).
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "application/pdf": ".pdf",
}


def _attach_ext(att) -> str:
    """Extension for a downloaded attachment (Claude infers its type from it).

    Priority: extension from file_name (if any) → else from mime_type →
    else the ".bin" default. Voice has no file_name, so its mime_type is used.
    """
    fname = getattr(att, "file_name", None)
    if fname and "." in fname:
        suffix = Path(fname).suffix.lower()
        if suffix:
            return suffix
    mime = getattr(att, "mime_type", None)
    if mime:
        ext = _MIME_EXT.get(mime.lower())
        if ext:
            return ext
    return ".bin"


def _sniff_image_ext(path: Path) -> str:
    """Image extension by content (magic bytes), fallback .jpg.

    Pyrogram's photo (Photo) object does NOT carry file_name/mime_type, so we
    detect the real image format (PNG/WebP/GIF/JPEG) from the first bytes of the
    downloaded file. If we can't detect it — assume .jpg (as before).
    """
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".jpg"


def _delete_path(path: Path, mode: str = "") -> None:
    """Delete a file/folder per DELETE_MODE: to trash or permanently.

    mode: 'trash' (macOS Trash, restorable) | 'permanent' (gone for good).
    If mode is not trash → permanent. For trash we prefer send2trash, falling
    back to osascript (Finder) when it's missing. For folders — rmtree/unlink.
    """
    mode = (mode or config.DELETE_MODE or "trash").lower()
    if mode == "trash":
        try:
            from send2trash import send2trash  # local import (optional dep)
            send2trash(str(path))
            return
        except Exception:
            pass
        # Fallback without send2trash: osascript Finder on macOS.
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "Finder" to delete POSIX file "{path}"'],
                check=False, capture_output=True,
            )
            return
        except Exception:
            pass
    # permanent (or failed to trash): delete for real.
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _fmt_bytes(n: int) -> str:
    """Nicely format bytes: 512 B / 4.2 KB / 1.3 MB / 2.1 GB."""
    for unit_key, div in (("uni.GB", 1024**3), ("uni.MB", 1024**2), ("uni.KB", 1024)):
        if n >= div:
            val = n / div
            return f"{val:.1f} {i18n.t(unit_key)}"
    return f"{n} {i18n.t('uni.B')}"


def _has_any_attach(message) -> bool:
    g = getattr
    return bool(
        g(message, "photo", None) or g(message, "video", None)
        or g(message, "video_note", None)
        or g(message, "audio", None) or g(message, "voice", None)
        or g(message, "document", None)
        or g(message, "animation", None) or g(message, "sticker", None)
        or g(message, "contact", None) or g(message, "venue", None)
        or g(message, "location", None)
        or g(message, "poll", None) or g(message, "dice", None)
        or g(message, "game", None)
        or g(message, "web_app_data", None) or g(message, "paid_media", None)
    )


def _attach_type_name(message) -> str:
    """A human name for the attachment type, for the "can't process" message.

    For recognized photo/video/audio/file the exact name is returned; for others
    (sticker, geo, poll, etc.) by presence. If none — None.
    """
    g = getattr
    if g(message, "photo", None):
        return i18n.t("attach.type_photo")
    if g(message, "video", None):
        return i18n.t("attach.type_video")
    if g(message, "video_note", None):
        return i18n.t("attach.type_video_note")
    if g(message, "audio", None):
        return i18n.t("attach.type_audio")
    if g(message, "voice", None):
        return i18n.t("attach.type_voice")
    if g(message, "document", None):
        return i18n.t("attach.type_file")
    if g(message, "animation", None):
        return i18n.t("attach.type_animation")
    if g(message, "sticker", None):
        return i18n.t("attach.type_sticker")
    if g(message, "contact", None):
        return i18n.t("attach.type_contact")
    if g(message, "location", None) or g(message, "venue", None):
        return i18n.t("attach.type_location")
    if g(message, "poll", None):
        return i18n.t("attach.type_poll")
    if g(message, "dice", None) or g(message, "game", None):
        return i18n.t("attach.type_game")
    if g(message, "web_app_data", None):
        return i18n.t("attach.type_webapp")
    if g(message, "paid_media", None):
        return i18n.t("attach.type_paid")
    return i18n.t("attach.type_unknown")


def _textual_attach_prompt(message) -> "str | None":
    """Build a textual description for file-less attachments (poll/geo/contact).

    Returns a ready prompt or None if the type is not a textual attachment. Needed so
    claude understands what the user sent and answers to the point.
    """
    g = getattr
    poll = g(message, "poll", None)
    if poll:
        q = g(poll, "question", None)
        question = getattr(q, "text", None) or str(q) if q else ""
        opts = [getattr(o, "text", None) or str(o) for o in (g(poll, "options", None) or [])]
        lines = [f"User started a poll: {question}".strip() or "User started a poll."]
        if opts:
            lines.append("Options:")
            lines += [f"  - {o}" for o in opts]
        return "\n".join(lines)

    contact = g(message, "contact", None)
    if contact:
        name = " ".join(x for x in (getattr(contact, "first_name", ""), getattr(contact, "last_name", "")) if x)
        phone = getattr(contact, "phone_number", "") or ""
        return f"User shared a contact: {name} ({phone})".strip()

    location = g(message, "location", None) or g(message, "venue", None)
    if location:
        lat = getattr(location, "latitude", "")
        lon = getattr(location, "longitude", "")
        title = getattr(location, "title", "") or ""
        addr = getattr(location, "address", "") or ""
        base = f"Geolocation: {lat}, {lon}"
        if title:
            base = f"{base} — {title}"
        if addr:
            base = f"{base} ({addr})"
        return base

    return None