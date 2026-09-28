"""Entry point: python -m claude_tg_bot (or python3 claude_tg_bot/__main__.py)."""

import asyncio
import sys
import threading

from . import status
from .client import main as bot_main


def _run_bot() -> None:
    asyncio.run(bot_main())


if __name__ == "__main__":
    # textual's App.run() installs SIGTSTP/SIGCONT/SIGTTOU/SIGTTIN handlers, and
    # Python only allows those on the main thread — so the TUI owns the main
    # thread and the whole asyncio bot runs on a worker thread. When stdout is a
    # tty we start the bot first, then run the TUI (blocking) on the main thread;
    # otherwise fall back to the plain single-threaded run (Ctrl+C works as before).
    if sys.stdout.isatty():
        bot = threading.Thread(target=_run_bot, name="bot", daemon=True)
        bot.start()
        status.ui_init()
        bot.join(timeout=5)
    else:
        asyncio.run(bot_main())