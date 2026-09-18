"""Telegram message handlers: sandbox, attachments, regular chat, launching Claude.

The central "orchestrator" tying together project work (commands), attachments
(media), the sandbox (sandbox) and Claude launch (runner/sessions).
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from pyrogram.types import Message

from . import config, i18n

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
    """Handle a sandbox launch by SANDBOX_COMMAND: works in SANDBOX_ROOT regardless of chat.

    The message must start with the config.SANDBOX_COMMAND trigger (default
    '@helpbot'), then a space and a command/text (with or without an image). It
    works in ANY chat the bot is a member of, as long as the sender is an allowed
    user (ALLOWED_USERS). The chat restriction (ALLOWED_CHAT_IDS) is NOT applied
    to this trigger.

    The remainder after the trigger is processed like a regular bot message, but
    in the sandbox context (root SANDBOX_ROOT, separate project choice):
      - a command (/list /new /switch /status /kill ...) → on_command(sandbox=True)
      - /clear → on_chat(sandbox=True) (this is a prompt for claude)
      - plain text / attachment → on_chat / on_photo|on_video|on_audio|on_document(sandbox=True).

    If after stripping the trigger and the whitespace an empty string is left and
    there's no attached image — the message is ignored (we send nothing).
    """
    user_id = _author(message)
    st = store.get(user_id)
    if not st:
        st = store.get_or_init(user_id)

    # The directory the sandbox works in: SANDBOX_ROOT or the default sandbox.
    try:
        config.SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        await _reply(message, i18n.t("handlers.sandbox_create_fail", path=config.SANDBOX_ROOT))
        return

    rest = _strip_sandbox_prefix(message.text or (message.caption or ""))
    has_media = _has_any_media(message)

    # Empty and no attachment — nothing to process, ignore silently.
    if not rest.strip() and not has_media:
        return

    # The remainder starts with '/' — it's a bot command (or /clear for claude).
    # An attachment is irrelevant for a command. We work in the sandbox context (sandbox=True).
    if rest.strip().startswith("/"):
        cmd = rest.strip().split()[0].lower()
        if cmd == "/clear":
            # /clear — not a bot command but a prompt for claude (it resets the context itself).
            await on_chat(client, message, rest.strip(), sandbox=True)
        else:
            await on_command(client, message, rest.strip(), sandbox=True)
        return

    # Plain text or attachment (photo/video/document): go to claude in SANDBOX_ROOT.
    if has_media:
        # Pass the stripped remainder as the caption (in caption it still has '@helpbot').
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
            # Unknown attachment type under @helpbot — tell the user we can't.
            await _on_unknown_media(client, message)
    else:
        await on_chat(client, message, rest, sandbox=True)


async def on_all_message(client, message: Message):
    """Filter for all messages: access check + command interception.

    The bot answers ONLY in a specific chat (from ALLOWED_CHAT_IDS) and only if
    the sender is an allowed user (ALLOWED_USERS). Everything else (other chats,
    foreign members) is silently ignored.
    """
    # Ignore our own outgoing replies: the bot == our account, so its answers
    # ("working", the result) come as outgoing + reply. Otherwise the bot would
    # loop on itself. Your regular messages (not replies) are processed.
    if message.outgoing and message.reply_to_message_id is not None:
        return

    user_id = _author(message)
    # @helpbot works "in any chat" — we restrict ONLY by the allowed user, not by
    # the chat. Regular messages — via _allowed (user + chat).
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
        # A video-note (circle) — treat as video.
        await on_video_note(client, message)
        return
    if g(message, "audio", None) or g(message, "voice", None):
        await on_audio(client, message)
        return
    if g(message, "document", None) or g(message, "animation", None):
        await on_document(client, message)
        return
    if g(message, "sticker", None):
        # A sticker — an image file, hand it to claude.
        await on_sticker(client, message)
        return
    if g(message, "poll", None) or g(message, "location", None) \
            or g(message, "venue", None) or g(message, "contact", None):
        # Poll/geo/contact: no file, but there is data → a textual description for claude.
        await _handle_textual_media(client, message)
        return
    # Unknown/undetected attachment type — tell "can't process it".
    if _has_any_media(message):
        await _on_unknown_media(client, message)
        return
    # /clear — not a bot command but a prompt for claude (claude handles it itself).
    # Therefore we send it to on_chat, not on_command.
    # IMPORTANT: compare EXACTLY (cmd == "/clear"), not startswith("/clear"),
    # otherwise it clashes with /clearmedia (it also starts with /clear).
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
    """Regular message → launch claude in the active project.

    We launch claude as a BACKGROUND task (create_task), not wait for it here.
    Otherwise with workers=1 the handler holds a worker while claude thinks
    (minutes), and all subsequent messages — incl. commands /help, /kill — queue
    up and are not processed. In the background commands run immediately.

    sandbox=True — the @helpbot context: we work in SANDBOX_ROOT (or the chosen
    sandbox project st.sandbox_project_root), not the regular project_root.
    """
    user_id = _author(message)
    st = store.get(user_id)
    if not st:
        await _reply(message, i18n.t("handlers.no_project"))
        return

    # The working directory depends on the mode:
    #  - regular: a chosen project is required (else an error);
    #  - @helpbot: the chosen sandbox project, else the SANDBOX_ROOT (the sandbox itself).
    if not sandbox:
        if not st.project_root:
            await _reply(message, i18n.t("handlers.no_project"))
            return
        project_root = st.project_root
    else:
        project_root = st.sandbox_project_root or str(config.SANDBOX_ROOT)

    # /clear — full reset: first clean the downloaded attachments, then send
    # /clear to claude (it resets the session context itself). Otherwise files stay.
    if text.split()[0].lower() == "/clear":
        root = config.SANDBOX_ROOT if sandbox else config.PROJECTS_ROOT
        # silent: if there's no attachments folder — go on silently to Claude.
        await _clear_media(message, project_root, sandbox=sandbox, root=str(root), silent=True)

    # We don't pass session_id — _run_and_reply finds the latest session of the
    # directory itself and does --resume, or starts a new one.
    _start_bg(_run_and_reply(client, message, st, text, [], cwd=project_root))


async def _run_and_reply(client, message, st, prompt: str, image_paths, resume_session_id=None, cwd=None):
    """The common point: launch claude + output the result.

    Before processing we send "working", remember its id, and when claude has
    answered — delete "working" and send the reply. This signals the bot is alive,
    not hung. image_paths — temporary attachments (images) inside the project.
    After processing they're deleted only if AUTO_DELETE_MEDIA is on (per
    DELETE_MODE), otherwise they stay in .claude_tg_bot_media until a manual
    /clearmedia.

    cwd — Claude's working directory. By default st.project_root (the active
    project). For @helpbot config.SANDBOX_ROOT (the sandbox) is passed.

    resume_session_id — the session id to continue via --resume. If not given
    explicitly, we find the directory's latest session from disk
    (find_latest_session) so all entry points (on_chat, on_photo, @helpbot) behave
    identically. If there's no session — a new one starts and its id comes back in
    result.session_id.
    """
    project = Path(cwd) if cwd else Path(st.project_root)
    if not resume_session_id:
        resume_session_id = find_latest_session(project)
    # Send a working indicator and remember its id (chat_id + message_id).
    # The indicator is sent with retries (like the reply): on an inter-DC error
    # ([500 INTERDC_X_CALL_ERROR]) we make a few attempts. If ALL fail — skip the
    # indicator and continue, so the claude launch isn't blocked (otherwise the
    # bot "goes silent" in chats outside the main DC).
    busy = None
    async def _send_busy():
        return await asyncio.wait_for(_reply(message, i18n.t("handlers.busy")), timeout=5)
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
                # A short timeout: with an unavailable MTProto-proxy Pyrogram
                # would otherwise retry "messages.DeleteMessages" forever and not
                # let the work finish (Ctrl+C) and/or the task be cancelled.
                await asyncio.wait_for(
                    client.delete_messages(busy_chat, busy_msg_id), timeout=3
                )
            except Exception:
                pass

    try:
        try:
            # Model switch when it's unavailable (issue #5): if the main model
            # didn't answer (API Error / Cannot connect / 502 / 503), we retry
            # with COMMAND_ARGS_ALTERNATIVE substituted, alternating the base and
            # alternative sets up to RETRY_LIMIT, with a _retry_backoff_delay pause.
            # If the alt set isn't set — just retry with the base arguments.
            variants = [None]
            if config.COMMAND_ARGS_ALTERNATIVE:
                variants.append(config.COMMAND_ARGS_ALTERNATIVE)
            max_attempts = max(1, config.RETRY_LIMIT)

            result = None
            # Flags for the Telegram model-switch notification: we send a message
            # exactly once on the first switch to the alternative argument set and
            # once when the fallback model answers — so we don't spam the user
            # while alternating base/alternative attempts.
            switch_notified = False
            was_on_alt = False
            for attempt in range(1, max_attempts + 1):
                # Alternate: attempt 1 — base, 2 — alt, 3 — base, ...
                chosen = variants[(attempt - 1) % len(variants)]
                is_alt = chosen is not None
                logger.info("Attempt %s/%s (args=%s)", attempt, max_attempts,
                            "base" if chosen is None else "alternative")
                if is_alt and not switch_notified:
                    # The main model didn't answer on the previous attempt — tell
                    # the user we're switching to the alternative argument set.
                    switch_notified = True
                    await _send_with_retry(
                        message,
                        i18n.t("handlers.switch_notify"),
                    )
                if is_alt:
                    was_on_alt = True
                result = await run_claude(
                    prompt,
                    cwd=project,
                    resume_session_id=resume_session_id,
                    image_paths=image_paths,
                    proc_registry=_bot_proc_pids,
                    command_args=chosen,
                )
                # The model answered or the limit is reached — exit; else pause and
                # next attempt (with a diff. set if there's an alternative).
                if not _is_model_unavailable(result):
                    if was_on_alt and switch_notified:
                        # The fallback model we switched to has answered.
                        await _send_with_retry(
                            message,
                            i18n.t("handlers.alt_ok"),
                        )
                    break
                if attempt == max_attempts:
                    break
                logger.warning("Model unavailable (attempt %s), switching argument set", attempt)
                await asyncio.sleep(_retry_backoff_delay(attempt))
            # A model/wrapper error (went down, returned is_error) — show ❌ in the
            # console status, not just "Working". The text goes to Telegram as-is
            # so the user sees the reason.
            if _is_run_error(result):
                _report_run_error(result.text or i18n.t("handlers.run_error", code=result.exit_code))
            text = result.text
            if not text:
                # On /clear Claude resets the context and answers with emptiness —
                # show a meaningful message instead of "empty reply".
                parts = prompt.split()
                if parts and parts[0].lower() == "/clear":
                    text = i18n.t("handlers.clear_done_prompt")
                else:
                    text = i18n.t("handlers.empty_response")
            # Telegram caps a message at 4096 chars — a longer answer is sent as
            # several messages (by paragraph boundaries) so nothing is truncated.
            # The reply first, then remove "working": if deleting the indicator
            # hangs (MTProto-proxy failure) the reply still reaches.
            await _send_split(message, text)
            await _cleanup_busy()
        except FileNotFoundError as e:
            await _cleanup_busy()
            await _send_with_retry(message, str(e))
        except ValueError as e:
            await _cleanup_busy()
            await _send_with_retry(message, str(e))
        except asyncio.CancelledError:
            # The task was cancelled (command /kill) — remove "Thinking..." and propagate.
            await _cleanup_busy()
            raise
        except Exception as e:
            await _cleanup_busy()
            await _send_with_retry(message, i18n.t("handlers.error", e=e))
    finally:
        # Auto-delete the attachments. If AUTO_DELETE_MEDIA is off — we do NOT
        # delete: the files stay in the project, cleaned manually via /clearmedia.
        if config.AUTO_DELETE_MEDIA:
            for p in image_paths or []:
                try:
                    _delete_path(Path(p))
                except Exception:
                    pass
            # Tidy up the empty attachments subfolder if it became empty.
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
    """Common attachment handling logic: photo/video/audio/file → claude.

    We place the file inside the active project (not /tmp) so the @path in the
    prompt is guaranteed to be read by claude as an attachment (the attachment
    must be in the process's cwd). After processing it's deleted only if
    AUTO_DELETE_MEDIA is on, otherwise the file stays (cleanup via /clearmedia).

    The caption becomes the prompt. If there's no caption — we go to claude with
    an EMPTY prompt: it sees the @attachment itself and keeps it in the session
    context. If the caption is a command (/...) — we handle it as on_command;
    the attachment is irrelevant.

    Some checks are left to claude itself: it decides how to process the file
    (extracts frames from a video or transcribes audio itself). We do NOT check
    the availability of external utilities in advance and don't refuse — we just
    hand the file over and claude sorts it out.

    media — the attachment object (photo/video/audio/voice/document).
    kind — the human name ("photo", "video", "audio", "file").
    ext — the file extension (.jpg, .mp4, .mp3, .pdf, ...).
    sniff_ext — for photos: after download, detect the real extension by content
    (PNG/JPEG/WebP/GIF) and rename the file so claude sees the right type (the
    Pyrogram photo object carries no mime_type/file_name).

    sandbox=True — the @helpbot context: we work in SANDBOX_ROOT (or the sandbox
    project), not the regular project_root. prompt_override — replaces caption for @helpbot.
    """
    user_id = _author(message)
    st = store.get(user_id)
    if not st:
        await _reply(message, i18n.t("handlers.select_project"))
        return

    # The working directory: the sandbox project (else SANDBOX_ROOT) or the regular one.
    if sandbox:
        project = Path(st.sandbox_project_root or config.SANDBOX_ROOT)
    else:
        if not st.project_root:
            await _reply(message, i18n.t("handlers.select_project"))
            return
        project = Path(st.project_root)

    # A subfolder inside the project for temporary attachments (deleted with the file)
    media_dir = project / ".claude_tg_bot_media"
    media_dir.mkdir(parents=True, exist_ok=True)

    caption = prompt_override if prompt_override is not None else (message.caption or "").strip()

    # For photos (sniff_ext) we download with a neutral extension, then refine the
    # real one by content and rename — so the @path and the type are correct.
    download_ext = ".img" if sniff_ext else ext
    fname = f"claude_tg_bot_{kind}_{time.time_ns()}{download_ext}"
    media_path = media_dir / fname

    try:
        await message.download(file_name=str(media_path))
    except Exception as e:
        await _reply(message, i18n.t("handlers.download_fail", kind=kind, e=e))
        return

    if sniff_ext:
        real_ext = _sniff_image_ext(media_path)
        if real_ext != download_ext:
            renamed = media_path.with_suffix(real_ext)
            try:
                media_path.rename(renamed)
                media_path = renamed
            except OSError:
                pass  # couldn't rename — keep as downloaded

    if caption.startswith("/"):
        # The caption is a command (e.g. /status): handle it, the attachment is irrelevant.
        media_path.unlink(missing_ok=True)
        await on_command(client, message, caption, sandbox=sandbox)
        return

    # An attachment with or without a caption: the caption becomes the prompt. If
    # there's no caption — go to claude with an EMPTY prompt: it sees the @file
    # itself and keeps it in the session context (no placeholder). We process in
    # the background so subsequent messages aren't blocked.
    # A single algorithm: _run_and_reply resolves the directory's latest session itself.
    _start_bg(_run_and_reply(client, message, st, caption, [media_path], cwd=str(project)))


async def on_photo(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Detection by the downloaded file's content (PNG/JPEG/WebP/GIF), since the
    Pyrogram photo object carries no file_name/mime_type. Fallback — .jpg, so
    claude always sees an image."""
    media = message.photo
    if media is None:
        return
    await _handle_attachment(client, message, i18n.t("media.type_photo"), ".jpg", sandbox, prompt_override, sniff_ext=True)


async def on_audio(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Handle audio/voice → claude (it transcribes/parses it itself)."""
    media = message.audio or message.voice
    if media is None:
        return
    await _handle_attachment(client, message, i18n.t("media.type_audio"), _media_ext(media), sandbox, prompt_override)


async def on_video(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Handle video → claude (it extracts frames and parses it itself)."""
    media = message.video
    if media is None:
        return
    await _handle_attachment(client, message, i18n.t("media.type_video"), _media_ext(media), sandbox, prompt_override)


async def on_video_note(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Handle a video-note (circle) as video.

    A "circle" is a round video message. We hand it to claude as a video: it
    extracts frames and parses the content itself. The extension — by mime
    (usually .mp4).
    """
    media = message.video_note
    if media is None:
        return
    ext = _media_ext(media)
    if ext == ".bin":  # a circle has no file_name; without mime assume .mp4
        ext = ".mp4"
    await _handle_attachment(client, message, i18n.t("media.type_video_note"), ext, sandbox, prompt_override)


async def on_document(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Handle an arbitrary file (document/GIF animation) → claude."""
    media = message.document or message.animation
    if media is None:
        return
    await _handle_attachment(client, message, i18n.t("media.type_file"), _media_ext(media), sandbox, prompt_override)


async def on_sticker(client, message: Message, sandbox: bool = False, prompt_override: Optional[str] = None):
    """Handle a sticker → claude as an image attachment.

    A sticker is a file (.webp/.tgs/.webm); we download and hand it to claude — it
    sees the image itself. If there's no caption — go to claude with an empty prompt.
    """
    media = message.sticker
    if media is None:
        return
    ext = _media_ext(media)
    if ext == ".bin":  # a sticker without mime — usually .webp
        ext = ".webp"
    await _handle_attachment(client, message, i18n.t("media.type_sticker"), ext, sandbox, prompt_override)


async def _handle_textual_media(client, message, sandbox: bool = False):
    """Send a textual description of media (poll/geo/contact) to claude.

    on_chat launches claude as a background task itself (_start_bg inside), so
    here we only build the prompt and hand it to on_chat.
    """
    prompt = _textual_media_prompt(message)
    if not prompt:
        return
    await on_chat(client, message, prompt, sandbox=sandbox)


async def _on_unknown_media(client, message: Message):
    """Unknown attachment type: tell "can't process it" + show the type."""
    kind = _media_type_name(message) or i18n.t("media.type_unknown")
    await _reply(message, i18n.t("handlers.unknown_media", kind=kind))