"""Определение песочницы: триггер SANDBOX_COMMAND и распознавание сообщения.

Сама обработка песочницы (on_sandbox) живёт в handlers.py — она запускает
остальные хендлеры. Здесь только чистые предикаты над строкой сообщения.
"""

from . import config

# Строка-триггер запуска песочницы. Берётся из config.SANDBOX_COMMAND (по умолчанию
# "@helpbot" — упоминание бота). Сообщение обязано начинаться с неё, дальше
# пробел + команда/текст (с картинкой или без).
SANDBOX_PREFIX = config.SANDBOX_COMMAND


def _is_sandbox_message(text: str) -> bool:
    """Начинается ли сообщение с триггера SANDBOX_COMMAND (с границей слова).

    Требуем, чтобы после триггера шёл пробел или конец строки — чтобы не
    путать с похожими строками (@helpbotxyz и т.п.). Пустой триггер считаем
    «не для песочницы» (на практике SANDBOX_COMMAND всегда непустой — дефолт
    @helpbot, см. config).
    """
    if not SANDBOX_PREFIX:
        return False
    t = text.lstrip()
    if not t.startswith(SANDBOX_PREFIX):
        return False
    tail = t[len(SANDBOX_PREFIX):]
    return tail == "" or tail[0] in " \t\r\n"


def _strip_sandbox_prefix(text: str) -> str:
    """Срезать с начала сообщения '@helpbot' и все пробелы после него.

    Возвращает «хвост» — команду/текст, который передаём в песочницу. Если после
    среза получилась пустая строка (и нет картинки) — сообщение игнорируется.
    """
    t = text.lstrip()
    rest = t[len(SANDBOX_PREFIX):]
    # Срезаем все пробелы (обычные, табы, переводы строк) сразу после слова
    return rest.lstrip(" \t\r\n")