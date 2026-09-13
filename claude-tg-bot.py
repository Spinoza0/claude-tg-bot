"""Telegram-бот на MTProto-клиенте поверх MTProto-прокси.

Запуск:
    python3 claude-tg-bot.py      (из директории проекта)

Бот работает как обычный аккаунт-клиент (userbot) через MTProto-протокол и не
требует доступа к api.telegram.org (Bot API) — это решает проблему
заблокированного Bot API. Если задан MT_PROXY (tg://proxy...) — ходит через
прокси; если нет — напрямую.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import pyrogram
from pyrogram import Client, filters
from pyrogram.types import Message

# Поддерживаем оба способа запуска:
#   python3 claude-tg-bot.py   (напрямую, без пакета)
if __package__ and __package__ != "__main__":
    from . import config
    from .claude_runner import format_command_hint, run_claude
    from .session import SessionStore, is_safe_project_name
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config  # noqa: E402
    from claude_runner import format_command_hint, run_claude  # noqa: E402
    from session import SessionStore, is_safe_project_name  # noqa: E402

# Глобальное хранилище состояний
store = SessionStore()

# ---------------------------------------------------------------------------
# Защита от двух экземпляров (single-instance): поиск уже запущенного процесса.
# Бот работает с одним .session (SQLite-базой) — если запустить второй
# экземпляр, тот не сможет открыть базу (sqlite3.OperationalError: database
# is locked). Поэтому при старте сканируем процессы системы и, если уже есть
# живой claude-tg-bot.py, пишем об этом и выходим (второй раз не запускаемся).
# ---------------------------------------------------------------------------

# Имя нашего файла-модуля — по нему ищем себя в списке процессов.
_BOT_SCRIPT = Path(__file__).resolve().name


def _get_bot_pids() -> list[int]:
    """PID всех запущенных экземпляров claude-tg-bot.py, кроме текущего.

    Сканируем `ps` по всей системе (не только потомков), чтобы поймать бота,
    запущенного из любого терминала/сессии. Match'им именно ЗАПУСК скрипта
    python-интерпретатором (`...python ...claude-tg-bot.py`), а не любое упоминание
    в командной строке шелла (иначе zsh/bash-обёртки с `source`/heredoc дают
    ложные срабатывания). Себя исключаем.
    """
    import re
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []
    my_pid = os.getpid()
    pids: list[int] = []
    # python/Python/python3, за которым идёт путь, заканчивающийся на наш скрипт.
    # (?i) — ловит и brew-'Python' (заглавная). \s+[^\s]+ — один аргумент-путь до скрипта.
    pattern = re.compile(
        rf"(?i)\bpython(?:3(?:\.\d+)?)?\s+[^\s]+\b{re.escape(_BOT_SCRIPT)}\b"
    )
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == my_pid:
            continue
        cmd = parts[1]
        # Исключаем шелл-обёртки (source/heredoc/-c), чтобы не ловить zsh/bash.
        if re.search(r"(^|\s)(zsh|bash|sh)(\s|$)", cmd):
            continue
        if pattern.search(cmd):
            pids.append(pid)
    return pids


# lock-файл для гашения ГОНКИ при почти-одновременном старте двух экземпляров.
# Создаётся атомарно (O_CREAT|O_EXCL) и удаляется через 2 секунды — поэтому
# не копит мусор на диске. После истечения защиту несёт проверка по процессам.
_LOCK_FILE = Path(__file__).resolve().parent / "claude-tg-bot.lock"


def _try_create_lock() -> bool:
    """Атомарно создать lock-файл (O_CREAT|O_EXCL) с нашим PID.

    Возвращает True, если lock получен нами. False — если файл уже существует
    (другой экземпляр стартует в эту же секунду), и мы должны выйти.
    """
    try:
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        # Кто-то уже занял lock прямо сейчас — это гонка, блокируем.
        return False
    except OSError:
        # Не смогли создать — не критично, полагаемся на проверку процессов.
        return True
    return True


def _schedule_lock_expiry(delay: float = 2.0) -> None:
    """Фоном удалить lock-файл через delay секунд, чтобы он не хранился зря."""
    def _rm():
        try:
            if _LOCK_FILE.exists() and _LOCK_FILE.read_text().strip() == str(os.getpid()):
                _LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        import threading
        threading.Timer(delay, _rm).start()
    except Exception:
        pass


def acquire_single_instance() -> bool:
    """Проверить, не запущен ли бот уже (процессы + атомарный lock-файл).

    Возвращает True, если можно стартовать; False — если бот уже работает
    (или стартует параллельно), и мы должны выйти.

    Двойная защита от двух экземпляров (оба работают с одним .session / SQLite):
      1) Атомарный lock-файл (создаётся O_EXCL, удаляется через 2 с) — гасит
         МГНОВЕННУЮ гонку, когда два бота стартуют одновременно.
      2) Сканирование процессов — ловит УЖЕ работающего бота.
    """
    if not _try_create_lock():
        print(f"⚠️  Бот уже запускается (lock-файл {_LOCK_FILE.name}). "
              "Второй экземпляр не запускается.")
        return False
    # Lock получен нами — запланируем автоудаление через 2 с.
    _schedule_lock_expiry()

    # Затем проверяем процессы: если бот уже давно работает, не стартуем.
    others = _get_bot_pids()
    if others:
        print(f"⚠️  Бот уже запущен (pid {', '.join(map(str, others))}). "
              "Второй экземпляр не запускается.")
        return False
    return True

# pid-группы claude-кластеров, ЗАПУЩЕННЫХ ЭТИМ БОТОМ. Нужны команде /kill,
# чтобы прибить только свои подвисшие сессии, а не все claude в системе.
# Ведётся в run_claude через param proc_registry (add на спавне / discard на выходе).
_bot_proc_pids: set[int] = set()

# Фоновые asyncio-задачи (вызовы claude). Храним, чтобы /kill мог их отменить.
_active_tasks: set["asyncio.Task"] = set()


def _is_allowed_user(user_id: int) -> bool:
    """Разрешён ли отправитель (ALLOWED_USERS)."""
    return user_id in config.ALLOWED_USERS


def _is_allowed_chat(user_id: int, chat_id: int) -> bool:
    """Разрешён ли чат для обычных сообщений.

    - Если ALLOWED_CHAT_IDS пуст — разрешён ТОЛЬКО чат, где chat_id
      равен твоему user_id (это Saved Messages / «Избранное»).
    - Если ALLOWED_CHAT_IDS задан — чат обязан быть в этом списке.
    """
    if config.ALLOWED_CHAT_IDS:
        return chat_id in config.ALLOWED_CHAT_IDS
    # Список пуст: разрешаем только чат, совпадающий с user_id
    # (в Saved Messages chat_id равен id твоего аккаунта).
    return chat_id == user_id


def _allowed(user_id: int, chat_id: int) -> bool:
    """Отвечать ли: отправитель разрешён И чат разрешён.

    Бот под твоей учёткой не должен отвечать везде, где ты пишешь.
    Обычные сообщения требуют и разрешённого юзера, и разрешённого чата
    (см. _is_allowed_chat). Для @helpbot чат НЕ ограничивается — только юзер
    (см. on_agent).
    """
    return _is_allowed_user(user_id) and _is_allowed_chat(user_id, chat_id)




def _author(message: Message) -> int:
    """user_id отправителя сообщения (надёжнее, чем chat.id в группах)."""
    if message.from_user is not None:
        return message.from_user.id
    # если отправитель не определён (напр. канал) — считаем chat.id
    return message.chat.id


async def _reply(message: Message, text: str):
    """Отправить сообщение от бота с префиксом-«головой робота» 🤖 + пробелом.

    Единый формат всех исходящих сообщений бота: эмодзи головы робота,
    пробел, затем само сообщение (🤖 текст). Используется во всех хендлерах,
    чтобы стиль был одинаковым.
    """
    return await message.reply_text(f"🤖 {text}")


async def _send_with_retry(message: Message, text: str, attempts: int = 3, delay: float = 2.0):
    """Отправить ответ с повторными попытками при транзиентной ошибке Telegram.

    Telegram временами отвечает внутренней ошибкой дата-центра
    (напр. [500 INTERDC_X_CALL_ERROR] — меж-DC вызов в чатах вне основного DC
    при работе через MTProto-прокси). Тогда `reply_text` бросает исключение, и
    результат теряется. Здесь делаем несколько попыток с паузой: первая же
    успешная отправка возвращает сообщение. Если все попытки провалились —
    возвращаем None (не бросаем), чтобы не уронить обработку дальше.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await _reply(message, text)
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                await asyncio.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# Единый статус в консоли вместо спама ошибок Pyrogram
# ---------------------------------------------------------------------------
#
# Pyrogram/Kurigram логирует свои ошибки подключения (gaierror, INTERDC… и
# ретраи) через стандартный logging с логгерами "pyrogram.*" и печатает их
# потоком в stderr. Это засоряет консоль и пугает. Вместо этого перехватываем
# логи Pyrogram своим Handler'ом: храним ПОСЛЕДНЮЮ ошибку, глушим потоковый
# вывод, а в консоль печатаем ОДНУ живую строку статуса — «● работаю» либо
# «✗ Ошибка: <последнее сообщение>», и только при смене состояния.

# Сколько секунд без новых ошибок считать, что всё снова работает.
_STATUS_OK_AFTER = 4.0


class _StatusFilter(logging.Handler):
    """Перехват логов Pyrogram: не печатает их, а запоминает последнюю ошибку.

    emit() вызывается из потока Pyrogram, поэтому пишем только в простые поля
    под блокировкой (без asyncio/print). Вывод статуса — отдельная фоновая
    задача в основном event loop (_status_loop).
    """

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._last_error: Optional[str] = None
        self._last_error_ts: float = 0.0
        # Отключаем наследование хендлеров у родительских логгеров, чтобы
        # Pyrogram не дублировал сообщения в stderr мимо перехвата.
        self._owned: list[str] = []

    def install(self) -> None:
        for name in ("pyrogram",):
            logger = logging.getLogger(name)
            # Убираем стандартный вывод pyrogram (StderrHandler и т.п.), чтобы
            # сообщения не печатались напрямую — только через перехват.
            for h in list(logger.handlers):
                logger.removeHandler(h)
            logger.addHandler(self)
            logger.setLevel(logging.WARNING)
            logger.propagate = False
            self._owned.append(name)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        try:
            msg = record.getMessage()
        except Exception:
            return
        with self._lock:
            self._last_error = msg
            self._last_error_ts = time.time()

    def snapshot(self) -> tuple[Optional[str], float]:
        with self._lock:
            return self._last_error, self._last_error_ts


_STATUS_FILTER = _StatusFilter()

# ANSI-цвета для статуса в терминале (зелёный «работаю», красный «ошибка»).
# Отключаются, если вывод не является TTY (напр. перенаправление в файл) —
# тогда оставляем только эмодзи, без экранирующих кодов.
def _use_color() -> bool:
    return sys.stdout.isatty()


def _friendly(record: str) -> str:
    """Превратить сырое сообщение Pyrogram в понятную ошибку пользователя.

    Pyrogram пишет вида: 'Retrying "updates.GetState" due to: Request timed out'
    или 'Connection failed: gaierror [...]'. Показываем суть, а не внутренности.
    """
    r = record.lower()
    if "timed out" in r or "timeout" in r:
        return "Сбой соединения с Telegram — нет ответа от сервера"
    if "gaierror" in r or "nodename" in r or "no host" in r:
        return "Нет доступа к сети — не удаётся разрешить хост"
    if "connection" in r or "connect" in r:
        return "Нет соединения с Telegram"
    if "internal server" in r or "interdc" in r or "500" in r:
        return "Временная ошибка Telegram (внутренняя/меж-DC)"
    # Незнакомое — режем до первых 120 символов простым текстом без кавычек.
    msg = record.strip().strip('"')
    return msg if len(msg) <= 120 else msg[:120] + "…"


def _print_status(state: str, detail: str = "") -> str:
    """Одна строка статуса в консоль. Возвращает показанное состояние."""
    if state == "ok":
        emoji, color, word = "🟢", "\033[32m", "Работаю"
        body = ""  # в норме текст не нужен — только статус
    else:
        emoji, color, word = "❌", "\033[31m", "Ошибка:"
        body = f"{detail or 'неизвестно'}"
    if _use_color():
        line = f"{color}{emoji} {word}{(' ' + body) if body else ''}\033[0m"
    else:
        line = f"{emoji} {word}{(' ' + body) if body else ''}"
    # \r — перезаписываем предыдущую строку статуса (одна живая строка).
    sys.stdout.write("\r" + " " * 60 + "\r" + line + "\n")
    sys.stdout.flush()
    return state


async def _status_loop(stop: asyncio.Event) -> None:
    """Фон: каждые 1.5с обновляет единую строку статуса при смене состояния."""
    shown = None
    await asyncio.sleep(0.2)  # дать pyrogram начать логировать
    while not stop.is_set():
        last_err, ts = _STATUS_FILTER.snapshot()
        if last_err and (time.time() - ts) <= _STATUS_OK_AFTER:
            # Свежая ошибка (была в последние секунды) — показываем её.
            state = "err"
            detail = _friendly(last_err)
        else:
            state = "ok"
            detail = ""
        # Печатаем только при смене состояния — без спама повторяющихся ошибок.
        if state != shown:
            _print_status(state, detail)
            shown = state
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            continue


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


# ---------------------------------------------------------------------------
# Удаление скачанных вложений (в корзину или навсегда)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Запуск агента «в любом чате» по строке-триггеру из конфига (SANDBOX_COMMAND)
# ---------------------------------------------------------------------------

# Строка-триггер запуска агента. Берётся из config.SANDBOX_COMMAND (по умолчанию
# "@helpbot" — упоминание бота). Сообщение обязано начинаться с неё, дальше
# пробел + команда/текст (с картинкой или без).
AGENT_PREFIX = config.SANDBOX_COMMAND


def _is_agent_message(text: str) -> bool:
    """Начинается ли сообщение с триггера SANDBOX_COMMAND (с границей слова).

    Требуем, чтобы после триггера шёл пробел или конец строки — чтобы не
    путать с похожими строками (@helpbotxyz и т.п.). Пустой триггер считаем
    «не агент-сообщение» (на практике SANDBOX_COMMAND всегда непустой — дефолт
    @helpbot, см. config).
    """
    if not AGENT_PREFIX:
        return False
    t = text.lstrip()
    if not t.startswith(AGENT_PREFIX):
        return False
    tail = t[len(AGENT_PREFIX):]
    return tail == "" or tail[0] in " \t\r\n"


def _strip_agent_prefix(text: str) -> str:
    """Срезать с начала сообщения '@helpbot' и все пробелы после него.

    Возвращает «хвост» — команду/текст, который передаём агенту. Если после
    среза получилась пустая строка (и нет картинки) — сообщение игнорируется.
    """
    t = text.lstrip()
    rest = t[len(AGENT_PREFIX):]
    # Срезаем все пробелы (обычные, табы, переводы строк) сразу после слова
    return rest.lstrip(" \t\r\n")


async def on_agent(client, message: Message):
    """Обработка агента по SANDBOX_COMMAND: работа в SANDBOX_ROOT независимо от чата.

    Сообщение обязано начинаться с триггера config.SANDBOX_COMMAND (по умолчанию
    '@helpbot'), дальше пробел и команда/текст (с картинкой или без). Работает
    в ЛЮБОМ чате, где бот является участником, если отправитель — разрешённый
    юзер (ALLOWED_USERS). Ограничение на чат (ALLOWED_CHAT_IDS) для этого
    триггера НЕ применяется.

    Остаток после триггера обрабатывается так же, как обычное сообщение бота,
    но в агентском контексте (корень SANDBOX_ROOT, отдельный выбор проекта):
      - команда (/list /new /switch /status /kill ...) → on_command(agent=True)
      - /clear → on_chat(agent=True) (это промпт для claude)
      - просто текст / вложение → on_chat / on_photo|on_video|on_audio|on_document(agent=True).

    Если после среза триггера и пробелов осталась пустая строка и нет
    прикреплённой картинки — сообщение игнорируется (ничего не отправляем).
    """
    user_id = _author(message)
    st = store.get(user_id)
    if not st:
        st = store.get_or_init(user_id)

    # Каталог, где работает агент: SANDBOX_ROOT или песочница по умолчанию.
    try:
        config.SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        await _reply(message, f"⚠️ Не удалось создать каталог агента: {config.SANDBOX_ROOT}")
        return

    rest = _strip_agent_prefix(message.text or (message.caption or ""))
    has_media = _has_any_media(message)

    # Пусто и нет вложения — нечего обрабатывать, игнорируем молча.
    if not rest.strip() and not has_media:
        return

    # Остаток начинается с '/' — это команда бота (или /clear для claude).
    # Вложение при команде не в дело. Работаем в агентском контексте (agent=True).
    if rest.strip().startswith("/"):
        cmd = rest.strip().split()[0].lower()
        if cmd == "/clear":
            # /clear — не команда бота, а промпт для claude (он сам сбросит контекст).
            await on_chat(client, message, rest.strip(), agent=True)
        else:
            await on_command(client, message, rest.strip(), agent=True)
        return

    # Просто текст или вложение (фото/видео/документ): уходим в claude в SANDBOX_ROOT.
    if has_media:
        # Срезанный остаток передаём как подпись (в caption он ещё с '@helpbot').
        # Вызываем соответствующий обработчик по типу вложения.
        g = getattr
        if g(message, "photo", None):
            await on_photo(client, message, agent=True, prompt_override=rest)
        elif g(message, "video", None):
            await on_video(client, message, agent=True, prompt_override=rest)
        elif g(message, "video_note", None):
            await on_video_note(client, message, agent=True, prompt_override=rest)
        elif g(message, "audio", None) or g(message, "voice", None):
            await on_audio(client, message, agent=True, prompt_override=rest)
        elif g(message, "document", None) or g(message, "animation", None):
            await on_document(client, message, agent=True, prompt_override=rest)
        elif g(message, "sticker", None):
            await on_sticker(client, message, agent=True, prompt_override=rest)
        elif g(message, "poll", None) or g(message, "location", None) \
                or g(message, "venue", None) or g(message, "contact", None):
            await _handle_textual_media(client, message, agent=True)
        else:
            # Неизвестный тип вложения под @helpbot — сообщаем, что не можем.
            await _on_unknown_media(client, message)
    else:
        await on_chat(client, message, rest, agent=True)


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


async def _on_unknown_media(client, message: Message):
    """Неизвестный тип вложения: сообщаем «не могу обработать» + указываем тип."""
    kind = _media_type_name(message) or "неизвестное вложение"
    await _reply(message, f"🤖 Не могу обработать: {kind}.")


# ---------------------------------------------------------------------------
# Служебные хендлеры
# ---------------------------------------------------------------------------

async def on_all_message(client, message: Message):
    """Фильтр всех сообщений: проверка доступа + перехват команд.

    Бот отвечает ТОЛЬКО в конкретном чате (из ALLOWED_CHAT_IDS) и только
    если отправитель — разрешённый юзер (ALLOWED_USERS). Всё остальное
    (другие чаты, чужие участники) молча игнорируется.
    """
    # Игнорируем собственные исходящие сообщения-ответы (реплаи): бот = твой
    # аккаунт, и его ответы («работаю», результат) приходят как outgoing + реплай.
    # Иначе бот зациклится. Твои обычные сообщения (не реплай) обрабатываются.
    if message.outgoing and message.reply_to_message_id is not None:
        return

    user_id = _author(message)
    # @helpbot работает «в любом чате» — ограничиваем ТОЛЬКО по разрешённому
    # юзеру, а не по чату. Обычные сообщения — по _allowed (юзер + чат).
    text = (message.text or "").strip()
    caption = (message.caption or "").strip()
    has_media = _has_any_media(message)
    if _is_agent_message(text) or (has_media and _is_agent_message(caption)):
        if not _is_allowed_user(user_id):
            return
        await on_agent(client, message)
        return
    if not _allowed(user_id, message.chat.id):
        return

    g = getattr
    if g(message, "photo", None):
        await on_photo(client, message)
        return
    if g(message, "video", None):
        await on_video(client, message)
        return
    if g(message, "video_note", None):
        # Видеосообщение-кружок — как видео.
        await on_video_note(client, message)
        return
    if g(message, "audio", None) or g(message, "voice", None):
        await on_audio(client, message)
        return
    if g(message, "document", None) or g(message, "animation", None):
        await on_document(client, message)
        return
    if g(message, "sticker", None):
        # Стикер — изображение-файл, отдаём в claude.
        await on_sticker(client, message)
        return
    if g(message, "poll", None) or g(message, "location", None) \
            or g(message, "venue", None) or g(message, "contact", None):
        # Опрос/гео/контакт: нет файла, но есть данные → текстовым описанием в claude.
        await _handle_textual_media(client, message)
        return
    # Неизвестный/не распознанный тип вложения — сообщаем «не могу обработать».
    if _has_any_media(message):
        await _on_unknown_media(client, message)
        return
    # /clear — не команда бота, а промпт для claude (claude сам обработает).
    # Поэтому отправляем его в on_chat, а не в on_command.
    # ВАЖНО: сравниваем ТОЧНО (cmd == "/clear"), а не startswith("/clear"),
    # иначе конфликт с /clearmedia (он тоже начинается с /clear).
    first = text.split()[0].lower() if text.split() else ""
    if first == "/clear":
        await on_chat(client, message, text)
        return
    if text.startswith("/"):
        await on_command(client, message, text)
        return
    if text:
        await on_chat(client, message, text)


async def on_command(client, message: Message, text: str, agent: bool = False):
    """Обработка команд: /start /list /switch /new /status /clean /help.

    agent=True — командный контекст @helpbot: работает внутри SANDBOX_ROOT и над
    отдельным выбором проекта st.agent_project_root (не пересекается с обычным
    стейтом, который живёт в PROJECTS_ROOT).
    """
    parts = text.split()
    cmd = parts[0].lower()
    user_id = _author(message)
    st = store.get_or_init(user_id)
    # Корень и активный проект зависят от режима: обычный (PROJECTS_ROOT) или
    # агентский (SANDBOX_ROOT). Ниже всё работает через root/active.
    root = config.SANDBOX_ROOT if agent else config.PROJECTS_ROOT
    label = "SANDBOX_ROOT" if agent else "PROJECTS_ROOT"
    active = st.get_active_root(agent)
    active_name = st.active_name(agent)

    if cmd == "/start" or cmd == "/help":
        box = [f"👋 Привет! Я бот для работы с Claude через Telegram (v{config.BOT_VERSION}).\n"]
        box.append(f"Корень проектов ({label}): {root}")
        if agent:
            box.append(f"Режим: {config.SANDBOX_COMMAND} (работа в {label})\n")
        if not active:
            box.append("Выбери проект командой /list или создай новый через /new.\n")
        else:
            box.append(f"Текущий проект: {active_name} ({active})\n")
        box.append("")
        box.append(format_command_hint())
        await _reply(message, "\n".join(box))

    elif cmd == "/list":
        projects = store.list_projects(user_id, root=root)
        if not projects:
            await _reply(message, 
                "Нет проектов. Создай первый: /new имя_проекта\n"
                f"(корень проектов: {root})"
            )
            return
        lines = [f"📁 Проекты (в {root}):"]
        for p in projects:
            star = "⭐" if active == str(p) else "•"
            lines.append(f"{star} /switch {p.name}")
        await _reply(message, "\n".join(lines))

    elif cmd == "/switch":
        name = parts[1] if len(parts) > 1 else ""
        if not is_safe_project_name(name):
            await _reply(message, "Неверное имя проекта. /switch <имя>")
            return
        proj = root / name
        if not proj.is_dir():
            await _reply(message, f"Проект {name} не найден. Смотри /list")
            return
        st.set_active(agent, str(proj), name)
        # НЕ сбрасываем сессию: на этом проекте хранится своя session_id,
        # она восстановится из st.session_ids при следующем запросе.
        store.update(st)
        await _reply(message, f"✅ Переключился на проект: {name}")

    elif cmd == "/new":
        name = parts[1] if len(parts) > 1 else f"proj_{user_id}"
        if not is_safe_project_name(name):
            await _reply(message, "Неверное имя проекта. /new <имя>")
            return
        proj = root / name
        proj.mkdir(parents=True, exist_ok=True)
        st.set_active(agent, str(proj), name)
        store.update(st)
        await _reply(message, f"✅ Создан и активирован проект: {name}")

    elif cmd == "/status":
        st = store.get(user_id) or st
        active = st.get_active_root(agent)
        lines = [f"📊 Статус (v{config.BOT_VERSION}):\nПроект: {st.active_name(agent) or '(не выбран)'}"]
        lines.append(f"Корень проектов ({label}): {root}")
        lines.append(f"Активный путь: {active or '/'}")
        lines.append(f"Сессия: {st.get_session(active) or '(новая)'}")
        lines.append(
            f"Запущено ботом: {len(_bot_proc_pids)} задач, "
            f"активных: {len(_active_tasks)}"
        )
        await _reply(message, "\n".join(lines))

    elif cmd == "/kill":
        # Прибить ТОЛЬКО зависшие claude-кластеры, запущенные ЭТИМ ботом.
        # Не трогаем чужие/ручные сессии (они не в _bot_proc_pids).
        killed, errs = kill_bot_procs()
        # Отменяем фоновые задачи, чтобы _run_and_reply дошёл до finally и
        # дочистил файлы, а run_claude убил свои дочерние процессы.
        for t in list(_active_tasks):
            t.cancel()
        msg = f"🔪 Закрыто процессов (сессий бота): {killed}"
        if errs:
            msg += f"\n⚠️ не удалось: {errs}"
        await _reply(message, msg)

    elif cmd == "/clearmedia":
        # Удалить скачанные вложения (.claude_tg_bot_media) в текущем каталоге.
        # В корзину или навсегда — по DELETE_MODE. Сообщаем сколько удалили.
        await _clear_media(message, active, agent=agent, root=str(root))

    else:
        # Незнакомые команды молча игнорируем
        return


async def _clear_media(message, active: str, agent: bool = False, root: str = ""):
    """Команда /clearmedia: удалить скачанные вложения текущего проекта.

    Удаляет подпапку .claude_tg_bot_media внутри активного каталога. Считает, сколько
    файлов и какой объём освобождается, и удаляет по DELETE_MODE (в корзину или
    навсегда). Если вложений нет — сообщает об этом.
    """
    base = Path(active) if active else Path(root)
    media_dir = base / ".claude_tg_bot_media"

    if not media_dir.exists() or not any(media_dir.iterdir()):
        await _reply(message, "Нет скачанных вложений — чистить нечего.")
        return

    # Сосчитать файлы и объём до удаления.
    files = [p for p in media_dir.rglob("*") if p.is_file()]
    count = len(files)
    size = _dir_size(media_dir)
    free = _fmt_bytes(size)

    mode = (config.DELETE_MODE or "trash").lower()
    where = "в корзину" if mode == "trash" else "навсегда"

    # Удаляем каталог целиком (по DELETE_MODE).
    try:
        _delete_path(media_dir, mode)
    except Exception as e:
        await _reply(message, f"⚠️ Не удалось очистить вложения: {e}")
        return

    await _reply(
        message,
        f"🧹 Удалено {count} файл(ов) (~{free}) — {where}.\n"
        f"Каталог: {media_dir}",
    )


async def _handle_attachment(
    client,
    message: Message,
    kind: str,
    ext: str,
    agent: bool = False,
    prompt_override: Optional[str] = None,
    sniff_ext: bool = False,
):
    """Общая логика обработки вложения: фото/видео/звук/файл → в claude.

    Файл кладём внутрь активного проекта (а не в /tmp), чтобы @путь из промпта
    гарантированно читался claude как вложение (вложение должно быть в cwd
    процесса). После обработки удаляется, только если AUTO_DELETE_MEDIA включён,
    иначе файл остаётся (очистка — вручную через /clearmedia).

    Подпись (caption) становится промптом. Если подписи нет — уходим в claude
    с ПУСТЫМ промптом: он сам увидит @вложение и сохранит его в контексте
    сессии. Если подпись — команда (/...) — обрабатываем её как on_command,
    вложение не в дело.

    Часть проверок отдаём самому claude: он сам решает, как обработать файл
    (сам извлечёт кадры из видео или транскрибирует звук). Мы НЕ проверяем
    доступность внешних утилит заранее и не отказываем — просто отдаём файл,
    а claude сам разрулит.

    media — объект вложения (photo/video/audio/voice/document).
    kind — человеческое имя («фото», «видео», «аудио», «файл»).
    ext — расширение файла (.jpg, .mp4, .mp3, .pdf, ...).
    sniff_ext — для фото: после скачивания определить реальное расширение по
    содержимому (PNG/JPEG/WebP/GIF) и переименовать файл, чтобы claude видел
    верный тип (объект photo в Pyrogram не несёт mime_type/file_name).

    agent=True — контекст @helpbot: работаем в SANDBOX_ROOT (или агентском проекте),
    а не в обычном project_root. prompt_override — заменяет caption для @helpbot.
    """
    user_id = _author(message)
    st = store.get(user_id)
    if not st:
        await _reply(message, "Сначала выбери проект: /list или /new <имя>")
        return

    # Рабочий каталог: агентский проект (иначе корень SANDBOX_ROOT) или обычный.
    if agent:
        project = Path(st.agent_project_root or config.SANDBOX_ROOT)
    else:
        if not st.project_root:
            await _reply(message, "Сначала выбери проект: /list или /new <имя>")
            return
        project = Path(st.project_root)

    # Подпапка внутри проекта для временных вложений (удаляется вместе с файлом)
    media_dir = project / ".claude_tg_bot_media"
    media_dir.mkdir(parents=True, exist_ok=True)

    caption = prompt_override if prompt_override is not None else (message.caption or "").strip()

    # Для фото (sniff_ext) скачиваем с нейтральным расширением, затем уточним
    # реальное по содержимому и переименуем — чтобы @путь и тип были верными.
    download_ext = ".img" if sniff_ext else ext
    fname = f"claude_tg_bot_{kind}_{time.time_ns()}{download_ext}"
    media_path = media_dir / fname

    try:
        await message.download(file_name=str(media_path))
    except Exception as e:
        await _reply(message, f"Не удалось скачать {kind}: {e}")
        return

    if sniff_ext:
        # Определяем формат по магическим байтам и переименовываем файл.
        real_ext = _sniff_image_ext(media_path)
        if real_ext != download_ext:
            renamed = media_path.with_suffix(real_ext)
            try:
                media_path.rename(renamed)
                media_path = renamed
            except OSError:
                pass  # не смогли переименовать — оставляем как скачали

    if caption.startswith("/"):
        # Подпись — команда (напр. /status): обрабатываем её, вложение не в дело.
        media_path.unlink(missing_ok=True)
        await on_command(client, message, caption, agent=agent)
        return

    # Вложение с подписью или без: caption становится промптом. Если подписи нет —
    # уходим в claude с ПУСТЫМ промптом: он сам увидит @файл и сохранит его в
    # контексте сессии (никакой заглушки). Обрабатываем в фоне, чтобы не
    # блокировать последующие сообщения.
    # Стартуем НОВУЮ сессию (без --resume): вложение уходит в claude как отдельное.
    _start_bg(_run_and_reply(client, message, st, caption, [media_path], cwd=str(project)))


async def on_photo(client, message: Message, agent: bool = False, prompt_override: Optional[str] = None):
    """Обработка фото → в claude.

    Расширение определяем по содержимому скачанного файла (PNG/JPEG/WebP/GIF),
    т.к. Pyrogram-объект photo не несёт file_name/mime_type. Fallback — .jpg
    (как в старом варианте, чтобы claude всегда видел картинку).
    """
    media = message.photo
    if media is None:
        return
    # Расширение по содержимому (PNG/JPEG/WebP/GIF) — определяем после скачивания.
    await _handle_attachment(client, message, "фото", ".jpg", agent, prompt_override, sniff_ext=True)


async def on_audio(client, message: Message, agent: bool = False, prompt_override: Optional[str] = None):
    """Обработка звука/голосового → в claude (он сам транскрибирует/разберёт)."""
    media = message.audio or message.voice
    if media is None:
        return
    await _handle_attachment(client, message, "аудио", _media_ext(media), agent, prompt_override)


async def on_video(client, message: Message, agent: bool = False, prompt_override: Optional[str] = None):
    """Обработка видео → в claude (он сам извлечёт кадры и разберёт)."""
    media = message.video
    if media is None:
        return
    await _handle_attachment(client, message, "видео", _media_ext(media), agent, prompt_override)


async def on_video_note(client, message: Message, agent: bool = False, prompt_override: Optional[str] = None):
    """Обработка видеосообщения-кружка (video_note) как видео.

    Кружок — круглое видеосообщение. Отдаём его claude как видео: он сам
    извлечёт кадры и разберёт содержание. Расширение — по mime (обычно .mp4).
    """
    media = message.video_note
    if media is None:
        return
    ext = _media_ext(media)
    if ext == ".bin":  # у кружка нет file_name; без mime считаем .mp4
        ext = ".mp4"
    await _handle_attachment(client, message, "видео-кружок", ext, agent, prompt_override)


async def on_document(client, message: Message, agent: bool = False, prompt_override: Optional[str] = None):
    """Обработка произвольного файла (документ/GIF-анимация) → в claude."""
    media = message.document or message.animation
    if media is None:
        return
    await _handle_attachment(client, message, "файл", _media_ext(media), agent, prompt_override)


async def on_sticker(client, message: Message, agent: bool = False, prompt_override: Optional[str] = None):
    """Обработка стикера → в claude как изображение-вложение.

    Стикер — это файл (.webp/.tgs/.webm), скачиваем и отдаём claude: он сам
    увидит картинку. Если подписи нет — уходим в claude с пустым промптом.
    """
    media = message.sticker
    if media is None:
        return
    ext = _media_ext(media)
    if ext == ".bin":  # стикер без mime — обычно .webp
        ext = ".webp"
    await _handle_attachment(client, message, "стикер", ext, agent, prompt_override)


def _textual_media_prompt(message) -> Optional[str]:
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


async def _handle_textual_media(client, message, agent: bool = False):
    """Отправить в claude текстовое описание медиа (опрос/гео/контакт).

    on_chat сам запускает claude фоновой задачей (_start_bg внутри), поэтому
    здесь лишь формируем промпт и передаём его в on_chat.
    """
    prompt = _textual_media_prompt(message)
    if not prompt:
        return
    await on_chat(client, message, prompt, agent=agent)


def _count_group(gid: int) -> int:
    """Сколько процессов в группе gid (для отчёта /kill). 0, если группа пуста."""
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=", "-g", str(gid)],
            capture_output=True, text=True, timeout=2,
        )
        return len([ln for ln in out.stdout.splitlines() if ln.strip()])
    except Exception:
        return 1  # не удалось посчитать — считаем минимум сам процесс


def kill_bot_procs() -> tuple[int, list[str]]:
    """Убить все зависшие claude-кластеры, запущенные этим ботом.

    Работает ТОЛЬКО по реестру _bot_proc_pids (pid групп, которые бот сам
    зарегистрировал в run_claude), поэтому чужие/ручные сессии claude не
    затрагиваются. Возвращает (сколько ПРОЦЕССОВ закрыто, список ошибок).
    """
    killed = 0
    errors: list[str] = []
    for pid in list(_bot_proc_pids):
        try:
            # Считаем процессы в группе ДО убийства (для честного отчёта).
            pgid = os.getpgid(pid)
            n = _count_group(pgid)
            # Убиваем всю группу (родитель + дочерние процессы), не только родителя.
            os.killpg(pgid, signal.SIGKILL)
            killed += n if n else 1
        except ProcessLookupError:
            # Процесс уже завершился сам — не ошибка, засчитываем.
            _bot_proc_pids.discard(pid)
            killed += 1
        except Exception as e:
            errors.append(f"pid {pid}: {e}")
    # Снимаем убитые pids из реестра, чтобы повторный /kill был идемпотентным.
    for pid in list(_bot_proc_pids):
        try:
            os.kill(pid, 0)  # probe: жив ли ещё
        except ProcessLookupError:
            _bot_proc_pids.discard(pid)
    return killed, errors


def _start_bg(coro):
    """Запустить корутину фоновой задачей и запомнить её для отмены из /kill.

    Храним ссылку на задачу, чтобы /kill мог прервать зависший вызов claude.
    При завершении задача сама снимается из _active_tasks.
    """
    task = asyncio.get_running_loop().create_task(coro)
    _active_tasks.add(task)

    def _done(t):
        _active_tasks.discard(t)
        # Не даём необработанным исключениям «молча» всплыть в event loop
        if not t.cancelled():
            try:
                t.exception()
            except asyncio.CancelledError:
                pass

    task.add_done_callback(_done)
    return task


async def on_chat(client, message: Message, text: str, agent: bool = False):
    """Обычное сообщение → запуск claude в активном проекте.

    Запускаем claude ФОНОВОЙ задачей (create_task), а не ждём её здесь.
    Иначе с workers=1 хендлер держит воркер, пока claude думает (минуты),
    и все последующие сообщения — в т.ч. команды /help, /kill — встают в
    очередь и не обрабатываются. В фоне команды отрабатывают сразу.

    agent=True — контекст @helpbot: работаем в SANDBOX_ROOT (или в выбранном
    агентском проекте st.agent_project_root), а не в обычном project_root.
    """
    user_id = _author(message)
    st = store.get(user_id)
    if not st:
        await _reply(message, 
            "Нет активного проекта. Выбери: /list или /new <имя>"
        )
        return

    # Рабочий каталог зависит от режима:
    #  - обычный: нужен выбранный проект (иначе ошибка);
    #  - @helpbot: выбранный агентский проект, иначе корень SANDBOX_ROOT (сама песочница).
    if not agent:
        if not st.project_root:
            await _reply(message, 
                "Нет активного проекта. Выбери: /list или /new <имя>"
            )
            return
        project_root = st.project_root
    else:
        project_root = st.agent_project_root or str(config.SANDBOX_ROOT)

    session_id = st.get_session(project_root) or None
    _start_bg(_run_and_reply(client, message, st, text, [], session_id=session_id, cwd=project_root))


async def _run_and_reply(client, message, st, prompt: str, image_paths, session_id=None, cwd=None):
    """Общая точка: запуск claude + вывод результата.

    Перед обработкой шлём «работаю», запоминаем его id, а когда claude ответил —
    удаляем «работаю» и отправляем ответ. Это сигналит, что бот жив, а не завис.
    image_paths — временные вложения (картинки) внутри проекта. После обработки
    они удаляются только если AUTO_DELETE_MEDIA включён (по DELETE_MODE), иначе
    остаются в .claude_tg_bot_media до ручной очистки /clearmedia.

    cwd — рабочий каталог Claude. По умолчанию st.project_root
    (активный проект). Для @helpbot передаётся config.SANDBOX_ROOT (песочница
    агента).
    """
    project = Path(cwd) if cwd else Path(st.project_root)
    # Помечаем чат «занятым», чтобы бот не реагировал на собственные ответы
    # Посылаем индикатор работы и запоминаем его id (chat_id + message_id).
    # Индикатор шлём с ретраями (как и ответ): при меж-DC ошибке Telegram
    # ([500 INTERDC_X_CALL_ERROR]) делаем несколько попыток. Если ВСЕ не
    # удались — пропускаем индикатор и продолжаем работу, чтобы не заблокировать
    # запуск claude (иначе бот «молчит» в чатах вне основного DC).
    busy = None
    for _attempt in range(3):
        try:
            # Таймаут на попытку: чтоб каждая не висела бесконечно (Pyrogram сам
            # ретраит SendMessage), но при транзиентной ошибке — повторяем.
            busy = await asyncio.wait_for(_reply(message, "Думаю..."), timeout=5)
            break
        except Exception:
            busy = None
            if _attempt < 2:
                await asyncio.sleep(2.0)
    busy_chat = busy.chat.id if busy else None
    busy_msg_id = busy.id if busy else None

    async def _cleanup_busy():
        # Удалить «работаю» после ответа/ошибки, как условие обработки выполнено
        if busy_chat is not None and busy_msg_id is not None:
            try:
                # Короткий таймаут: при недоступном MTProto-прокси Pyrogram
                # иначе бесконечно ретраит "messages.DeleteMessages" и не даёт
                # завершить работу (Ctrl+C) и/или отмену задачи.
                await asyncio.wait_for(
                    client.delete_messages(busy_chat, busy_msg_id), timeout=3
                )
            except Exception:
                pass

    try:
        try:
            result = await run_claude(
                prompt,
                cwd=project,
                session_id=session_id,
                image_paths=image_paths,
                proc_registry=_bot_proc_pids,
            )
            # Сохраняем актуальный session_id для ЭТОГО проекта, чтобы далее
            # продолжать контекст даже после /switch на другой проект и обратно.
            # Для @helpbot ключём служит фактический каталог (project), а не
            # активный проект пользователя.
            if result.session_id:
                st.set_session(str(project), result.session_id)
                store.update(st)
            text = result.text or "(пустой ответ)"
            # Телеграм лимит 4096 — режем с указанием
            if len(text) > 4000:
                text = text[:4000] + "\n\n… (ответ обрезан, продолжай следующим сообщением)"
            # Сначала присылаем ответ, потом убираем «работаю». Если удаление
            # индикатора зависнет (сбой MTProto-прокси), ответ всё равно дойдёт.
            # Ответ шлём с ретраями: при меж-DC ошибке Telegram (INTERDC_X_CALL_ERROR)
            # он не должен теряться — делаем несколько попыток.
            await _send_with_retry(message, text)
            await _cleanup_busy()
        except FileNotFoundError as e:
            await _cleanup_busy()
            await _send_with_retry(message, str(e))
        except ValueError as e:
            await _cleanup_busy()
            await _send_with_retry(message, str(e))
        except asyncio.CancelledError:
            # Задачу отменили (команда /kill) — убираем «Думаю...» и пробрасываем.
            await _cleanup_busy()
            raise
        except Exception as e:
            await _cleanup_busy()
            await _send_with_retry(message, f"⚠️ Ошибка: {e}")
    finally:
        # Автоудаление вложений. Если AUTO_DELETE_MEDIA выключен — НЕ удаляем:
        # файлы остаются в проекте, очистить можно вручную командой /clearmedia.
        if config.AUTO_DELETE_MEDIA:
            for p in image_paths or []:
                try:
                    _delete_path(Path(p))
                except Exception:
                    pass
            # Пустую подпапку вложений подчищаем, если она опустела.
            try:
                media_dir = project / ".claude_tg_bot_media"
                if media_dir.exists() and not any(media_dir.iterdir()):
                    media_dir.rmdir()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def _build_client() -> Client:
    """Создать Client Pyrogram (Kurigram, dev-ветка) с нативной поддержкой MTProto-прокси.

    Kurigram (dev) умеет MTProto-прокси из коробки: передаём proxy строкой
    "tg://proxy?server=...&port=...&secret=..." — он сам распознаёт secret
    (ee → FakeTLS с SNI-доменом, dd/plain → random padding), выбирает нужный
    транспорт TCPIntermediatePadded и делает FakeTLS-хендшейк. Никакой внешний
    mtproxy-bridge не нужен.

    Если MT_PROXY не задан — передаём None: бот подключается к Telegram
    напрямую, без прокси (MT_PROXY больше не обязателен).

    Возвращает настроенный Client.
    """
    return Client(
        name=config.SESSION_NAME,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        phone_number=config.PHONE,
        password=config.CLOUD_PASSWORD,
        # hide_password=True — прячем ввод пароля 2FA (getpass без эха),
        # чтобы он не светился в терминале. Код подтверждения остаётся видимым.
        hide_password=True,
        proxy=config.MT_PROXY or None,
        # workers=1 — сообщения обрабатываются ПОСЛЕДОВАТЕЛЬНО. Иначе Pyrogram
        # параллельно запускает несколько процессов Claude; при сетевом сбое это
        # множит зависшие процессы и даёт кашу из ответов вместо внятного таймаута.
        workers=1,
    )


async def main():
    config.validate()
    # Защита от второго экземпляра: если бот уже запущен — выходим, не стартуя.
    if not acquire_single_instance():
        return
    app = _build_client()

    # Важно: используем filters.all, а не filters.INCOMING.
    #  - В «Избранном» свои сообщения приходят как incoming, но В ГРУППЕ твои
    #    сообщения (ты = аккаунт бота) — как outgoing. Поэтому filters.incoming
    #    в группах их не ловит → нужен filters.all.
    #  - Чтобы бот не зациклился на собственных ответах, в on_all_message
    #    игнорируем исходящие сообщения-реплаи (reply_to_message_id).
    # workers=1 — обрабатываем сообщения ПОСЛЕДОВАТЕЛЬНО. Иначе Pyrogram
    # параллельно запускает несколько процессов Claude; при сетевом сбое это множит
    # зависшие процессы и даёт кашу из ответов вместо внятного таймаута.
    app.on_message(filters.all & ~filters.service)(on_all_message)

    # Перехватываем логи Pyrogram (ошибки подключения/ретраи), чтобы вместо
    # спама в stderr показывать в консоли единую строку статуса.
    _STATUS_FILTER.install()

    print(f"Запуск бота. Сессия: {config.SESSION_NAME}")
    env_src = str(config.CONFIG_ENV_PATH) if config.CONFIG_ENV_PATH else "не найден — дефолты"
    print(f"config.env: {env_src}")
    print("MT-прокси:", ("задан" if config.MT_PROXY else "НЕ задан (прямое подключение)"))
    print(f"Команда Claude: {config.CLAUDE_COMMAND} {config.COMMAND_ARGS}".strip())
    print(f"Каталог агента ({config.SANDBOX_COMMAND}): {config.SANDBOX_ROOT}")

    # start() — подключаемся к Telegram (в т.ч. логин). При сбое сети не
    # вылетаем с трейсбеком, а печатаем короткое сообщение и повторяем
    # с растущей паузой (1 → 15 мин). Ctrl+C прерывает паузу.
    await _start_with_retry(app)

    print("✅ Бот запущен и работает. Жду сообщения в Telegram.")
    print("   Чтобы остановить — нажми Ctrl+C в этом окне (SIGINT).")

    # Фоновая задача: держит в консоли одну живую строку статуса «Работаю» /
    # «✗ Ошибка: …». Останавливается вместе с ботом.
    stop_status = asyncio.Event()
    status_task = asyncio.create_task(_status_loop(stop_status))

    # idle() — держим процесс живым, пока не придёт SIGINT/SIGTERM.
    try:
        await pyrogram.idle()
    finally:
        # Останавливаем статус-луп и корректно завершаемся.
        stop_status.set()
        status_task.cancel()
        # Корректная остановка: отменяем фоновые задачи (они могли остаться
        # зависшими на claude) и глушим Pyrogram. Иначе asyncio.run не может
        # завершить цикл событий, пока живы эти задачи, и Ctrl+C «не работает».
        await _shutdown(app)


async def _start_with_retry(app):
    """Подключение к Telegram с умными повторами при сбое сети.

    Вместо вылета с трейсбеком (TimeoutError после внутренних ретраев) печатаем
    КОРОТКОЕ понятное сообщение и повторяем подключение с растущей паузой:
    1, 2, ... мин, доходя до 15 мин, дальше держим 15 мин.

    Ctrl+C прерывает паузу: KeyboardInterrupt/CancelledError — это
    BaseException, мы их НЕ ловим, поэтому приложение закрывается как обычно.
    """
    delay = 60           # стартовая пауза, сек (1 мин)
    max_delay = 15 * 60  # потолок — 15 мин, дальше держим 15
    while True:
        try:
            await app.start()
            return  # подключились — выходим из цикла
        except KeyboardInterrupt:
            raise
        except (ConnectionError, TimeoutError, OSError) as e:
            mins = delay // 60
            # Одна короткая строка: без спама повторяющихся ошибок.
            note = f"{_friendly(str(e))}. Повтор через {mins} мин..."
            if _use_color():
                line = f"\033[31m❌ {note}\033[0m"
            else:
                line = f"❌ {note}"
            sys.stdout.write("\r" + " " * 60 + "\r" + line + "\n")
            sys.stdout.flush()
            await asyncio.sleep(delay)
            # Умный рост: +1 мин за попытку до 15, потом держим 15.
            delay = min(delay + 60, max_delay)


async def _shutdown(app):
    """Отменить фоновые задачи и остановить Pyrogram, чтобы бот вышел чисто."""
    # 1. Отменяем все фоновые задачи (вызовы claude), не дожидаясь их вечно —
    #    они сами в finally убьют свои процессы и дочистят файлы.
    tasks = list(_active_tasks)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # 2. Останавливаем Pyrogram (гасит его внутренние воркеры/ретраи).
    #    block=False — не ждём, пока зависшие на ретраях воркеры завершатся,
    #    иначе Ctrl+C «не прерывает» бота.
    try:
        await app.stop(block=False)
    except Exception:
        pass


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())