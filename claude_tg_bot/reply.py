"""Отправка сообщений от бота: единый формат + повторы при транзиентной ошибке."""

from pyrogram.types import Message

from . import config
from .retry import _run_with_retry


async def _reply(message: Message, text: str):
    """Отправить сообщение от бота с префиксом-«головой робота» 🤖 + пробелом.

    Единый формат всех исходящих сообщений бота: эмодзи головы робота,
    пробел, затем само сообщение (🤖 текст). Используется во всех хендлерах,
    чтобы стиль был одинаковым.
    """
    return await message.reply_text(f"🤖 {text}")


async def _send_with_retry(message: Message, text: str, attempts: int | None = None, delay: float | None = None):
    """Отправить ответ с повторными попытками при транзиентной ошибке Telegram.

    Telegram временами отвечает внутренней ошибкой дата-центра
    (напр. [500 INTERDC_X_CALL_ERROR] — меж-DC вызов в чатах вне основного DC
    при работе через MTProto-прокси). Тогда `reply_text` бросает исключение, и
    результат теряется. Здесь используем единый механизм повторов с короткими
    паузами (секунды). Если все попытки провалились — возвращаем None (не
    бросаем), чтобы не уронить обработку дальше.
    """
    return await _run_with_retry(
        lambda: _reply(message, text),
        limit=attempts if attempts is not None else config.MESSAGE_RETRY_LIMIT,
        base_delay=delay if delay is not None else config.MESSAGE_RETRY_DELAY,
        # Отправка — транзиентная ошибка, пауза постоянная (множитель 1),
        # а не растущая: иначе ответ «зависал» бы дольше нужного.
        multiplier=1.0,
        max_delay=config.MESSAGE_RETRY_DELAY,
    )