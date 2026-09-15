"""claude_tg_bot — Telegram-бот для управления Claude.

Работает как userbot на MTProto-клиенте (MTProto-протокол) через
MTProto-прокси, поэтому не зависит от доступа к api.telegram.org (Bot API).
"""

from .version import BOT_VERSION as __version__

__all__ = ["__version__"]