"""Bot command handling: /start /list /switch /new /status /kill /clean /help.

Plus the attachment-cleanup commands (/clearattach, /attachsize), called both
directly and automatically from /clear (see handlers.on_chat).
"""

from pathlib import Path

from pyrogram.types import Message

from . import config, i18n
from .runner import format_command_hint
from .sessions import find_latest_session, is_safe_project_name, store
from .access import _author
from .process import _active_tasks, _bot_proc_pids, kill_bot_procs
from .attach import _delete_path, _dir_size, _fmt_bytes
from .reply import _reply

# Reserved slash commands handled by on_command (lowercase, without the '/').
# Used to validate that SANDBOX_COMMAND never shadows a bot command (see setup.py).
RESERVED_COMMANDS = {
    "start", "help", "list", "switch", "new", "status", "lang",
    "kill", "clearattach", "attachsize",
}


async def on_command(client, message: Message, text: str, sandbox: bool = False):
    """Command handling: /start /list /switch /new /status /clean /help.

    sandbox=True — the @helpbot command context: works inside SANDBOX_ROOT and
    over a separate project choice st.sandbox_project_root (does not overlap the
    regular state, which lives in PROJECTS_ROOT).
    """
    parts = text.split()
    cmd = parts[0].lower()
    user_id = _author(message)
    st = store.get_or_init(user_id)
    # The root and active project depend on the mode: regular (PROJECTS_ROOT) or
    # sandbox (SANDBOX_ROOT). Below everything works via root/active.
    root = config.SANDBOX_ROOT if sandbox else config.PROJECTS_ROOT
    label = "SANDBOX_ROOT" if sandbox else "PROJECTS_ROOT"
    active = st.get_active_root(sandbox)
    active_name = st.active_name(sandbox)

    if cmd == "/start" or cmd == "/help":
        box = [i18n.t("cmd.start_hello", version=config.BOT_VERSION) + "\n"]
        box.append(i18n.t("cmd.projects_root", label=label, root=root))
        if sandbox:
            box.append(i18n.t("cmd.sandbox_mode", cmd=config.SANDBOX_COMMAND, label=label))
        if not active:
            box.append(i18n.t("cmd.no_project") + "\n")
        else:
            box.append(i18n.t("cmd.current_project", name=active_name, path=active) + "\n")
        box.append("")
        box.append(format_command_hint())
        await _reply(message, "\n".join(box))

    elif cmd == "/list":
        projects = store.list_projects(user_id, root=root)
        if not projects:
            await _reply(message,
                i18n.t("cmd.list_empty", root=root)
            )
            return
        lines = [i18n.t("cmd.list_header", root=root)]
        for p in projects:
            star = "⭐" if active == str(p) else "•"
            lines.append(f"{star} /switch {p.name}")
        await _reply(message, "\n".join(lines))

    elif cmd == "/switch":
        name = parts[1] if len(parts) > 1 else ""
        if not is_safe_project_name(name):
            await _reply(message, i18n.t("cmd.invalid_project_name"))
            return
        proj = root / name
        if not proj.is_dir():
            await _reply(message, i18n.t("cmd.project_not_found", name=name))
            return
        st.set_active(sandbox, str(proj), name)
        # We do NOT reset the session: this project has its own session, and on
        # the next request find_latest_session picks it from disk for --resume.
        store.update(st)
        await _reply(message, i18n.t("cmd.switched", name=name))

    elif cmd == "/new":
        name = parts[1] if len(parts) > 1 else f"proj_{user_id}"
        if not is_safe_project_name(name):
            await _reply(message, i18n.t("cmd.invalid_project_name"))
            return
        proj = root / name
        proj.mkdir(parents=True, exist_ok=True)
        st.set_active(sandbox, str(proj), name)
        store.update(st)
        await _reply(message, i18n.t("cmd.created", name=name))

    elif cmd == "/status":
        st = store.get(user_id) or st
        active = st.get_active_root(sandbox)
        lines = [i18n.t("cmd.status_header", version=config.BOT_VERSION,
                        name=st.active_name(sandbox) or i18n.t("cmd.name_not_selected"))]
        lines.append(i18n.t("cmd.status_root", label=label, root=root))
        lines.append(i18n.t("cmd.status_active", path=active or "/"))
        _sid = find_latest_session(Path(active))
        lines.append(i18n.t("cmd.status_session", sid=_sid or "(new)"))
        lines.append(
            i18n.t("cmd.status_tasks", tasks=len(_bot_proc_pids), active=len(_active_tasks))
        )
        await _reply(message, "\n".join(lines))

    elif cmd == "/lang":
        # Switch language on the fly: without an argument — show current and
        # available; with one (e.g. "ru"/"en") — switch and write to config.env.
        lang_arg = parts[1] if len(parts) > 1 else ""
        available = i18n.available_langs()
        if not lang_arg:
            current = i18n.current_lang()
            await _reply(
                message,
                i18n.t("cmd.lang_current", current=current, langs=", ".join(available)),
            )
            return
        new_code = i18n.set_lang(lang_arg)
        if new_code != lang_arg.strip().lower():
            # Code not among the available ones — we stayed on the previous lang.
            await _reply(message, i18n.t("cmd.lang_invalid", code=lang_arg, langs=", ".join(available)))
            return
        if config.write_lang_to_config(new_code):
            await _reply(message, i18n.t("cmd.lang_ok", code=new_code))
        else:
            # config.env not found — switch only for the current session.
            await _reply(message, i18n.t("cmd.lang_ok_ephemeral", code=new_code))

    elif cmd == "/kill":
        # Kill ONLY hung claude clusters launched BY THIS bot.
        # We don't touch foreign/manual sessions (they are not in _bot_proc_pids).
        killed, errs = kill_bot_procs()
        # Cancel the background tasks so _run_and_reply reaches finally and
        # cleans up files, and run_claude kills its child processes.
        for t in list(_active_tasks):
            t.cancel()
        msg = i18n.t("cmd.killed", killed=killed)
        if errs:
            msg += i18n.t("cmd.kill_failed", errs=errs)
        await _reply(message, msg)

    elif cmd == "/clearattach":
        # Delete downloaded attachments (.claude_tg_bot_attach) in the current dir.
        # To trash or permanently — per DELETE_MODE. Report how many were deleted.
        await _clear_attach(message, active, sandbox=sandbox, root=str(root))

    elif cmd == "/attachsize":
        await _attach_size(message, active, root=str(root))

    else:
        return


async def _clear_attach(message, active: str, sandbox: bool = False, root: str = "", silent: bool = False):
    """/clearattach command: delete the current project's downloaded attachments.

    Removes the .claude_tg_bot_attach subfolder inside the active directory as a
    whole (including an empty one — so no traces stay), per DELETE_MODE. Counts
    how many files and how much space is freed.

    silent=True — auto-call from /clear: if there's no attachments folder, send
    nothing and return silently (so the cleanup doesn't answer "nothing to
    clean"). If the folder was empty — delete it and report that too.
    """
    base = Path(active) if active else Path(root)
    attach_dir = base / ".claude_tg_bot_attach"

    if not attach_dir.exists():
        if not silent:
            await _reply(message, i18n.t("cmd.clear_attach_none"))
        return

    files = [p for p in attach_dir.rglob("*") if p.is_file()]
    count = len(files)
    size = _dir_size(attach_dir)
    free = _fmt_bytes(size)

    mode = (config.DELETE_MODE or "trash").lower()
    where = i18n.t("cmd.trash") if mode == "trash" else i18n.t("cmd.permanent")

    try:
        _delete_path(attach_dir, mode)
    except Exception as e:
        await _reply(message, i18n.t("cmd.clear_attach_err", e=e))
        return

    if count:
        await _reply(
            message,
            i18n.t("cmd.clear_attach_done", count=count, size=free, where=where, path=attach_dir),
        )
    else:
        await _reply(message, i18n.t("cmd.clear_attach_empty_done", path=attach_dir))


async def _attach_size(message, active: str, root: str = ""):
    """/attachsize command: show the count and size of downloaded attachments.

    Deletes nothing — only a report over the active directory's .claude_tg_bot_attach.
    """
    base = Path(active) if active else Path(root)
    attach_dir = base / ".claude_tg_bot_attach"

    if not attach_dir.exists() or not any(attach_dir.iterdir()):
        await _reply(message, i18n.t("cmd.attach_size_none"))
        return

    files = [p for p in attach_dir.rglob("*") if p.is_file()]
    count = len(files)
    size = _dir_size(attach_dir)

    await _reply(
        message,
        i18n.t("cmd.attach_size", count=count, size=_fmt_bytes(size), path=attach_dir),
    )