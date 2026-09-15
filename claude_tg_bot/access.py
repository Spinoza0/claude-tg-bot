"""Проверка доступа: кто может писать боту и в каких чатах."""

from pyrogram.types import Message

from . import config


def _is_allowed_user(user_id: int) -> bool:
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
    (см. on_sandbox).
    """
    return _is_allowed_user(user_id) and _is_allowed_chat(user_id, chat_id)


def _author(message: Message) -> int:
    """user_id отправителя сообщения (надёжнее, чем chat.id в группах)."""
    if message.from_user is not None:
        return message.from_user.id
    # если отправитель не определён (напр. канал) — считаем chat.id
    return message.chat.id