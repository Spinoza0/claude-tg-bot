"""Обработка команд бота: /start /list /switch /new /status /kill /clean /help.

Плюс команды очистки вложений (/clearmedia, /mediasize), которые вызываются и
напрямую, и автоматически из /clear (см. handlers.on_chat).
"""

from pathlib import Path

from pyrogram.types import Message

from . import config
from .runner import format_command_hint
from .sessions import find_latest_session, is_safe_project_name, store
from .access import _author
from .process import _active_tasks, _bot_proc_pids, kill_bot_procs
from .media import _delete_path, _dir_size, _fmt_bytes
from .reply import _reply


async def on_command(client, message: Message, text: str, sandbox: bool = False):
    """Обработка команд: /start /list /switch /new /status /clean /help.

    sandbox=True — командный контекст @helpbot: работает внутри SANDBOX_ROOT и над
    отдельным выбором проекта st.sandbox_project_root (не пересекается с обычным
    стейтом, который живёт в PROJECTS_ROOT).
    """
    parts = text.split()
    cmd = parts[0].lower()
    user_id = _author(message)
    st = store.get_or_init(user_id)
    # Корень и активный проект зависят от режима: обычный (PROJECTS_ROOT) или
    # песочница (SANDBOX_ROOT). Ниже всё работает через root/active.
    root = config.SANDBOX_ROOT if sandbox else config.PROJECTS_ROOT
    label = "SANDBOX_ROOT" if sandbox else "PROJECTS_ROOT"
    active = st.get_active_root(sandbox)
    active_name = st.active_name(sandbox)

    if cmd == "/start" or cmd == "/help":
        box = [f"👋 Привет! Я бот для работы с Claude через Telegram (v{config.BOT_VERSION}).\n"]
        box.append(f"Корень проектов ({label}): {root}")
        if sandbox:
            box.append(f"Режим: {config.SANDBOX_COMMAND} (работа в {label})\n")
        if not active:
            box.append("Выбери проект командой /list или создай новый через /new.\n")
        else:
            box.append(f"Текущий проект: {active_name} ({active})\n")
        box.append("")
        box.append(format_command_hint())
        await _reply(message, "\n".join(box))

    elif cmd == "/list":
        projects = store.list_projects(user_id, root=root)
        if not projects:
            await _reply(message,
                "Нет проектов. Создай первый: /new имя_проекта\n"
                f"(корень проектов: {root})"
            )
            return
        lines = [f"📁 Проекты (в {root}):"]
        for p in projects:
            star = "⭐" if active == str(p) else "•"
            lines.append(f"{star} /switch {p.name}")
        await _reply(message, "\n".join(lines))

    elif cmd == "/switch":
        name = parts[1] if len(parts) > 1 else ""
        if not is_safe_project_name(name):
            await _reply(message, "Неверное имя проекта. /switch <имя>")
            return
        proj = root / name
        if not proj.is_dir():
            await _reply(message, f"Проект {name} не найден. Смотри /list")
            return
        st.set_active(sandbox, str(proj), name)
        # НЕ сбрасываем сессию: на этом проекте своя сессия, при следующем
        # запросе find_latest_session подхватит её с диска для --resume.
        store.update(st)
        await _reply(message, f"✅ Переключился на проект: {name}")

    elif cmd == "/new":
        name = parts[1] if len(parts) > 1 else f"proj_{user_id}"
        if not is_safe_project_name(name):
            await _reply(message, "Неверное имя проекта. /new <имя>")
            return
        proj = root / name
        proj.mkdir(parents=True, exist_ok=True)
        st.set_active(sandbox, str(proj), name)
        store.update(st)
        await _reply(message, f"✅ Создан и активирован проект: {name}")

    elif cmd == "/status":
        st = store.get(user_id) or st
        active = st.get_active_root(sandbox)
        lines = [f"📊 Статус (v{config.BOT_VERSION}):\nПроект: {st.active_name(sandbox) or '(не выбран)'}"]
        lines.append(f"Корень проектов ({label}): {root}")
        lines.append(f"Активный путь: {active or '/'}")
        _sid = find_latest_session(Path(active))
        lines.append(f"Сессия: {_sid or '(новая)'}")
        lines.append(
            f"Запущено ботом: {len(_bot_proc_pids)} задач, "
            f"активных: {len(_active_tasks)}"
        )
        await _reply(message, "\n".join(lines))

    elif cmd == "/kill":
        # Прибить ТОЛЬКО зависшие claude-кластеры, запущенные ЭТИМ ботом.
        # Не трогаем чужие/ручные сессии (они не в _bot_proc_pids).
        killed, errs = kill_bot_procs()
        # Отменяем фоновые задачи, чтобы _run_and_reply дошёл до finally и
        # дочистил файлы, а run_claude убил свои дочерние процессы.
        for t in list(_active_tasks):
            t.cancel()
        msg = f"🔪 Закрыто процессов (сессий бота): {killed}"
        if errs:
            msg += f"\n⚠️ не удалось: {errs}"
        await _reply(message, msg)

    elif cmd == "/clearmedia":
        # Удалить скачанные вложения (.claude_tg_bot_media) в текущем каталоге.
        # В корзину или навсегда — по DELETE_MODE. Сообщаем сколько удалили.
        await _clear_media(message, active, sandbox=sandbox, root=str(root))

    elif cmd == "/mediasize":
        await _media_size(message, active, root=str(root))

    else:
        return


async def _clear_media(message, active: str, sandbox: bool = False, root: str = "", silent: bool = False):
    """Команда /clearmedia: удалить скачанные вложения текущего проекта.

    Удаляет подпапку .claude_tg_bot_media внутри активного каталога целиком
    (в т.ч. пустую — чтобы не оставалось следов), по DELETE_MODE. Считает,
    сколько файлов и какой объём освобождается.

    silent=True — автовызов из /clear: если каталога с вложениями НЕТ, ничего
    не шлём и молча возвращаемся (чтобы очистка не отвечала «чистить нечего»).
    Если каталог был пуст — удаляем его и тоже сообщаем об этом.
    """
    base = Path(active) if active else Path(root)
    media_dir = base / ".claude_tg_bot_media"

    if not media_dir.exists():
        if not silent:
            await _reply(message, "Нет скачанных вложений — чистить нечего.")
        return

    files = [p for p in media_dir.rglob("*") if p.is_file()]
    count = len(files)
    size = _dir_size(media_dir)
    free = _fmt_bytes(size)

    mode = (config.DELETE_MODE or "trash").lower()
    where = "в корзину" if mode == "trash" else "навсегда"

    try:
        _delete_path(media_dir, mode)
    except Exception as e:
        await _reply(message, f"⚠️ Не удалось очистить вложения: {e}")
        return

    if count:
        await _reply(
            message,
            f"🧹 Удалён каталог вложений ({count} файл(ов), ~{free}) — {where}.\n"
            f"{media_dir}",
        )
    else:
        await _reply(message, f"🧹 Удалён пустой каталог вложений.\n{media_dir}")


async def _media_size(message, active: str, root: str = ""):
    """Команда /mediasize: показать количество и объём скачанных вложений.

    Не удаляет ничего — только отчёт по .claude_tg_bot_media активного каталога.
    """
    base = Path(active) if active else Path(root)
    media_dir = base / ".claude_tg_bot_media"

    if not media_dir.exists() or not any(media_dir.iterdir()):
        await _reply(message, "Нет скачанных вложений в активном проекте.")
        return

    files = [p for p in media_dir.rglob("*") if p.is_file()]
    count = len(files)
    size = _dir_size(media_dir)

    await _reply(
        message,
        f"📎 Вложения ({count} файл(ов), ~{_fmt_bytes(size)}):\n"
        f"Каталог: {media_dir}",
    )