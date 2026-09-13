"""claude-tg-bot — Telegram-бот для управления Claude.

Работает как userbot на MTProto-клиенте (MTProto-протокол) через MTProto-прокси,
поэтому не зависит от доступа к api.telegram.org (Bot API).
"""

try:
    from .config import BOT_VERSION as __version__
except ImportError:
    # Запуск как скрипт (python3 claude-tg-bot.py) — модуль __init__ без пакета.
    from config import BOT_VERSION as __version__