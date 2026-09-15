"""Работа с вложениями: определение типа, размер, удаление, текстовые описания.

Только чистые функции над файлами и объектами вложения. Обработчики вложений
(on_photo/on_video и т.п.) живут в handlers.py, т.к. запускают Claude.
"""

import shutil
import subprocess
from pathlib import Path

from . import config


# Расширение по mime_type (для объекта без file_name — напр. Voice).
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


def _media_ext(media) -> str:
    """Расширение для скачиваемого вложения (Claude определяет тип по нему).

    Приоритет: расширение из file_name (если есть) → иначе из mime_type →
    иначе дефолт ".bin". Voice не имеет file_name, поэтому для него mime_type.
    """
    fname = getattr(media, "file_name", None)
    if fname and "." in fname:
        suffix = Path(fname).suffix.lower()
        if suffix:
            return suffix
    mime = getattr(media, "mime_type", None)
    if mime:
        ext = _MIME_EXT.get(mime.lower())
        if ext:
            return ext
    return ".bin"


def _sniff_image_ext(path: Path) -> str:
    """Расширение изображения по содержимому (магические байты), fallback .jpg.

    У Pyrogram объект photo (Photo) НЕ несёт file_name/mime_type, поэтому
    реальный формат картинки (PNG/WebP/GIF/JPEG) определяем по первым байтам
    скачанного файла. Если не распознали — считаем .jpg (как раньше).
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
    """Удалить файл/каталог по DELETE_MODE: в корзину (trash) или навсегда.

    mode: 'trash' (корзина macOS, restore-able) | 'permanent' (навсегда).
    Если mode не Trash → навсегда. Для trash предпочитаем send2trash, при его
    отсутствии — через osascript (Finder). Для каталогов — rmtree/unlink.
    """
    mode = (mode or config.DELETE_MODE or "trash").lower()
    if mode == "trash":
        try:
            from send2trash import send2trash  # локальный импорт (опционально)
            send2trash(str(path))
            return
        except Exception:
            pass
        # Фолбэк без send2trash: osascript Finder на macOS.
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "Finder" to delete POSIX file "{path}"'],
                check=False, capture_output=True,
            )
            return
        except Exception:
            pass
    # permanent (или не удалось в корзину): удаляем насмерть.
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _dir_size(path: Path) -> int:
    """Суммарный размер (байты) всех файлов внутри каталога path (рекурсивно)."""
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _fmt_bytes(n: int) -> str:
    """Красиво отформатировать байты: 512 Б / 4.2 КБ / 1.3 МБ / 2.1 ГБ."""
    for unit, div in (("ГБ", 1024**3), ("МБ", 1024**2), ("КБ", 1024)):
        if n >= div:
            val = n / div
            return f"{val:.1f} {unit}"
    return f"{n} Б"


def _has_any_media(message) -> bool:
    """Есть ли в сообщении вложение любого типа (не только распознанные)."""
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


def _media_type_name(message) -> str:
    """Человеческое имя типа вложения для сообщения «не могу обработать».

    Для распознанных фото/видео/звука/файла возвращается точное имя; для
    прочих (стикер, гео, опрос и т.п.) — по наличию. Если ничего нет — None.
    """
    g = getattr
    if g(message, "photo", None):
        return "фото"
    if g(message, "video", None):
        return "видео"
    if g(message, "video_note", None):
        return "видеосообщение (кружок)"
    if g(message, "audio", None):
        return "аудио"
    if g(message, "voice", None):
        return "голосовое"
    if g(message, "document", None):
        return "файл"
    if g(message, "animation", None):
        return "анимация (GIF)"
    if g(message, "sticker", None):
        return "стикер"
    if g(message, "contact", None):
        return "контакт"
    if g(message, "location", None) or g(message, "venue", None):
        return "геопозиция"
    if g(message, "poll", None):
        return "опрос"
    if g(message, "dice", None) or g(message, "game", None):
        return "игра/анимация"
    if g(message, "web_app_data", None):
        return "web-app данные"
    if g(message, "paid_media", None):
        return "платный медиафайл"
    return "неизвестное вложение"


def _textual_media_prompt(message) -> "str | None":
    """Сформировать текст-описание для медиа без файла (опрос/гео/контакт).

    Возвращает готовый промпт или None, если тип не textual-медиа. Нужно,
    чтобы claude понял, что прислал пользователь, и ответил по сути.
    """
    g = getattr
    poll = g(message, "poll", None)
    if poll:
        q = g(poll, "question", None)
        question = getattr(q, "text", None) or str(q) if q else ""
        opts = [getattr(o, "text", None) or str(o) for o in (g(poll, "options", None) or [])]
        lines = [f"Пользователь запустил опрос: {question}".strip() or "Пользователь запустил опрос."]
        if opts:
            lines.append("Варианты:")
            lines += [f"  - {o}" for o in opts]
        return "\n".join(lines)

    contact = g(message, "contact", None)
    if contact:
        name = " ".join(x for x in (getattr(contact, "first_name", ""), getattr(contact, "last_name", "")) if x)
        phone = getattr(contact, "phone_number", "") or ""
        return f"Пользователь поделился контактом: {name} ({phone})".strip()

    location = g(message, "location", None) or g(message, "venue", None)
    if location:
        lat = getattr(location, "latitude", "")
        lon = getattr(location, "longitude", "")
        title = getattr(location, "title", "") or ""
        addr = getattr(location, "address", "") or ""
        base = f"Геопозиция: {lat}, {lon}"
        if title:
            base = f"{base} — {title}"
        if addr:
            base = f"{base} ({addr})"
        return base

    return None