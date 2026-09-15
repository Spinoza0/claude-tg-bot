"""Единый механизм повторов с растущей паузой (issue #6).

Общий для долгих повторов (подключение к Telegram, вызовы Claude) и переопределяемый
короткими (отправка сообщений). Пауза после n-й неудачи растёт как
base * multiplier^(n-1) с потолком max.
"""

import asyncio

from . import config


def _retry_backoff_delay(n: int) -> float:
    """Пауза перед (n)-м повтором: base * multiplier^(n-1) с потолком max.

    n отсчитывается от 1 (первый повтор). Так после первой неудачи — base,
    после второй — base*multiplier и т.д. до max. Возвращает секунды.
    """
    delay = config.RETRY_BASE_DELAY * (config.RETRY_MULTIPLIER ** (n - 1))
    return min(delay, config.RETRY_MAX_DELAY)


async def _run_with_retry(
    action,
    *,
    limit: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    multiplier: float | None = None,
):
    """Единый механизм повторов: вызывает action(), при неудаче ждёт паузу.

    Пауза после n-й неудачи растёт как base*multiplier^(n-1) с потолком max
    (по умолчанию — из config, см. _retry_backoff_delay). После limit попыток
    (включая первую) отдаёт None. Не ловит KeyboardInterrupt/CancelledError —
    прерывание должно работать всегда.

    Параметры limit/base_delay/max_delay/multiplier переопределяют значения из
    config: для коротких повторов отправки сообщений задаются секундами и
    multiplier=1.0 (постоянная пауза), для долгих — минуты с ростом.
    """
    limit = limit if limit is not None else config.RETRY_LIMIT
    for attempt in range(1, limit + 1):
        try:
            result = await action()
            return result
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception:
            if attempt < limit:
                if base_delay is not None:
                    await asyncio.sleep(
                        min(base_delay * (multiplier or 1.0) ** (attempt - 1), max_delay or base_delay)
                    )
                else:
                    await asyncio.sleep(_retry_backoff_delay(attempt))
    return None