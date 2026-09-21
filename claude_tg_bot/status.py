"""A single console status instead of Pyrogram error spam.

Pyrogram/Kurigram logs its connection errors (gaierror, INTERDC... and retries)
via standard logging under the "pyrogram.*" loggers and prints them as a stream
to stderr. That clutters the terminal and scares the user. Instead we intercept
Pyrogram logs with our own Handler: keep the LAST error, mute the streamed
output, and print a status block to the console — "🟢 Working" or
"❌ Error: <last message>" — only when the state changes.
"""

import asyncio
import logging
import sys
import threading
import time
from typing import Optional

from . import i18n

# Seconds without a new error before we consider things working again.
_STATUS_OK_AFTER = 4.0


class _StatusFilter(logging.Handler):
    """Intercept Pyrogram logs: don't print them, but remember the last error.

    emit() is called from a Pyrogram thread, so we only write to plain fields
    under a lock (no asyncio/print). The status output is a separate background
    task in the main event loop (_status_loop).
    """

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._last_error: Optional[str] = None
        self._last_error_ts: float = 0.0
        # Disable handler inheritance from parent loggers so Pyrogram does not
        # duplicate messages to stderr bypassing the intercept.
        self._owned: list[str] = []

    def install(self) -> None:
        for name in ("pyrogram",):
            logger = logging.getLogger(name)
            # Remove Pyrogram's default output (StderrHandler etc.) so messages
            # are not printed directly — only via the intercept.
            for h in list(logger.handlers):
                logger.removeHandler(h)
            logger.addHandler(self)
            logger.setLevel(logging.WARNING)
            logger.propagate = False
            self._owned.append(name)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        try:
            msg = record.getMessage()
        except Exception:
            return
        with self._lock:
            self._last_error = msg
            self._last_error_ts = time.time()
        # Mirror to the log file (if logging is enabled) — these are Pyrogram
        # errors (connection/retries), important for diagnostics.
        logging.getLogger("claude_tg_bot").log(record.levelno, msg)

    def snapshot(self) -> tuple[Optional[str], float]:
        with self._lock:
            return self._last_error, self._last_error_ts


_STATUS_FILTER = _StatusFilter()

# A Claude launch error (model/wrapper went down): stored separately from the
# pyrogram logs (those are about the Telegram connection). Written from
# _run_and_reply on a nonzero exit_code or error-like text, read by _status_loop.
# This way the console shows ❌ on a model drop too, not only on a network break.
_RUN_ERR_LOCK = threading.Lock()
_RUN_ERR_TEXT: str = ""
_RUN_ERR_TS: float = 0.0


def _report_run_error(text: str) -> None:
    global _RUN_ERR_TEXT, _RUN_ERR_TS
    with _RUN_ERR_LOCK:
        _RUN_ERR_TEXT = text
        _RUN_ERR_TS = time.time()


def _snapshot_run_error() -> tuple[str, float]:
    with _RUN_ERR_LOCK:
        return _RUN_ERR_TEXT, _RUN_ERR_TS


# A model/wrapper error inside the response text (if exit_code somehow stays 0
# but the user receives an error message in chat instead of an answer).
_RUN_ERR_MARKERS = ("API Error", "Cannot connect", "502", "503")


def _is_run_error(result) -> bool:
    return result.exit_code != 0 or result.text.lstrip().startswith(_RUN_ERR_MARKERS)


# Markers that indicate the MODEL/provider itself is unavailable (not a launch
# crash, a too-long prompt, etc.). Based on these the bot decides to switch the
# model to COMMAND_ARGS_ALTERNATIVE (issue #5). These are wrapper answers like
# "API Error: 502 Cannot connect ..." or "model not found"/"rate limit".
_MODEL_UNAVAILABLE_MARKERS = (
    "API Error", "Cannot connect", "502", "503",
    "model not found", "model unavailable", "rate limit", "upstream",
)


def _is_model_unavailable(result) -> bool:
    """True if the answer indicates the model is unavailable (switch it)."""
    text = result.text.lstrip()
    return any(m in text for m in _MODEL_UNAVAILABLE_MARKERS)


# ANSI colors for the status in the terminal (green "working", red "error").
# Disabled when stdout is not a TTY (e.g. redirected to a file) — then we keep
# only the emoji, without escape codes.
def _use_color() -> bool:
    return sys.stdout.isatty()


def _friendly(record: str) -> str:
    """Turn a raw Pyrogram message into a user-friendly error.

    Pyrogram writes like: 'Retrying "updates.GetState" due to: Request timed out'
    or 'Connection failed: gaierror [...]'. We show the essence, not the internals.
    """
    r = record.lower()
    if "timed out" in r or "timeout" in r:
        return i18n.t("status.err_timeout")
    if "gaierror" in r or "nodename" in r or "no host" in r:
        return i18n.t("status.err_no_network")
    if "connection" in r or "connect" in r:
        return i18n.t("status.err_no_connection")
    if "internal server" in r or "interdc" in r or "500" in r:
        return i18n.t("status.err_temp_telegram")
    # Unknown — trim to the first 120 chars as plain text without quotes.
    msg = record.strip().strip('"')
    return msg if len(msg) <= 120 else msg[:120] + "…"


# Number of lines the last status block took (for redrawing).
_STATUS_PREV_LINES = 0


def _stamp() -> str:
    """Timestamp for a status line (e.g. 2026-09-12 14:32:05)."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _draw_status(lines: list[str]) -> None:
    """Redraw the status block in place, without touching output above it.

    lines — the block rows: first the working state, second (if any) the last
    error. Move up to the start of the previous block (\\033[F) and erase from
    the cursor to the end of screen (\\033[J) — this removes the whole old block
    (so a 1↔2 height change leaves no tail) without touching the lines above.
    Then print the new block. \\033[2K before a line clears leftovers on wrap.
    """
    global _STATUS_PREV_LINES
    out = sys.stdout
    if _STATUS_PREV_LINES:
        out.write(f"\033[{_STATUS_PREV_LINES}F")
        out.write("\033[J")
    for i, line in enumerate(lines):
        if i:
            out.write("\n")
        out.write("\033[2K" + line)
    out.flush()
    _STATUS_PREV_LINES = len(lines)


def _status_lines(err_display: str, err_ts: float, err_active: bool, work_ts: float) -> list[str]:
    """Assemble the status block rows: working state + last error.

    The icon (🟢/❌) is set only on the CURRENT state:
      - if there's a problem now (err_active=True) — ❌ on the error, "Working"
        without 🟢;
      - if all is well — 🟢 on "Working", and the last error without ❌ (as
        history). Both rows are shown together; the error is always last.
    No errors at all — only "Working".
    work_ts — the time the "working" state was established (NOT ticking every
    loop, otherwise the line would change and the block would keep redrawing).
    """
    color = _use_color()
    def fmt(t: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
    work_ts_s = fmt(work_ts)
    if err_active:
        # Problem now: working without 🟢, the error with ❌.
        if color:
            work = f"\033[32m{i18n.t('status.working')}\033[0m [{work_ts_s}]"
            err = f"\033[31m{i18n.t('status.error', err=err_display)}\033[0m [{fmt(err_ts)}]"
        else:
            work = f"{i18n.t('status.working')} [{work_ts_s}]"
            err = f"{i18n.t('status.error', err=err_display)} [{fmt(err_ts)}]"
        return [work, err]
    # All well now: 🟢 on working; the last error — without ❌ (history).
    if color:
        work = f"\033[32m{i18n.t('status.working_ok')}\033[0m [{work_ts_s}]"
    else:
        work = f"{i18n.t('status.working_ok')} [{work_ts_s}]"
    lines = [work]
    if err_display:
        lines.append(f"{i18n.t('status.error_no_icon', err=err_display)} [{fmt(err_ts)}]")
    return lines


async def _status_loop(stop: asyncio.Event) -> None:
    """Background: every 1.5s keeps a block — "working" + last error.

    The ❌/🟢 icon is set only on the current state: ❌ on a fresh error, 🟢 on a
    healthy one. The last error (if any) is always shown, but without ❌ when all
    is well now, and carries its time. The error is not duplicated (the block is
    only printed when it changes).
    """
    prev = None
    prev_err = None
    ok_since = 0.0
    await asyncio.sleep(0.2)  # give pyrogram a moment to start logging
    while not stop.is_set():
        # Merge two error sources: the Telegram connection (pyrogram logs) and
        # a model/wrapper drop (Claude launch). Take the more recent one.
        pg_err, pg_ts = _STATUS_FILTER.snapshot()
        run_err, run_ts = _snapshot_run_error()
        if run_err and (not pg_err or run_ts >= pg_ts):
            last_err, ts = run_err, run_ts
        else:
            last_err, ts = pg_err, pg_ts
        err_active = bool(last_err) and (time.time() - ts) <= _STATUS_OK_AFTER
        # The "working" time is fixed when moving to ok (or at start), so it does not
        # tick every loop and the block does not redraw without changes.
        if not err_active and (prev_err or ok_since == 0.0):
            ok_since = time.time()
        prev_err = err_active
        # The last error is always shown (if any) — a pyrogram error is tidied via
        # _friendly (text about network/Telegram), a Claude launch error is shown
        # as-is (it is not about the Telegram connection).
        if last_err:
            if run_err and last_err == run_err:
                err_display = last_err
            else:
                err_display = _friendly(last_err)
            err_ts = ts
        else:
            err_display = ""
            err_ts = 0.0
        lines = _status_lines(err_display, err_ts, err_active, ok_since)
        # Print only when the content changes — no repeated spam.
        if lines != prev:
            _draw_status(lines)
            prev = lines
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            continue