"""Создание Telegram-клиента, точка входа main() и подключение с повторами."""

import asyncio
import logging
import sys

import pyrogram
from pyrogram import Client, filters

from . import config
from .log import log_filename, maybe_cleanup_old_logs, parse_log_flag, setup_logging
from .process import _active_tasks, acquire_single_instance
from .retry import _retry_backoff_delay
from .handlers import on_all_message
from .status import (
    _STATUS_FILTER,
    _STATUS_PREV_LINES,
    _friendly,
    _status_loop,
    _use_color,
)

# События бота (запуск/остановка, команды, запуск Claude, ошибки) — в файл лога.
logger = logging.getLogger("claude_tg_bot")


def _build_client() -> Client:
    """Создать Client Pyrogram (Kurigram, dev-ветка) с нативной поддержкой MTProto-прокси.

    Kurigram (dev) умеет MTProto-прокси из коробки: передаём proxy строкой
    "tg://proxy?server=...&port=...&secret=..." — он сам распознаёт secret
    (ee → FakeTLS с SNI-доменом, dd/plain → random padding), выбирает нужный
    транспорт TCPIntermediatePadded и делает FakeTLS-хендшейк. Никакой внешний
    mtproxy-bridge не нужен.

    Если MT_PROXY не задан — передаём None: бот подключается к Telegram
    напрямую, без прокси (MT_PROXY больше не обязателен).

    Возвращает настроенный Client.
    """
    return Client(
        name=config.SESSION_NAME,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        phone_number=config.PHONE,
        password=config.CLOUD_PASSWORD,
        # hide_password=True — прячем ввод пароля 2FA (getpass без эха),
        # чтобы он не светился в терминале. Код подтверждения остаётся видимым.
        hide_password=True,
        proxy=config.MT_PROXY or None,
        # workers=1 — сообщения обрабатываются ПОСЛЕДОВАТЕЛЬНО. Иначе Pyrogram
        # параллельно запускает несколько процессов Claude; при сетевом сбое это
        # множит зависшие процессы и даёт кашу из ответов вместо внятного таймаута.
        workers=1,
    )


async def _start_with_retry(app):
    """Подключение к Telegram с повторными попытками при сбое сети.

    Вместо вылета с трейсбеком (TimeoutError после внутренних ретраев) печатаем
    КОРОТКОЕ понятное сообщение и повторяем подключение с единой схемой паузы:
    1, 2, ... мин (потолок RETRY_MAX_DELAY). После RETRY_LIMIT попыток —
    сообщаем об ошибке и завершаемся (не крутим бесконечно).

    Ctrl+C прерывает паузу: KeyboardInterrupt/CancelledError — это
    BaseException, мы их НЕ ловим, поэтому приложение закрывается как обычно.
    """
    for attempt in range(1, config.RETRY_LIMIT + 1):
        try:
            await app.start()
            logger.info("Подключено к Telegram")
            return  # подключились — выходим из цикла
        except KeyboardInterrupt:
            raise
        except (ConnectionError, TimeoutError, OSError) as e:
            if attempt < config.RETRY_LIMIT:
                pause = _retry_backoff_delay(attempt)
                mins = max(1, round(pause / 60))
                # Одна короткая строка: без спама повторяющихся ошибок.
                note = f"{_friendly(str(e))}. Повтор через {mins} мин ({attempt}/{config.RETRY_LIMIT})..."
                logger.warning("Подключение к Telegram: попытка %s/%s неудачна: %s",
                               attempt, config.RETRY_LIMIT, _friendly(str(e)))
                if _use_color():
                    line = f"\033[31m❌ {note}\033[0m"
                else:
                    line = f"❌ {note}"
                sys.stdout.write("\r" + " " * 60 + "\r" + line + "\n")
                sys.stdout.flush()
                await asyncio.sleep(pause)
            else:
                logger.error("Не удалось подключиться к Telegram после %s попыток: %s",
                             config.RETRY_LIMIT, _friendly(str(e)))
                sys.stdout.write(
                    "\n❌ Не удалось подключиться к Telegram после "
                    f"{config.RETRY_LIMIT} попыток: {_friendly(str(e))}\n"
                )
                sys.stdout.flush()
                raise


async def _shutdown(app):
    """Отменить фоновые задачи и остановить Pyrogram, чтобы бот вышел чисто."""
    # 1. Отменяем все фоновые задачи (вызовы claude), не дожидаясь их вечно —
    #    они сами в finally убьют свои процессы и дочистят файлы.
    tasks = list(_active_tasks)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # 2. Останавливаем Pyrogram (гасит его внутренние воркеры/ретраи).
    #    block=False — не ждём, пока зависшие на ретраях воркеры завершатся,
    #    иначе Ctrl+C «не прерывает» бота.
    try:
        await app.stop(block=False)
    except Exception:
        pass


async def main():
    config.validate()
    # Защита от второго экземпляра: если бот уже запущен — выходим, не стартуя.
    if not acquire_single_instance():
        return

    # Логирование (issue #12): включается флагом --log[=уровень]. По умолчанию
    # (без флага) — не пишем; при интерактивном запуске без лога, но с уже
    # существующими логами — предлагаем удалить старые.
    log_enabled, log_level = parse_log_flag(sys.argv)
    log_path = None
    if log_enabled:
        log_path = setup_logging(log_level, log_filename())
        logger.info("Логирование включено (уровень=%s), файл: %s",
                    logging.getLevelName(log_level), log_path)
    else:
        maybe_cleanup_old_logs()

    app = _build_client()

    # Важно: используем filters.all, а не filters.INCOMING.
    #  - В «Избранном» свои сообщения приходят как incoming, но В ГРУППЕ твои
    #    сообщения (ты = аккаунт бота) — как outgoing. Поэтому filters.incoming
    #    в группах их не ловит → нужен filters.all.
    #  - Чтобы бот не зациклился на собственных ответах, в on_all_message
    #    игнорируем исходящие сообщения-реплаи (reply_to_message_id).
    # workers=1 — обрабатываем сообщения ПОСЛЕДОВАТЕЛЬНО. Иначе Pyrogram
    # параллельно запускает несколько процессов Claude; при сетевом сбое это множит
    # зависшие процессы и даёт кашу из ответов вместо внятного таймаута.
    app.on_message(filters.all & ~filters.service)(on_all_message)

    # Перехватываем логи Pyrogram (ошибки подключения/ретраи), чтобы вместо
    # спама в stderr показывать в консоли единую строку статуса.
    _STATUS_FILTER.install()

    print(f"Запуск бота. Telegram-сессия: {config.SESSION_NAME}")
    # config.env путь уже показал run.sh (==> config.env). Здесь печатаем корень
    # проектов и каталог песочницы подряд, чтобы было видно, где лежит что.
    print(f"Корень проектов (PROJECTS_ROOT): {config.PROJECTS_ROOT}")
    print(f"Каталог песочницы ({config.SANDBOX_COMMAND}): {config.SANDBOX_ROOT}")
    print("MT-прокси:", ("задан" if config.MT_PROXY else "НЕ задан (прямое подключение)"))
    print(f"Команда Claude: {config.CLAUDE_COMMAND} {config.COMMAND_ARGS}".strip())

    # start() — подключаемся к Telegram (в т.ч. логин). При сбое сети не
    # вылетаем с трейсбеком, а печатаем короткое сообщение и повторяем
    # с растущей паузой. Ctrl+C прерывает паузу.
    await _start_with_retry(app)

    logger.info("Бот запущен и работает (в %s)", config.SANDBOX_ROOT)
    print("✅ Бот запущен и работает. Жду сообщения в Telegram.")
    print("   Чтобы остановить — нажми Ctrl+C в этом окне (SIGINT).")

    # Фоновая задача: держит в консоли блок статуса «Работаю» / «❌ Ошибка: …».
    # Останавливается вместе с ботом.
    stop_status = asyncio.Event()
    status_task = asyncio.create_task(_status_loop(stop_status))

    # idle() — держим процесс живым, пока не придёт SIGINT/SIGTERM.
    try:
        await pyrogram.idle()
    finally:
        # Останавливаем статус-луп и корректно завершаемся.
        stop_status.set()
        status_task.cancel()
        # Перенос строк после последнего блока статуса — иначе следующий вывод
        # (трейсбек, shell prompt) прилипнет к последней строке блока.
        sys.stdout.write("\n" * (_STATUS_PREV_LINES + 1))
        sys.stdout.flush()
        # Корректная остановка: отменяем фоновые задачи (они могли остаться
        # зависшими на claude) и глушим Pyrogram. Иначе asyncio.run не может
        # завершить цикл событий, пока живы эти задачи, и Ctrl+C «не работает».
        logger.info("Бот остановлен")
        await _shutdown(app)