"""Entry point: python -m claude_tg_bot (or python3 claude_tg_bot/__main__.py)."""

import asyncio
import threading

from . import status
from .client import main as bot_main


def _run_bot() -> None:
    asyncio.run(bot_main())


if __name__ == "__main__":
    # textual's App.run() installs SIGTSTP/SIGCONT handlers, which Python only
    # allows on the main thread — so the TUI owns the main thread and the whole
    # asyncio bot runs on a worker thread. ui_init() blocks on the main thread
    # until the TUI exits (or returns immediately when stdout is not a tty).
    bot = threading.Thread(target=_run_bot, name="bot", daemon=True)
    bot.start()
    status.ui_init()
    bot.join(timeout=5)