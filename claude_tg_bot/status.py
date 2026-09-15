"""Единый статус в консоли вместо спама ошибок Pyrogram.

Pyrogram/Kurigram логирует свои ошибки подключения (gaierror, INTERDC… и
ретраи) через стандартный logging с логгерами "pyrogram.*" и печатает их
потоком в stderr. Это засоряет консоль и пугает. Вместо этого перехватываем
логи Pyrogram своим Handler'ом: храним ПОСЛЕДНЮЮ ошибку, глушим потоковый
вывод, а в консоль печатаем блок статуса — «🟢 Работаю» либо
«❌ Ошибка: <последнее сообщение>», и только при смене состояния.
"""

import asyncio
import logging
import sys
import threading
import time
from typing import Optional

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

# Ошибка запуска Claude (отвала модели/обёртки): хранится отдельно от логов
# pyrogram (тот — про связь с Telegram). Пишется из _run_and_reply при
# ненулевом exit_code или тексте с признаком ошибки, читается _status_loop.
# Так консоль показывает ❌ и при отвале модели, а не только при обрыве сети.
_RUN_ERR_LOCK = threading.Lock()
_RUN_ERR_TEXT: str = ""
_RUN_ERR_TS: float = 0.0


def _report_run_error(text: str) -> None:
    global _RUN_ERR_TEXT, _RUN_ERR_TS
    with _RUN_ERR_LOCK:
        _RUN_ERR_TEXT = text
        _RUN_ERR_TS = time.time()


def _snapshot_run_error() -> tuple[str, float]:
    with _RUN_ERR_LOCK:
        return _RUN_ERR_TEXT, _RUN_ERR_TS


# Ошибка модели/обёртки в тексте ответа (если exit_code по какой-то причине
# остался 0, но пользователь получает в чат не ответ, а сообщение об ошибке).
_RUN_ERR_MARKERS = ("API Error", "⚠️ Ошибка", "Cannot connect", "502", "503")


def _is_run_error(result) -> bool:
    return result.exit_code != 0 or result.text.lstrip().startswith(_RUN_ERR_MARKERS)


# Маркеры того, что НЕДОСТУПНА именно МОДЕЛЬ/провайдер (а не сбой запуска,
# длинный промпт и т.п.). По ним бот решает переключить модель на
# COMMAND_ARGS_ALTERNATIVE (issue #5). Сюда попадают ответы обёртки вида
# "API Error: 502 Cannot connect ..." или "model not found"/"rate limit".
_MODEL_UNAVAILABLE_MARKERS = (
    "API Error", "Cannot connect", "502", "503",
    "model not found", "model unavailable", "rate limit", "upstream",
)


def _is_model_unavailable(result) -> bool:
    """True, если ответ говорит о недоступности модели (её надо сменить)."""
    text = result.text.lstrip()
    return any(m in text for m in _MODEL_UNAVAILABLE_MARKERS)


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


# Число строк, которое занял последний блок статуса (для перерисовки).
_STATUS_PREV_LINES = 0


def _stamp() -> str:
    """Дата+время для строки статуса (напр. 2026-09-12 14:32:05)."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _draw_status(lines: list[str]) -> None:
    """Перерисовать блок статуса на месте, не трогая вывод выше блока.

    lines — строки блока: первая — состояние работы, вторая (если есть) —
    последняя ошибка. Поднимаемся к началу прошлого блока (\\033[F) и стираем
    всё от курсора до конца экрана (\\033[J) — это снимает и старый блок целиком
    (при смене высоты 1↔2 не остаётся «хвоста»), не задевая строки выше. Затем
    печатаем новый блок. \\033[2K перед строкой добивает остатки при переносе.
    """
    global _STATUS_PREV_LINES
    out = sys.stdout
    if _STATUS_PREV_LINES:
        out.write(f"\033[{_STATUS_PREV_LINES}F")
        out.write("\033[J")
    for i, line in enumerate(lines):
        if i:
            out.write("\n")
        out.write("\033[2K" + line)
    out.flush()
    _STATUS_PREV_LINES = len(lines)


def _status_lines(err_display: str, err_ts: float, err_active: bool, work_ts: float) -> list[str]:
    """Собрать строки блока статуса: работа + последняя ошибка.

    Иконка (🟢/❌) ставится только у АКТУАЛЬНОГО состояния:
      - если сейчас проблема (err_active=True) — ❌ у ошибки, у «Работаю» без 🟢;
      - если сейчас всё хорошо — 🟢 у «Работаю», а последняя ошибка без ❌ (как
        история). Обе строки показываются вместе; ошибка всегда последняя.
    Нет ошибок вовсе — только «Работаю».
    work_ts — время, когда установилось состояние «работаю» (НЕ тикает каждый
    цикл, иначе строка менялась бы и блок не переставал перерисовываться).
    """
    color = _use_color()
    def fmt(t: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
    work_ts_s = fmt(work_ts)
    if err_active:
        # Сейчас проблема: работа без 🟢, ошибка с ❌.
        if color:
            work = f"\033[32mРаботаю\033[0m [{work_ts_s}]"
            err = f"\033[31m❌ Ошибка: {err_display}\033[0m [{fmt(err_ts)}]"
        else:
            work = f"Работаю [{work_ts_s}]"
            err = f"❌ Ошибка: {err_display} [{fmt(err_ts)}]"
        return [work, err]
    # Сейчас всё хорошо: 🟢 у работы; последняя ошибка — без ❌ (история).
    if color:
        work = f"\033[32m🟢 Работаю\033[0m [{work_ts_s}]"
    else:
        work = f"🟢 Работаю [{work_ts_s}]"
    lines = [work]
    if err_display:
        lines.append(f"Ошибка: {err_display} [{fmt(err_ts)}]")
    return lines


async def _status_loop(stop: asyncio.Event) -> None:
    """Фон: каждые 1.5с держит блок — «работаю» + последняя ошибка.

    Иконка ❌/🟢 ставится только у актуального состояния: при свежей ошибке —
    ❌, при норме — 🟢. Последняя ошибка (если была) показывается всегда, но
    без ❌, когда сейчас всё хорошо, и содержит время. Повторно ошибка не
    дублируется (блок печатается только при изменении).
    """
    prev = None
    prev_err = None
    ok_since = 0.0
    await asyncio.sleep(0.2)  # дать pyrogram начать логировать
    while not stop.is_set():
        # Объединяем два источника ошибки: связь с Telegram (логи pyrogram) и
        # отвал модели/обёртки (запуск Claude). Берём более свежую.
        pg_err, pg_ts = _STATUS_FILTER.snapshot()
        run_err, run_ts = _snapshot_run_error()
        if run_err and (not pg_err or run_ts >= pg_ts):
            last_err, ts = run_err, run_ts
        else:
            last_err, ts = pg_err, pg_ts
        err_active = bool(last_err) and (time.time() - ts) <= _STATUS_OK_AFTER
        # Время «работаю» фиксируем при переходе в ок (или на старте), чтобы
        # оно не тикало каждый цикл и блок не перерисовывался без изменений.
        if not err_active and (prev_err or ok_since == 0.0):
            ok_since = time.time()
        prev_err = err_active
        # Последняя ошибка показывается всегда (если была) — pyrogram-ошибку
        # причесываем через _friendly (текст про сеть/Telegram), ошибку запуска
        # Claude показываем как есть (это не про связь с Telegram).
        if last_err:
            if run_err and last_err == run_err:
                err_display = last_err
            else:
                err_display = _friendly(last_err)
            err_ts = ts
        else:
            err_display = ""
            err_ts = 0.0
        lines = _status_lines(err_display, err_ts, err_active, ok_since)
        # Печатаем только при изменении содержимого — без спама повторов.
        if lines != prev:
            _draw_status(lines)
            prev = lines
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            continue