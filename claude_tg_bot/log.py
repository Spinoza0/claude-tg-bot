"""File logging for diagnostics (issue #12).

By default the bot does not write logs. Logging is enabled by the command-line
flag `--log[=level]` when running `python -m claude_tg_bot`. Files land in
`~/.claude-tg-bot/logs/`. If the launch is in an interactive terminal (tty)
without `--log`, and old logs already exist, the bot asks whether to delete
them (by default — not to delete).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import i18n

# Log levels available via --log[=level]. The key is uppercased, so both 'info'
# and 'INFO' are accepted.
_LEVELS = {"error": logging.ERROR, "warning": logging.WARNING,
           "warn": logging.WARNING, "info": logging.INFO, "debug": logging.DEBUG}

# Loggers routed to the file: our own claude_tg_bot (bot events) and pyrogram
# (connection errors/retries). Which ones actually land — the level decides.
_LOGGERS = ("claude_tg_bot", "pyrogram")


def log_dir() -> Path:
    return Path.home() / ".claude-tg-bot" / "logs"


def parse_level(value: Optional[str]) -> Optional[int]:
    """Turn the --log value into a logging level.

    None/empty — ERROR (default). 'info'/'debug'/'error' map accordingly.
    Unknown values are ignored, returning ERROR (we don't crash).
    """
    if not value:
        return logging.ERROR
    return _LEVELS.get(value.strip().lower(), logging.ERROR)


def parse_log_flag(argv: list[str]) -> tuple[bool, int]:
    """Parse argv for the --log flag.

    Returns (enabled, level). Supports `--log` (level ERROR) and
    `--log=info` / `--log=debug` / `--log=error`. The flag can also come as a
    separate word `--log info` (we don't handle it — we keep it simple, only
    `--log[=level]`, as documented).
    """
    for arg in argv:
        if arg == "--log":
            return True, logging.ERROR
        if arg.startswith("--log="):
            return True, parse_level(arg[len("--log="):])
    return False, logging.ERROR


def setup_logging(level: int, path: Path) -> Path:
    """Enable file logging for the bot and pyrogram loggers.

    Adds a FileHandler to each of _LOGGERS (with its own formatter), setting the
    level to `level`. Returns the path to the log file.
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
    """A dated log filename, e.g. claude-tg-bot-2026-09-15.log."""
    return log_dir() / f"claude-tg-bot-{datetime.now():%Y-%m-%d}.log"


def old_logs() -> list[Path]:
    """All existing log files in the directory (if the directory exists)."""
    d = log_dir()
    if not d.exists():
        return []
    return sorted(p for p in d.glob("claude-tg-bot-*.log") if p.is_file())


def is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_yes_no(prompt: str, default: bool = False) -> bool:
    """Ask y/n with a default. Only for interactive (tty) launches."""
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def maybe_cleanup_old_logs() -> None:
    """If the bot runs without logs but old logs exist — ask about deletion.

    Only done on an interactive launch (tty). By default (or on a non-interactive
    / background launch) it deletes nothing.
    """
    existing = old_logs()
    if not existing or not is_tty():
        return
    n = len(existing)
    print(i18n.t("log.found_old", n=n, path=log_dir()))
    if _ask_yes_no(i18n.t("log.ask_delete"), default=False):
        removed = 0
        for p in existing:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        print(i18n.t("log.deleted", n=removed))