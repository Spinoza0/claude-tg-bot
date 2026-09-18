"""Access control: who may write to the bot and in which chats."""

from pyrogram.types import Message

from . import config


def _is_allowed_user(user_id: int) -> bool:
    return user_id in config.ALLOWED_USERS


def _is_allowed_chat(user_id: int, chat_id: int) -> bool:
    """Whether the chat is allowed for regular messages.

    - If ALLOWED_CHAT_IDS is empty, only the chat where chat_id equals your
      own user_id is allowed (Saved Messages).
    - If ALLOWED_CHAT_IDS is set, the chat must be in that list.
    """
    if config.ALLOWED_CHAT_IDS:
        return chat_id in config.ALLOWED_CHAT_IDS
    # Empty list: allow only the chat matching the user_id
    # (in Saved Messages the chat_id equals your account id).
    return chat_id == user_id


def _allowed(user_id: int, chat_id: int) -> bool:
    """Whether to reply: sender allowed AND chat allowed.

    The bot runs under your account and must not reply everywhere you write.
    Regular messages require both an allowed user and an allowed chat
    (see _is_allowed_chat). For @helpbot the chat is NOT restricted — only
    the user (see on_sandbox).
    """
    return _is_allowed_user(user_id) and _is_allowed_chat(user_id, chat_id)


def _author(message: Message) -> int:
    """The message sender's user_id (more reliable than chat.id in groups)."""
    if message.from_user is not None:
        return message.from_user.id
    # sender not determined (e.g. channel) — fall back to chat.id
    return message.chat.id