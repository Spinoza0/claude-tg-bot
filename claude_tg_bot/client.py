"""Telegram client creation, the main() entry point, and retrying connections."""

import asyncio
import logging
import sys

import pyrogram
from pyrogram import Client, filters

from . import config, i18n
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

# Bot events (start/stop, commands, Claude launch, errors) — to the log file.
logger = logging.getLogger("claude_tg_bot")


def _build_client() -> Client:
    """Build a Pyrogram Client (Kurigram, dev branch) with native MTProto-proxy support.

    Kurigram (dev) supports MTProto-proxy out of the box: we pass the proxy as a
    string "tg://proxy?server=...&port=...&secret=..." — it recognizes the secret
    (ee → FakeTLS with an SNI-domain, dd/plain → random padding), picks the right
    TCPIntermediatePadded transport and does the FakeTLS handshake. No external
    mtproxy-bridge is needed.

    If MT_PROXY is unset — we pass None: the bot connects to Telegram directly,
    without a proxy (MT_PROXY is no longer mandatory).

    Returns the configured Client.
    """
    return Client(
        name=config.SESSION_NAME,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        phone_number=config.PHONE,
        password=config.CLOUD_PASSWORD,
        # hide_password=True — hide the 2FA password prompt (getpass without echo)
        # so it doesn't show in the terminal. The confirmation code remains visible.
        hide_password=True,
        proxy=config.MT_PROXY or None,
        # workers=1 — messages are processed SEQUENTIALLY. Otherwise Pyrogram
        # spawns several Claude processes in parallel; on a network failure that
        # multiplies hung processes and yields a mess of answers instead of a clear
        # timeout.
        workers=1,
    )


async def _start_with_retry(app):
    """Connect to Telegram retrying on network failure.

    Instead of crashing with a traceback (TimeoutError after its internal
    retries) we print a SHORT clear message and repeat the connection with the
    shared backoff scheme: 1, 2, ... min (cap RETRY_MAX_DELAY). After RETRY_LIMIT
    attempts — report the error and exit (we don't loop forever).

    Ctrl+C interrupts the pause: KeyboardInterrupt/CancelledError are
    BaseException and we don't catch them, so the app closes as usual.
    """
    for attempt in range(1, config.RETRY_LIMIT + 1):
        try:
            await app.start()
            logger.info("Connected to Telegram")
            return  # connected — exit the loop
        except KeyboardInterrupt:
            raise
        except (ConnectionError, TimeoutError, OSError) as e:
            if attempt < config.RETRY_LIMIT:
                pause = _retry_backoff_delay(attempt)
                mins = max(1, round(pause / 60))
                # One short line: no spam of repeated errors.
                note = i18n.t("client.connect_retry", friendly=_friendly(str(e)),
                              mins=mins, attempt=attempt, limit=config.RETRY_LIMIT)
                logger.warning("Telegram connection: attempt %s/%s failed: %s",
                               attempt, config.RETRY_LIMIT, _friendly(str(e)))
                if _use_color():
                    line = f"\033[31m❌ {note}\033[0m"
                else:
                    line = f"❌ {note}"
                sys.stdout.write("\r" + " " * 60 + "\r" + line + "\n")
                sys.stdout.flush()
                await asyncio.sleep(pause)
            else:
                logger.error("Could not connect to Telegram after %s attempts: %s",
                             config.RETRY_LIMIT, _friendly(str(e)))
                sys.stdout.write(
                    i18n.t("client.connect_fail", limit=config.RETRY_LIMIT, friendly=_friendly(str(e)))
                )
                sys.stdout.flush()
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
    # Second-instance guard: if the bot is already running — exit without starting.
    if not acquire_single_instance():
        return

    # Logging (issue #12): enabled by the --log[=level] flag. By default (no flag)
    # we don't write; on an interactive launch without a log but with existing
    # logs — offer to delete the old ones.
    log_enabled, log_level = parse_log_flag(sys.argv)
    log_path = None
    if log_enabled:
        log_path = setup_logging(log_level, log_filename())
        logger.info("Logging enabled (level=%s), file: %s",
                    logging.getLevelName(log_level), log_path)
    else:
        maybe_cleanup_old_logs()

    app = _build_client()

    # Important: use filters.all, not filters.INCOMING.
    #  - In Saved Messages our own messages come as incoming, but IN A GROUP your
    #    messages (you = the bot account) come as outgoing. So filters.incoming
    #    doesn't catch them in groups → filters.all is needed.
    #  - To avoid the bot feeding back on its own answers, on_all_message ignores
    #    outgoing reply messages (reply_to_message_id).
    # workers=1 — we process messages SEQUENTIALLY. Otherwise Pyrogram runs
    # several Claude processes in parallel; on a network failure that multiplies
    # hung processes and yields a mess of answers instead of a clear timeout.
    app.on_message(filters.all & ~filters.service)(on_all_message)

    # Intercept Pyrogram logs (connection errors/retries) so the console shows a
    # single status line instead of a flood on stderr.
    _STATUS_FILTER.install()

    print(i18n.t("client.startup", name=config.SESSION_NAME))
    # run.sh already showed the config.env path (==> config.env). Here we print the
    # projects root and the sandbox directory in a row, so it's clear what's where.
    print(i18n.t("client.root", path=config.PROJECTS_ROOT))
    print(i18n.t("client.sandbox_dir", cmd=config.SANDBOX_COMMAND, path=config.SANDBOX_ROOT))
    mt_state = i18n.t("client.mtproxy_set") if config.MT_PROXY else i18n.t("client.mtproxy_unset")
    print(i18n.t("client.mtproxy", state=mt_state))
    print(i18n.t("client.claude_cmd", cmd=f"{config.CLAUDE_COMMAND} {config.COMMAND_ARGS}".strip()))

    # start() — connect to Telegram (incl. login). On a network failure we don't
    # crash with a traceback, but print a short message and retry with a growing
    # pause. Ctrl+C interrupts the pause.
    await _start_with_retry(app)

    logger.info("Bot running and working (in %s)", config.SANDBOX_ROOT)
    print(i18n.t("client.running"))
    print(i18n.t("client.stop_hint"))

    # Background task: keeps the console status block "Working" / "❌ Error: …".
    # Stops together with the bot.
    stop_status = asyncio.Event()
    status_task = asyncio.create_task(_status_loop(stop_status))

    # idle() — keep the process alive until SIGINT/SIGTERM.
    try:
        await pyrogram.idle()
    finally:
        # Stop the status loop and exit cleanly.
        stop_status.set()
        status_task.cancel()
        # Newline after the last status block — otherwise the next output
        # (traceback, shell prompt) would stick to the last block line.
        sys.stdout.write("\n" * (_STATUS_PREV_LINES + 1))
        sys.stdout.flush()
        # Clean shutdown: cancel the background tasks (they may be stuck on
        # claude) and silence Pyrogram. Otherwise asyncio.run can't finish the
        # event loop while those tasks are alive, and Ctrl+C "doesn't work".
        logger.info("Bot stopped")
        await _shutdown(app)