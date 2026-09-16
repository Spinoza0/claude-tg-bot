"""Обработчики сообщений Telegram: песочница, вложения, обычный чат, запуск Claude.

Центральный «оркестратор»: собирает вместе работу с проектами (commands),
вложениями (media), песочницей (sandbox) и запуск Claude (runner/sessions).
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from pyrogram.types import Message

from . import config

logger = logging.getLogger("claude_tg_bot")
from .runner import run_claude
from .sessions import find_latest_session, store
from .access import _allowed, _author, _is_allowed_user
from .process import _active_tasks, _bot_proc_pids, _start_bg
from .retry import _retry_backoff_delay, _run_with_retry
from .commands import _clear_media, on_command
from .media import (
    _delete_path,
    _has_any_media,
    _media_ext,
    _media_type_name,
    _sniff_image_ext,
    _textual_media_prompt,
)
from .reply import _reply, _send_split, _send_with_retry
from .sandbox import _is_sandbox_message, _strip_sandbox_prefix
from .status import _is_model_unavailable, _is_run_error, _report_run_error


async def on_sandbox(client, message: Message):
    """Обработка песочницы по SANDBOX_COMMAND: работа в SANDBOX_ROOT независимо от чата.

    Сообщение обязано начинаться с триггера config.SANDBOX_COMMAND (по умолчанию
    '@helpbot'), дальше пробел и команда/текст (с картинкой или без). Работает
    в ЛЮБОМ чате, где бот является участником, если отправитель — разрешённый
    юзер (ALLOWED_USERS). Ограничение на чат (ALLOWED_CHAT_IDS) для этого
    триггера НЕ применяется.

    Остаток после триггера обрабатывается так же, как обычное сообщение бота,
    но в контексте песочницы (корень SANDBOX_ROOT, отдельный выбор проекта):
      - команда (/list /new /switch /status /kill ...) → on_command(sandbox=True)
      - /clear → on_chat(sandbox=True) (это промпт для claude)
      - просто текст / вложение → on_chat / on_photo|on_video|on_audio|on_document(sandbox=True).

    Если после среза триггера и пробелов осталась пустая строка и нет
    прикреплённой картинки — сообщение игнорируется (ничего не отправляем).
    """
    user_id = _author(message)
    st = store.get(user_id)
    if not st:
        st = store.get_or_init(user_id)

    # Каталог, где работает песочница: SANDBOX_ROOT или песочница по умолчанию.
    try:
        config.SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        await _reply(message, f"⚠️ Не удалось создать каталог песочницы: {config.SANDBOX_ROOT}")
        return

    rest = _strip_sandbox_prefix(message.text or (message.caption or ""))
    has_media = _has_any_media(message)

    # Пусто и нет вложения — нечего обрабатывать, игнорируем молча.
    if not rest.strip() and not has_media:
        return

    # Остаток начинается с '/' — это команда бота (или /clear для claude).
    # Вложение при команде не в дело. Работаем в контексте песочницы (sandbox=True).
    if rest.strip().startswith("/"):
        cmd = rest.strip().split()[0].lower()
        if cmd == "/clear":
            # /clear — не команда бота, а промпт для claude (он сам сбросит контекст).
            await on_chat(client, message, rest.strip(), sandbox=True)
        else:
            await on_command(client, message, rest.strip(), sandbox=True)
        return

    # Просто текст или вложение (фото/видео/документ): уходим в claude в SANDBOX_ROOT.
    if has_media:
        # Срезанный остаток передаём как подпись (в caption он ещё с '@helpbot').
        g = getattr
        if g(message, "photo", None):
            await on_photo(client, message, sandbox=True, prompt_override=rest)
        elif g(message, "video", None):
            await on_video(client, message, sandbox=True, prompt_override=rest)
        elif g(message, "video_note", None):
            await on_video_note(client, message, sandbox=True, prompt_override=rest)
        elif g(message, "audio", None) or g(message, "voice", None):
            await on_audio(client, message, sandbox=True, prompt_override=rest)
        elif g(message, "document", None) or g(message, "animation", None):
            await on_document(client, message, sandbox=True, prompt_override=rest)
        elif g(message, "sticker", None):
            await on_sticker(client, message, sandbox=True, prompt_override=rest)
        elif g(message, "poll", None) or g(message, "location", None) \
                or g(message, "venue", None) or g(message, "contact", None):
            await _handle_textual_media(client, message, sandbox=True)
        else:
            # Неизвестный тип вложения под @helpbot — сообщаем, что не можем.
            await _on_unknown_media(client, message)
    else:
        await on_chat(client, message, rest, sandbox=True)


async def on_all_message(client, message: Message):
    """Фильтр всех сообщений: проверка доступа + перехват команд.

    Бот отвечает ТОЛЬКО в конкретном чате (из ALLOWED_CHAT_IDS) и только
    если отправитель — разрешённый юзер (ALLOWED_USERS). Всё остальное
    (другие чаты, чужие участники) молча игнорируется.
    """
    # Игнорируем собственные исходящие сообщения-ответы (реплаи): бот = твой
    # аккаунт, и его ответы («работаю», результат) приходят как outgoing + реплай.
    # Иначе бот зациклится. Твои обычные сообщения (не реплай) обрабатываются.
    if message.outgoing and message.reply_to_message_id is not None:
        return

    user_id = _author(message)
    # @helpbot работает «в любом чате» — ограничиваем ТОЛЬКО по разрешённому
    # юзеру, а не по чату. Обычные сообщения — по _allowed (юзер + чат).
    text = (message.text or "").strip()
    caption = (message.caption or "").strip()
    has_media = _has_any_media(message)
    if _is_sandbox_message(text) or (has_media and _is_sandbox_message(caption)):
        if not _is_allowed_user(user_id):
            return
        await on_sandbox(client, message)
        return
    if not _allowed(user_id, message.chat.id):
        return

    g = getattr
    if g(message, "photo", None):
        await on_photo(client, message)
        return
    if g(message, "video", None):
        await on_video(client, message)
        return
    if g(message, "video_note", None):
        # Видеосообщение-кружок — как видео.
        await on_video_note(client, message)
        return
    if g(message, "audio", None) or g(message, "voice", None):
        await on_audio(client, message)
        return
    if g(message, "document", None) or g(message, "animation", None):
        await on_document(client, message)
        return
    if g(message, "sticker", None):
        # Стикер — изображение-файл, отдаём в claude.
        await on_sticker(client, message)
        return
    if g(message, "poll", None) or g(message, "location", None) \
            or g(message, "venue", None) or g(message, "contact", None):
        # Опрос/гео/контакт: нет файла, но есть данные → текстовым описанием в claude.
        await _handle_textual_media(client, message)
        return
    # Неизвестный/не распознанный тип вложения — сообщаем «не могу обработать».
    if _has_any_media(message):
        await _on_unknown_media(client, message)
        return
    # /clear — не команда бота, а промпт для claude (claude сам обработает).
    # Поэтому отправляем его в on_chat, а не в on_command.
    # ВАЖНО: сравниваем ТОЧНО (cmd == "/clear"), а не startswith("/clear"),
    # иначе конфликт с /clearmedia (он тоже начинается с /clear).
    first = text.split()[0].lower() if text.split() else ""
    if first == "/clear":
        await on_chat(client, message, text)
        return
    if text.startswith("/"):
        await on_command(client, message, text)
        return
    if text:
        await on_chat(client, message, text)


async def on_chat(client, message: Message, text: str, sandbox: bool = False):
    """Обычное сообщение → запуск claude в активном проекте.

    Запускаем claude ФОНОВОЙ задачей (create_task), а не ждём её здесь.
    Иначе с workers=1 хендлер держит воркер, пока claude думает (минуты),
    и все последующие сообщения — в т.ч. команды /help, /kill — встают в
    очередь и не обрабатываются. В фоне команды отрабатывают сразу.

    sandbox=True — контекст @helpbot: работаем в SANDBOX_ROOT (или в выбранном
    проекте песочницы st.sandbox_project_root), а не в обычном project_root.
    """
    user_id = _author(message)
    st = store.get(user_id)
    if not st:
        await _reply(message,
            "Нет активного проекта. Выбери: /list или /new <имя>"
        )
        return

    # Рабочий каталог зависит от режима:
    #  - обычный: нужен выбранный проект (иначе ошибка);
    #  - @helpbot: выбранный проект песочницы, иначе корень SANDBOX_ROOT (сама песочница).
    if not sandbox:
        if not st.project_root:
            await _reply(message,
                "Нет активного проекта. Выбери: /list или /new <имя>"
            )
            return
        project_root = st.project_root
    else:
        project_root = st.sandbox_project_root or str(config.SANDBOX_ROOT)

    # /clear — полный сброс: сначала чистим скачанные вложения, потом шлём
    # /clear в claude (тот сам сбросит контекст сессии). Иначе файлы остаются.
    if text.split()[0].lower() == "/clear":
        root = config.SANDBOX_ROOT if sandbox else config.PROJECTS_ROOT
        # silent: если каталога вложений нет — молча идём дальше, в Claude.
        await _clear_media(message, project_root, sandbox=sandbox, root=str(root), silent=True)

    # session_id не передаём — _run_and_reply сам найдёт последнюю сессию
    # каталога и сделает --resume, либо запустит новую.
    _start_bg(_run_and_reply(client, message, st, text, [], cwd=project_root))


async def _run_and_reply(client, message, st, prompt: str, image_paths, resume_session_id=None, cwd=None):
    """Общая точка: запуск claude + вывод результата.

    Перед обработкой шлём «работаю», запоминаем его id, а когда claude ответил —
    удаляем «работаю» и отправляем ответ. Это сигналит, что бот жив, а не завис.
    image_paths — временные вложения (картинки) внутри проекта. После обработки
    они удаляются только если AUTO_DELETE_MEDIA включён (по DELETE_MODE), иначе
    остаются в .claude_tg_bot_media до ручной очистки /clearmedia.

    cwd — рабочий каталог Claude. По умолчанию st.project_root
    (активный проект). Для @helpbot передаётся config.SANDBOX_ROOT (песочница).

    resume_session_id — id сессии для продолжения через --resume. Если не задан
    явно — сами находим последнюю сессию каталога с диска (find_latest_session),
    чтобы все точки входа (on_chat, on_photo, @helpbot) вели себя одинаково.
    Если сессии нет — запускается новая, и её id вернётся в result.session_id.
    """
    project = Path(cwd) if cwd else Path(st.project_root)
    if not resume_session_id:
        resume_session_id = find_latest_session(project)
    # Посылаем индикатор работы и запоминаем его id (chat_id + message_id).
    # Индикатор шлём с ретраями (как и ответ): при меж-DC ошибке Telegram
    # ([500 INTERDC_X_CALL_ERROR]) делаем несколько попыток. Если ВСЕ не
    # удались — пропускаем индикатор и продолжаем работу, чтобы не заблокировать
    # запуск claude (иначе бот «молчит» в чатах вне основного DC).
    busy = None
    async def _send_busy():
        return await asyncio.wait_for(_reply(message, "Думаю..."), timeout=5)
    busy = await _run_with_retry(
        _send_busy,
        limit=config.MESSAGE_RETRY_LIMIT,
        base_delay=config.MESSAGE_RETRY_DELAY,
        multiplier=1.0,
        max_delay=config.MESSAGE_RETRY_DELAY,
    )
    busy_chat = busy.chat.id if busy else None
    busy_msg_id = busy.id if busy else None

    async def _cleanup_busy():
        if busy_chat is not None and busy_msg_id is not None:
            try:
                # Короткий таймаут: при недоступном MTProto-прокси Pyrogram
                # иначе бесконечно ретраит "messages.DeleteMessages" и не даёт
                # завершить работу (Ctrl+C) и/или отмену задачи.
                await asyncio.wait_for(
                    client.delete_messages(busy_chat, busy_msg_id), timeout=3
                )
            except Exception:
                pass

    try:
        try:
            # Смена модели при её недоступности (issue #5): если основная модель
            # не ответила (API Error / Cannot connect / 502 / 503), повторяем
            # запрос, подставляя COMMAND_ARGS_ALTERNATIVE, и чередуем базовый и
            # альтернативный наборы до RETRY_LIMIT, с паузой _retry_backoff_delay.
            # Если alt-набор не задан — просто повторяем базовыми аргументами.
            variants = [None]
            if config.COMMAND_ARGS_ALTERNATIVE:
                variants.append(config.COMMAND_ARGS_ALTERNATIVE)
            max_attempts = max(1, config.RETRY_LIMIT)

            result = None
            for attempt in range(1, max_attempts + 1):
                # Чередуем: попытка 1 — базовые, 2 — alt, 3 — базовые, ...
                chosen = variants[(attempt - 1) % len(variants)]
                logger.info("Попытка %s/%s (args=%s)", attempt, max_attempts,
                            "базовые" if chosen is None else "альтернативные")
                result = await run_claude(
                    prompt,
                    cwd=project,
                    resume_session_id=resume_session_id,
                    image_paths=image_paths,
                    proc_registry=_bot_proc_pids,
                    command_args=chosen,
                )
                # Модель ответила или исчерпали лимит — выходим; иначе пауза и
                # следующая попытка (с др. набором, если есть альтернатива).
                if not _is_model_unavailable(result) or attempt == max_attempts:
                    break
                logger.warning("Модель недоступна (попытка %s), меняю набор аргументов", attempt)
                await asyncio.sleep(_retry_backoff_delay(attempt))
            # Ошибка модели/обёртки (отвалилась, вернула is_error) — показываем
            # ❌ в консольном статусе, а не только «Работаю». Текст в Telegram
            # уходит как есть, чтобы пользователь видел причину.
            if _is_run_error(result):
                _report_run_error(result.text or f"Ошибка Claude (код {result.exit_code})")
            text = result.text
            if not text:
                # При /clear Claude сбрасывает контекст и отвечает пустотой —
                # показываем осмысленное сообщение вместо «(пустой ответ)».
                parts = prompt.split()
                if parts and parts[0].lower() == "/clear":
                    text = "🧹 Контекст очищен — начата новая сессия."
                else:
                    text = "(пустой ответ)"
            # Telegram режет сообщение на 4096 символов — ответ длиннее лимита
            # шлём несколькими сообщениями (по границам абзацев), чтобы ничего
            # не обрезать. Сначала ответ, потом убираем «работаю»: если удаление
            # индикатора зависнет (сбой MTProto-прокси), ответ всё равно дойдёт.
            await _send_split(message, text)
            await _cleanup_busy()
        except FileNotFoundError as e:
            await _cleanup_busy()
            await _send_with_retry(message, str(e))
        except ValueError as e:
            await _cleanup_busy()
            await _send_with_retry(message, str(e))
        except asyncio.CancelledError:
            # Задачу отменили (команда /kill) — убираем «Думаю...» и пробрасываем.
            await _cleanup_busy()
            raise
        except Exception as e:
            await _cleanup_busy()
            await _send_with_retry(message, f"⚠️ Ошибка: {e}")
    finally:
        # Автоудаление вложений. Если AUTO_DELETE_MEDIA выключен — НЕ удаляем:
        # файлы остаются в проекте, очистить можно вручную командой /clearmedia.
        if config.AUTO_DELETE_MEDIA:
            for p in image_paths or []:
                try:
                    _delete_path(Path(p))
                except Exception:
                    pass
            # Пустую подпапку вложений подчищаем, если она опустела.
            try:
                media_dir = project / ".claude_tg_bot_media"
                if media_dir.exists() and not any(media_dir.iterdir()):
                    media_dir.rmdir()
            except Exception:
                pass


async def _handle_attachment(
    client,
    message: Message,
    kind: str,
    ext: str,
    sandbox: bool = False,
    prompt_override: Optional[str] = None,
    sniff_ext: bool = False,
):
    """Общая логика обработки вложения: фото/видео/звук/файл → в claude.

    Файл кладём внутрь активного проекта (а не в /tmp), чтобы @путь из промпта
    гарантированно читался claude как вложение (вложение должно быть в cwd
    процесса). После обработки удаляется, только если AUTO_DELETE_MEDIA включён,
    иначе файл остаётся (очистка — вручную через /clearmedia).

    Подпись (caption) становится промптом. Если подписи нет — уходим в claude
    с ПУСТЫМ промптом: он сам увидит @вложение и сохранит его в контексте
    сессии. Если подпись — команда (/...) — обрабатываем её как on_command,
    вложение не в дело.

    Часть проверок отдаём самому claude: он сам решает, как обработать файл
    (сам извлечёт кадры из видео или транскрибирует звук). Мы НЕ проверяем
    доступность внешних утилит заранее и не отказываем — просто отдаём файл,
    а claude сам разрулит.

    media — объект вложения (photo/video/audio/voice/document).
    kind — человеческое имя («фото», «видео», «аудио», «файл»).
    ext — расширение файла (.jpg, .mp4, .mp3, .pdf, ...).
    sniff_ext — для фото: после скачивания определить реальное расширение по
    содержимому (PNG/JPEG/WebP/GIF) и переименовать файл, чтобы claude видел
    верный тип (объект photo в Pyrogram не несёт mime_type/file_name).

    sandbox=True — контекст @helpbot: работаем в SANDBOX_ROOT (или проекте песочницы),
    а не в обычном project_root. prompt_override — заменяет caption для @helpbot.
    """
    user_id = _author(message)
    st = store.get(user_id)
    if not st:
        await _reply(message, "Сначала выбери проект: /list или /new <имя>")
        return

    # Рабочий каталог: проект песочницы (иначе корень SANDBOX_ROOT) или обычный.
    if sandbox:
        project = Path(st.sandbox_project_root or config.SANDBOX_ROOT)
    else:
        if not st.project_root:
            await _reply(message, "Сначала выбери проект: /list или /new <имя>")
            return
        project = Path(st.project_root)

    # Подпапка внутри проекта для временных вложений (удаляется вместе с файлом)
    media_dir = project / ".claude_tg_bot_media"
    media_dir.mkdir(parents=True, exist_ok=True)

    caption = prompt_override if prompt_override is not None else (message.caption or "").strip()

    # Для фото (sniff_ext) скачиваем с нейтральным расширением, затем уточним
    # реальное по содержимому и переименуем — чтобы @путь и тип были верными.
    download_ext = ".img" if sniff_ext else ext
    fname = f"claude_tg_bot_{kind}_{time.time_ns()}{download_ext}"
    media_path = media_dir / fname

    try:
        await message.download(file_name=str(media_path))
    except Exception as e:
        await _reply(message, f"Не удалось скачать {kind}: {e}")
        return

    if sniff_ext:
        real_ext = _sniff_image_ext(media_path)
        if real_ext != download_ext:
            renamed = media_path.with_suffix(real_ext)
            try:
                media_path.rename(renamed)
                media_path = renamed
            except OSError:
                pass  # не смогли переименовать — оставляем как скачали

    if caption.startswith("/"):
        # Подпись — команда (напр. /status): обрабатываем её, вложение не в дело.
        media_path.unlink(missing_ok=True)
        await on_command(client, message, caption, sandbox=sandbox)
        return

    # Вложение с подписью или без: caption становится промптом. Если подписи нет —
    # уходим в claude с ПУСТЫМ промптом: он сам увидит @файл и сохранит его в
    # контексте сессии (никакой заглушки). Обрабатываем в фоне, чтобы не
    # блокировать последующие сообщения.
    # Единый алгоритм: _run_and_reply сам резолвит последнюю сессию каталога.
    _start_bg(_run_and_reply(client, message, st, caption, [media_path], cwd=str(project)))


async def on_photo(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Расширение определяем по содержимому скачанного файла (PNG/JPEG/WebP/GIF),
    т.к. Pyrogram-объект photo не несёт file_name/mime_type. Fallback — .jpg,
    чтобы claude всегда видел картинку."""
    media = message.photo
    if media is None:
        return
    await _handle_attachment(client, message, "фото", ".jpg", sandbox, prompt_override, sniff_ext=True)


async def on_audio(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Обработка звука/голосового → в claude (он сам транскрибирует/разберёт)."""
    media = message.audio or message.voice
    if media is None:
        return
    await _handle_attachment(client, message, "аудио", _media_ext(media), sandbox, prompt_override)


async def on_video(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Обработка видео → в claude (он сам извлечёт кадры и разберёт)."""
    media = message.video
    if media is None:
        return
    await _handle_attachment(client, message, "видео", _media_ext(media), sandbox, prompt_override)


async def on_video_note(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Обработка видеосообщения-кружка (video_note) как видео.

    Кружок — круглое видеосообщение. Отдаём его claude как видео: он сам
    извлечёт кадры и разберёт содержание. Расширение — по mime (обычно .mp4).
    """
    media = message.video_note
    if media is None:
        return
    ext = _media_ext(media)
    if ext == ".bin":  # у кружка нет file_name; без mime считаем .mp4
        ext = ".mp4"
    await _handle_attachment(client, message, "видео-кружок", ext, sandbox, prompt_override)


async def on_document(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Обработка произвольного файла (документ/GIF-анимация) → в claude."""
    media = message.document or message.animation
    if media is None:
        return
    await _handle_attachment(client, message, "файл", _media_ext(media), sandbox, prompt_override)


async def on_sticker(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Обработка стикера → в claude как изображение-вложение.

    Стикер — это файл (.webp/.tgs/.webm), скачиваем и отдаём claude: он сам
    увидит картинку. Если подписи нет — уходим в claude с пустым промптом.
    """
    media = message.sticker
    if media is None:
        return
    ext = _media_ext(media)
    if ext == ".bin":  # стикер без mime — обычно .webp
        ext = ".webp"
    await _handle_attachment(client, message, "стикер", ext, sandbox, prompt_override)


async def _handle_textual_media(client, message, sandbox: bool = False):
    """Отправить в claude текстовое описание медиа (опрос/гео/контакт).

    on_chat сам запускает claude фоновой задачей (_start_bg внутри), поэтому
    здесь лишь формируем промпт и передаём его в on_chat.
    """
    prompt = _textual_media_prompt(message)
    if not prompt:
        return
    await on_chat(client, message, prompt, sandbox=sandbox)


async def _on_unknown_media(client, message: Message):
    """Неизвестный тип вложения: сообщаем «не могу обработать» + указываем тип."""
    kind = _media_type_name(message) or "неизвестное вложение"
    await _reply(message, f"🤖 Не могу обработать: {kind}.")