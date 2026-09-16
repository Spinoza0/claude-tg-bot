"""Отправка сообщений от бота: единый формат + повторы при транзиентной ошибке."""

from pyrogram.types import Message

from . import config
from .retry import _run_with_retry

# Telegram режет одно сообщение на 4096 символов. Держим запас и режем по
# границе абзаца, чтобы не рвать мысль на середине.
_MSG_LIMIT = 4000


async def _reply(message: Message, text: str):
    """Отправить сообщение от бота с префиксом-«головой робота» 🤖 + пробелом.

    Единый формат всех исходящих сообщений бота: эмодзи головы робота,
    пробел, затем само сообщение (🤖 текст). Используется во всех хендлерах,
    чтобы стиль был одинаковым.
    """
    return await message.reply_text(f"🤖 {text}")


def _split_text(text: str) -> list[str]:
    """Разбить длинный текст на части ≤ _MSG_LIMIT, по границам абзацев.

    Не рвём строку посередине слова: ищем последний перенос строки или пробел
    перед лимитом. Если текст короче лимита — возвращаем его одним куском.
    """
    text = text.strip()
    if len(text) <= _MSG_LIMIT:
        return [text]
    chunks = []
    while len(text) > _MSG_LIMIT:
        cut = text[: _MSG_LIMIT]
        # Ищем последний разрыв (абзац/пробел) в пределах куска.
        split_at = max(cut.rfind("\n"), cut.rfind(" "))
        if split_at <= 0:
            split_at = _MSG_LIMIT
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks


async def _send_split(message: Message, text: str, attempts: int | None = None, delay: float | None = None):
    """Отправить текст, при необходимости разбив на несколько сообщений.

    Каждая часть — отдельное сообщение (с ретраями). Используется, когда ответ
    Claude длиннее лимита Telegram: вместо обрезки доходим весь текст кусками.
    """
    for chunk in _split_text(text):
        await _send_with_retry(message, chunk, attempts, delay)


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