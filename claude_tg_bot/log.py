"""Логирование в файл для диагностики (issue #12).

По умолчанию бот не пишет логи. Логирование включается флагом командной
строки `--log[=уровень]` при запуске `python -m claude_tg_bot`. Файлы
кладутся в каталог `~/.claude-tg-bot/logs/`. Если запуск идёт в интерактивном
терминале (tty) без флага `--log`, а старые логи уже есть — бот спрашивает,
удалить ли их (по умолчанию — не удалять).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Уровни логирования, доступные через --log[=уровень]. Ключ приводится к верхнему
# регистру, поэтому принимаем и 'info', и 'INFO'.
_LEVELS = {"error": logging.ERROR, "warning": logging.WARNING,
           "warn": logging.WARNING, "info": logging.INFO, "debug": logging.DEBUG}

# Логгеры, которые направляем в файл: свой claude_tg_bot (события бота) и
# pyrogram (ошибки подключения/ретраи). Что именно попадёт — решает уровень.
_LOGGERS = ("claude_tg_bot", "pyrogram")


def log_dir() -> Path:
    """Каталог, куда пишутся логи: ~/.claude-tg-bot/logs."""
    return Path.home() / ".claude-tg-bot" / "logs"


def parse_level(value: Optional[str]) -> Optional[int]:
    """Превратить значение флага --log в уровень logging.

    None/пусто — ERROR (по умолчанию). 'info'/'debug'/'error' — соответствует.
    Незнакомое значение игнорируем, возвращая ERROR (не падаем).
    """
    if not value:
        return logging.ERROR
    return _LEVELS.get(value.strip().lower(), logging.ERROR)


def parse_log_flag(argv: list[str]) -> tuple[bool, int]:
    """Разобрать argv на предмет флага --log.

    Возвращает (включено, уровень). Поддерживается `--log` (уровень ERROR)
    и `--log=info` / `--log=debug` / `--log=error`. Флаг также может идти
    отдельным словом `--log info` (не обрабатываем — оставляем простым,
    только `--log[=уровень]`, как в документации).
    """
    for arg in argv:
        if arg == "--log":
            return True, logging.ERROR
        if arg.startswith("--log="):
            return True, parse_level(arg[len("--log="):])
    return False, logging.ERROR


def setup_logging(level: int, path: Path) -> Path:
    """Включить файловое логирование для логгеров бота и pyrogram.

    Добавляет FileHandler к каждому из _LOGGERS (со своим форматтером),
    выставляя уровень level. Возвращает путь к лог-файлу.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    ))
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.addHandler(handler)
    return path


def log_filename() -> Path:
    """Имя файла лога с датой, напр. claude-tg-bot-2026-09-15.log."""
    return log_dir() / f"claude-tg-bot-{datetime.now():%Y-%m-%d}.log"


def old_logs() -> list[Path]:
    """Все существующие файлы логов в каталоге (если каталог есть)."""
    d = log_dir()
    if not d.exists():
        return []
    return sorted(p for p in d.glob("claude-tg-bot-*.log") if p.is_file())


def is_tty() -> bool:
    """Запущен ли бот из интерактивного терминала (есть ввод с клавиатуры)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_yes_no(prompt: str, default: bool = False) -> bool:
    """Спросить y/n с дефолтом. Только для интерактивного запуска (tty)."""
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


def maybe_cleanup_old_logs() -> None:
    """Если бот запущен без логов, но старые логи есть — спросить об удалении.

    Выполняется только при интерактивном запуске (tty). По умолчанию (или при
    неинтерактивном запуске/в фоне) — ничего не удаляет.
    """
    existing = old_logs()
    if not existing or not is_tty():
        return
    n = len(existing)
    print(f"⚠️ Найдены старые логи ({n} файл(ов) в {log_dir()}).")
    if _ask_yes_no("Удалить их? [y/N] (по умолчанию НЕ удалять): ", default=False):
        removed = 0
        for p in existing:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        print(f"Удалено {removed} файл(ов) логов.")