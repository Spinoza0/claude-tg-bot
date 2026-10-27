"""Telegram client creation, the main() entry point, and retrying connections."""

import asyncio
import logging
from types import MethodType
import sys

import pyrogram
from pyrogram import Client, filters
from pyrogram import raw
from pyrogram.errors import AuthKeyUnregistered, SessionPasswordNeeded

from . import config, i18n
from .log import log_filename, maybe_cleanup_old_logs, parse_no_proxy, parse_log_flag, setup_logging
from .process import _active_tasks, acquire_single_instance
from .retry import _retry_backoff_delay
from .handlers import on_all_message


def _restore_terminal() -> None:
    """Put stdin back into the canonical (line-buffered, echoed) mode.

    A previous run that was killed while curses/rich owned the terminal can leave
    it in raw mode, which makes input() echo control chars (e.g. "y^M") and the
    prompt look shifted. Reset only if stdin is a real tty, and never raise.
    """
    if not sys.stdin.isatty():
        return
    try:
        import termios

        attrs = termios.tcgetattr(sys.stdin.fileno())
        attrs[3] |= termios.ICANON | termios.ECHO
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attrs)
    except Exception:
        pass
from .status import (
    _STATUS_FILTER,
    _STATUS_PREV_LINES,
    _friendly,
    _report_run_error,
    _status_loop,
    _use_color,
    ui_active,
    ui_close,
    ui_init,
    ui_log,
    ui_redirect_stdout,
)

# Bot events (start/stop, commands, Claude launch, errors) — to the log file.
logger = logging.getLogger("claude_tg_bot")


async def _sms_first_send_phone_number_code(self, phone_number, *args, **kwargs):
    """Send a login code as SMS, not to a Telegram app on another device.

    By default Kurigram's send_phone_number_code uses an empty CodeSettings, so
    Telegram routes a login code to a Telegram app wherever the phone number
    already has an active session (SentCodeTypeApp) — a code the user often never
    sees. Forcing current_number=False makes Telegram send it as an SMS instead.
    """
    kwargs.setdefault("current_number", False)
    return await Client.send_phone_number_code(self, phone_number, *args, **kwargs)


def _build_client(use_proxy: bool = True) -> Client:
    """Build a Pyrogram Client (Kurigram, dev branch) with native Telegram-proxy support.

    Kurigram (dev) supports MTProxy and Telegram Web proxy out of the box: we pass
    the configured proxy as a string "tg://proxy?server=...&port=...&secret=..."
    (MTProxy) or "tg://webproxy?server=...&secret=..." (Web proxy). Kurigram picks
    the proxy type and transport from the scheme, does the FakeTLS handshake and,
    for MTProxy, recognizes the secret (ee → FakeTLS with an SNI-domain, dd/plain →
    random padding). No external proxy-bridge is needed.

    use_proxy=False (the --no-proxy flag) forces a direct connection, ignoring the
    configured proxy — a diagnostic/fallback switch.

    If PROXY is unset (empty string) — we pass None: the bot connects to Telegram
    directly, without a proxy (the proxy is no longer mandatory). An empty string
    is NOT a valid proxy and would make kurigram raise "not a recognized proxy
    URL", so it must be mapped to None here.

    Returns the configured Client.
    """
    client = Client(
        name=config.SESSION_NAME,
        # Keep the .session in ~/.claude-tg-bot (not cwd), so it survives a
        # brew upgrade / a different launch directory.
        workdir=str(config.DATA_DIR),
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        phone_number=config.PHONE,
        # The 2FA cloud password is not in config.env — the user enters it at login.
        hide_password=True,
        # An empty PROXY means "no proxy": map it to None so kurigram connects
        # directly instead of rejecting the empty string as a malformed proxy.
        proxy=(config.PROXY or None) if use_proxy else None,
        # workers=1 — messages are processed SEQUENTIALLY. Otherwise Pyrogram
        # spawns several Claude processes in parallel; on a network failure that
        # multiplies hung processes and yields a mess of answers instead of a clear
        # timeout.
        workers=1,
    )
    # By default Telegram sends a login code to a Telegram app when the phone
    # number has an active session elsewhere, which the user may never see. Force
    # current_number=False so the code comes as an SMS instead.
    client.send_phone_number_code = MethodType(
        _sms_first_send_phone_number_code, client
    )
    return client


async def _with_timeout(coro, timeout):
    """Run a coroutine but give up after `timeout` seconds.

    asyncio.wait_for would hang here: Pyrogram's connect()/start() to an
    unreachable Telegram DC does not respond to task cancellation, so wait_for
    waits forever for the cancel to finish. asyncio.wait returns on timeout and
    we abandon (cancel, do not await) the stuck coroutine, so the caller gets a
    TimeoutError instead of a freeze.
    """
    task = asyncio.ensure_future(coro)
    done, pending = await asyncio.wait({task}, timeout=timeout)
    if pending:
        task.cancel()  # abandon; do not wait for the cancel to take effect
        raise asyncio.TimeoutError
    return task.result()  # re-raises any exception from the coroutine


async def _ensure_authorized(app):
    """Make sure the Telegram session is authorized once the live console is up.

    Called AFTER ui_init(): the bot is already showing its status, so a network
    stall does not look like a silent hang. A dead session (AUTH_KEY_UNREGISTERED
    — Telegram rejected the stored key) is not a network problem: retrying won't
    help. Here we detect it, clear the stale record and let Pyrogram re-authorize
    interactively. Because the login code prompt must be readable from stdin, we
    temporarily LEAVE the live console (ui_close), ask for the code in the normal
    terminal, then re-enter (ui_init) and carry on.

    On a network failure we do NOT block here — _start_with_retry handles it in
    the live console. We only act when the session itself is invalid.
    """
    auth_needed = False
    has_user_id = False
    try:
        with ui_redirect_stdout():
            await _with_timeout(app.connect(), config.CONNECT_TIMEOUT)
        has_user_id = bool(await app.storage.user_id())
        if has_user_id:
            # A stored user id means Pyrogram thinks it's authorized; a real
            # auth-probe (GetState) reveals whether Telegram actually accepts the
            # key. Raises AuthKeyUnregistered if the session was revoked.
            with ui_redirect_stdout():
                await app.invoke(raw.functions.updates.GetState())
    except AuthKeyUnregistered:
        # The stored key is no longer registered on Telegram. Drop it here while
        # the storage is still open — `disconnect()` below closes the sqlite DB,
        # and writing to it after that raises sqlite3.ProgrammingError, which
        # would crash the bot instead of re-authorizing.
        await app.storage.user_id(None)
        await app.storage.auth_key(None)
        await app.storage.is_bot(None)
        await app.storage.save()
        auth_needed = True
    except (asyncio.TimeoutError, ConnectionError, OSError) as e:
        # Network unreachable (e.g. a hung proxy, or Telegram's own DC down).
        # We can't even reach Telegram, so there is no session to authorize — and
        # retrying won't help. Report the cause and let main() stop, not hang.
        if app.is_connected and app.session is not None:
            await app.disconnect()
        raise

    # Close the probe so a stale session can be discarded cleanly. We do NOT call
    # `authorize()` here: after `disconnect()` the client is connected but not
    # "started", so `authorize()`'s internal `invoke()` would fail with
    # `ConnectionError: Client has not been started yet` (which main() used to
    # misreport as a network failure). Authorization is left to `start()` in
    # `_start_with_retry`, which brings the client up correctly from a clean
    # session.
    if app.is_connected and app.session is not None:
        await app.disconnect()

    if auth_needed or not has_user_id:
        # The stored session is revoked (auth_needed) or never fully authorized
        # (no user_id — e.g. a key without an identity, which a partial/old
        # session can leave behind). Delete the whole `.session` file, not just
        # the key inside it, so the next start() begins from a genuinely empty
        # session and re-authorizes cleanly instead of tripping over a
        # half-initialized storage.
        session_file = config.DATA_DIR / (config.SESSION_NAME + ".session")
        try:
            session_file.unlink(missing_ok=True)
            logger.info("Removed stale Telegram session: %s", session_file)
        except OSError as e:
            logger.warning("Could not remove stale session %s: %s", session_file, e)

    return auth_needed or not has_user_id


async def _start_with_retry(app, *, needs_auth: bool = False):
    """Connect to Telegram and stop on a network failure.

    _ensure_authorized has already confirmed the network is reachable and the
    session is valid, so app.start() should succeed. If it stalls or fails, the
    Telegram server is gone — retrying won't help, so we report the cause and
    stop (the caller exits), not retry for many minutes.

    Pyrogram's FakeTLS/proxy handshake can stall silently (no exception) when
    the proxy hangs mid-handshake, so app.start() never returns. We cap it with
    CONNECT_TIMEOUT and, on timeout, raise so the caller shows a clear error.

    When `needs_auth` is True the session was just discarded and `start()` runs
    `authorize()`, which waits for the user to type a login code from stdin. That
    must NOT be cut off by CONNECT_TIMEOUT, so the timeout is skipped in that
    case — `_ensure_authorized` already proved the network is reachable. The full
    -screen live console is briefly closed (ui_close/ui_init) around it so the
    code prompt and Ctrl+C reach the real terminal, not the live region.

    Ctrl+C propagates: KeyboardInterrupt/CancelledError are BaseException and we
    don't catch them, so the app closes as usual.
    """
    try:
        if needs_auth:
            # Authorization prompts for the login code interactively; no timeout
            # on it. Leave the rich live console first (ui_close): the code prompt
            # must be read from the real stdin and Ctrl+C must reach it, which the
            # full-screen live region would otherwise swallow. Re-enter after.
            ui_close()
            try:
                await app.start()
            finally:
                ui_init()
        else:
            await _with_timeout(app.start(), config.CONNECT_TIMEOUT)
        logger.info("Connected to Telegram")
    except KeyboardInterrupt:
        raise
    except (AuthKeyUnregistered, SessionPasswordNeeded) as e:
        # Login/session is no longer valid (401). Retrying won't fix it; the
        # main() handler shows the re-authenticate message. Exiting cleanly.
        logger.error("Telegram authorization failed: %s", _friendly(str(e)))
        raise
    except (asyncio.TimeoutError, ConnectionError, TimeoutError, OSError) as e:
        # Telegram unreachable (hung proxy, or direct DC down). We already know
        # the network was up a moment ago, so it just died — retrying won't help.
        logger.error("Could not connect to Telegram: %s", _friendly(str(e)))
        note = i18n.t("client.connect_fail", limit=1, friendly=_friendly(str(e)))
        ui_log(note)
        _report_run_error(note)
        raise


async def _shutdown(app):
    """Cancel background tasks and stop Pyrogram so the bot exits cleanly."""
    # 1. Cancel all background tasks (claude calls) without waiting for them
    #    forever — they kill their processes and clean up files in finally.
    tasks = list(_active_tasks)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # 2. Stop Pyrogram (turns off its internal workers/retries).
    #    block=False — don't wait for workers hung on retries to finish,
    #    otherwise Ctrl+C "doesn't interrupt" the bot.
    try:
        await app.stop(block=False)
    except Exception:
        pass


async def main():
    # Fail fast if no locale file at all is available (neither the configured
    # language nor English): we can't produce readable strings, so stop instead
    # of silently emitting bare i18n keys.
    i18n.ensure_available()
    config.validate()
    # A previous run (a killed curses/rich process) can leave the terminal in
    # raw mode, so an interactive input() echoes "y^M" instead of a line. Reset
    # the terminal to canonical mode before we ever prompt the user.
    _restore_terminal()
    # Second-instance guard: if the bot is already running — exit without starting.
    if not acquire_single_instance():
        return

    # Logging (issue #12): enabled by the --log[=level] flag. By default (no flag)
    # we don't write; on an interactive launch without a log but with existing
    # logs — offer to delete the old ones. This runs BEFORE the live console so
    # the "delete old logs?" prompt (an interactive input) appears in the normal
    # terminal, not inside the alt-screen region the live console owns.
    log_enabled, log_level = parse_log_flag(sys.argv)
    log_path = None
    if log_enabled:
        log_path = setup_logging(log_level, log_filename())
        logger.info("Logging enabled (level=%s), file: %s",
                    logging.getLevelName(log_level), log_path)
    else:
        maybe_cleanup_old_logs()

    # Interactive console comes on FIRST, so a network/auth stall shows a status
    # line instead of a silent hang; the "no_proxy" hint and diagnostics are
    # visible from the start. Auth prompts later briefly leave this console.
    use_proxy = not parse_no_proxy(sys.argv)
    ui_init()
    if not use_proxy and config.PROXY:
        logger.warning("--no-proxy: ignoring configured proxy, connecting directly")
        ui_log(i18n.t("client.no_proxy"))

    app = None
    try:
        app = _build_client(use_proxy)
    except ValueError as e:
        # A malformed proxy string (bad secret length/format) makes Client()
        # raise before any connection attempt. Report it clearly, don't crash
        # with a traceback and an empty log.
        friendly = _friendly(str(e))
        logger.error("Invalid Telegram proxy: %s", friendly)
        msg = i18n.t("client.connect_proxy_bad", friendly=friendly)
        _report_run_error(msg)
        ui_close()
        sys.stdout.write("\n" + msg + "\n")
        sys.stdout.flush()
        return

    # Important: use filters.all, not filters.INCOMING.
    #  - In Saved Messages our own messages come as incoming, but IN A GROUP your
    #    messages (you = the bot account) come as outgoing. So filters.incoming
    #    doesn't catch them in groups → filters.all is needed.
    #  - To avoid the bot feeding back on its own answers, on_all_message ignores
    #    messages whose id is in the bot-sent register (its own replies/attachments),
    #    while still processing the owner's genuine replies.
    # workers=1 — we process messages SEQUENTIALLY. Otherwise Pyrogram runs
    # several Claude processes in parallel; on a network failure that multiplies
    # hung processes and yields a mess of answers instead of a clear timeout.
    app.on_message(filters.all & ~filters.service)(on_all_message)

    # Intercept Pyrogram logs (connection errors/retries) so the console shows a
    # single status line instead of a flood on stderr.
    _STATUS_FILTER.install()

    # run.sh already showed the config.env path (==> config.env). Here we print the
    # projects root and the sandbox directory in a row, so it's clear what's where.
    ui_log(i18n.t("client.root", path=config.PROJECTS_ROOT))
    ui_log(i18n.t("client.sandbox_dir", cmd=config.SANDBOX_COMMAND, path=config.SANDBOX_ROOT))
    mt_state = i18n.t("client.proxy_set") if config.PROXY else i18n.t("client.proxy_unset")
    ui_log(i18n.t("client.proxy", state=mt_state))
    ui_log(i18n.t("client.claude_cmd", cmd=f"{config.CLAUDE_COMMAND} {config.COMMAND_ARGS}".strip()))

    # Re-authorize a dead session in the live console (a dead key is not fixable
    # by retrying). This briefly leaves the live console to ask for the login
    # code, then re-enters and keeps running. Ctrl+C here (while connecting or
    # waiting for the code) exits cleanly, not with a traceback.
    try:
        needs_auth = await _ensure_authorized(app)
    except KeyboardInterrupt:
        ui_close()
        return
    except (AuthKeyUnregistered, SessionPasswordNeeded, EOFError) as e:
        # Re-authorization did not succeed (e.g. the user aborted or the code was
        # wrong), or the login code could not be read (EOFError — stdin closed,
        # e.g. a non-interactive launch). Show the reason and exit, not silently.
        msg = i18n.t("client.connect_auth", friendly=_friendly(str(e)))
        logger.error("Telegram authorization failed: %s", _friendly(str(e)))
        ui_close()
        sys.stdout.write("\n" + msg + "\n")
        sys.stdout.flush()
        return
    except (TimeoutError, OSError, ConnectionError):
        # Telegram's server is unreachable (hung proxy, or the DC down with
        # --no-proxy). We couldn't even connect, so there is no session to
        # authorize; retrying won't help. Show a clear message and stop.
        msg = i18n.t("client.connect_no_server", secs=round(config.CONNECT_TIMEOUT))
        logger.error("Telegram server unreachable after %ss", round(config.CONNECT_TIMEOUT))
        _report_run_error(msg)
        ui_close()
        sys.stdout.write("\n" + msg + "\n")
        sys.stdout.flush()
        return

    # start() — connect to Telegram (incl. login). On a network failure we don't
    # crash with a traceback, but print a short message and retry with a growing
    # pause. Ctrl+C interrupts the pause.
    # The status loop runs DURING the connection attempts (not only after a
    # successful connect), so the "Working"/"❌ Error" block and any diagnostic
    # are visible on screen while the bot tries to reach Telegram — not blank.
    stop_status = asyncio.Event()
    status_task = asyncio.create_task(_status_loop(stop_status))
    with ui_redirect_stdout():
        try:
            await _start_with_retry(app, needs_auth=needs_auth)
        except (TimeoutError, OSError, ConnectionError):
            # All retry attempts were exhausted (e.g. a dead proxy, or direct
            # connection unavailable with --no-proxy). The reason was already
            # printed by _start_with_retry; exit cleanly, not with a traceback.
            stop_status.set()
            status_task.cancel()
            ui_close()
            sys.stdout.write("\n")
            sys.stdout.flush()
            return
        except Exception as e:
            # Any other Telegram-side failure (e.g. a dead session). The right
            # guide depends on the cause: auth -> re-authenticate, else a generic
            # unexpected-stop message. Either way the reason is shown, not silent.
            if isinstance(e, (AuthKeyUnregistered, SessionPasswordNeeded)):
                msg = i18n.t("client.connect_auth", friendly=_friendly(str(e)))
            else:
                msg = i18n.t("client.connect_unexpected", friendly=_friendly(str(e)))
            logger.error("Bot stopped: unexpected Telegram error: %s", _friendly(str(e)))
            _report_run_error(msg)
            ui_log(msg)
            stop_status.set()
            status_task.cancel()
            ui_close()
            # The live-console frame is torn down, so the buffered ui_log lines may
            # not have been painted. Write the reason directly to the real terminal
            # (not sys.stdout, still redirected into rich) so it is never lost.
            sys.__stdout__.write("\n" + msg + "\n")
            sys.__stdout__.flush()
            return

    logger.info("Bot running and working (in %s)", config.SANDBOX_ROOT)
    ui_log(i18n.t("client.running"))
    ui_log(i18n.t("client.stop_hint"))

    # idle() — keep the process alive until SIGINT/SIGTERM.
    try:
        await pyrogram.idle()
    finally:
        # Stop the status loop and exit cleanly.
        stop_status.set()
        status_task.cancel()
        # Newline after the last status block — otherwise the next output
        # (traceback, shell prompt) would stick to the last block line.
        if ui_active():
            # curses was on: restore the terminal, then leave a blank line so the
            # shell prompt doesn't stick to the last screen row.
            ui_close()
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            sys.stdout.write("\n" * (_STATUS_PREV_LINES + 1))
            sys.stdout.flush()
        # Clean shutdown: cancel the background tasks (they may be stuck on
        # claude) and silence Pyrogram. Otherwise asyncio.run can't finish the
        # event loop while those tasks are alive, and Ctrl+C "doesn't work".
        logger.info("Bot stopped")
        await _shutdown(app)