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
import re
import shutil
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
        # Original root handlers stripped in install() and re-attached for
        # non-Pyrogram records (we don't want to silence the rest of the app).
        self._passthrough: list = []

    def install(self) -> None:
        # Intercept at the ROOT logger, not per-logger: Pyrogram creates its
        # child loggers (pyrogram.connection, ...) lazily, so a per-name pass
        # over loggerDict finds none at startup and the raw stderr output leaks.
        # Routing through the root guarantees every "pyrogram.*" record is seen.
        root = logging.getLogger()
        self._passthrough = list(root.handlers)
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(self)
        root.setLevel(logging.NOTSET)

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name
        if name == "claude_tg_bot":
            # Our own diagnostic log lines ("Claude launch finished with code
            # ...", "Model unavailable ...") belong in the log FILE (--log), not
            # on the console — otherwise they wrap and shred the status block.
            # Their handler/file was already consulted along the chain; dropping
            # here keeps the console clean. (They still reach claude_tg_bot's own
            # FileHandler when logging is enabled.)
            return
        if name.startswith("pyrogram"):
            # Pyrogram connection errors/retries — keep them off the console,
            # remember the last one for the status block.
            if record.levelno < logging.WARNING:
                return
            try:
                msg = record.getMessage()
            except Exception:
                return
            with self._lock:
                self._last_error = msg
                self._last_error_ts = time.time()
            return
        # Everything else — let it reach the original root handlers (or the
        # lastResort fallback) so the rest of the app still logs normally.
        emitted = False
        for h in self._passthrough:
            try:
                if h.level <= record.levelno:
                    h.handle(record)
                    emitted = True
            except Exception:
                pass
        if not emitted and logging.lastResort is not None:
            try:
                logging.lastResort.handle(record)
            except Exception:
                pass
        try:
            msg = record.getMessage()
        except Exception:
            return
        with self._lock:
            self._last_error = msg
            self._last_error_ts = time.time()
        # Mirror to the log file (if logging is enabled) — these are Pyrogram
        # errors (connection/retries), important for diagnostics. Only write it
        # if claude_tg_bot actually has a handler; otherwise the logging module
        # would emit a "No handlers could be found" warning to stderr.
        bot_log = logging.getLogger("claude_tg_bot")
        if bot_log.handlers:
            bot_log.log(record.levelno, msg)

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


def ui_active() -> bool:
    return _CURSES_STDSCR is not None


def ui_init() -> bool:
    """Enable the curses two-region console if stdout is a TTY.

    Returns True when curses is active. Otherwise the bot keeps plain output
    (piped / --log in background) and the status is a no-op. Must be called from
    the main thread before any console writing.
    """
    global _CURSES_STDSCR
    if not sys.stdout.isatty() or _CURSES_STDSCR is not None:
        return _CURSES_STDSCR is not None
    import curses
    try:
        std = curses.initscr()
        curses.noecho()
        curses.cbreak()
        std.keypad(True)
        std.nodelay(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
    except Exception:
        # TTY exists but curses can't take over (rare) — fall back to plain.
        try:
            curses.endwin()
        except Exception:
            pass
        return False
    _CURSES_STDSCR = std
    return True


def ui_close() -> None:
    """Restore the terminal (call on shutdown)."""
    global _CURSES_STDSCR
    if _CURSES_STDSCR is None:
        return
    import curses
    try:
        curses.nocbreak()
        _CURSES_STDSCR.keypad(False)
        curses.echo()
        curses.endwin()
    except Exception:
        pass
    _CURSES_STDSCR = None


def ui_log(line: str) -> None:
    """Write an ordinary (non-status) line to the console.

    In curses mode it appends to the scrollable log area above the status bar
    and repaints the bottom row so it is never erased. Outside curses it falls
    back to a plain stderr write (no in-place redraw, so it can't be wiped).
    """
    global _CURSES_LOG
    line = line.rstrip("\n")
    if _CURSES_STDSCR is None:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        return
    _CURSES_LOG.append(line)
    _ui_paint()


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


def _strip_color(s: str) -> str:
    return _ANSI_RE.sub("", s)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _display_width(s: str) -> int:
    """Visible width in terminal columns (emojis/wide glyphs = 2, else 1).

    ANSI color codes (\\x1b[...m) take no column — strip them before counting.
    """
    s = _ANSI_RE.sub("", s)
    w = 0
    for ch in s:
        cp = ord(ch)
        if cp >= 0x1F000 or 0x2500 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF:
            w += 2
        else:
            w += 1
    return w


def _fit_width(text: str) -> str:
    """Trim a line to the terminal width so it never wraps.

    A wrapped line would occupy a whole extra row, but _draw_status tracks the
    block height by len(lines) — so a wrap breaks the redraw (leftover text,
    "Working" duplicates). We trim by VISIBLE column width (emojis are 2 cols)
    so a line can't exceed the terminal and wrap. Fall back to 80 if not a tty.
    """
    width = shutil.get_terminal_size().columns or 80
    if _display_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        cw = 2 if _display_width(ch) == 2 else 1
        if used + cw > width - 1:  # reserve 1 col for the ellipsis
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def _wrap(text: str) -> list[str]:
    """Split a long error text into terminal-width lines (no ellipsis, no cut).

    Unlike _fit_width (which truncates the message), we wrap: the message keeps
    every word and line, just continues on the next row. Each returned line is at
    most the terminal column width, so _draw_status' height tracking (len(lines))
    stays correct and nothing gets clipped. ANSI codes are absent from the text
    here (color is applied outside), so we count pure visible width.
    """
    width = shutil.get_terminal_size().columns or 80
    lines: list[str] = []
    for raw in text.split("\n"):
        if not raw:
            lines.append("")
            continue
        cur = ""
        cur_w = 0
        for word in raw.split(" "):
            if not word:
                continue
            w = _display_width(word)
            new_w = cur_w + (1 if cur else 0) + w
            if new_w <= width:
                if cur:
                    cur += " "
                    cur_w += 1
                cur += word
                cur_w += w
            else:
                if cur:
                    lines.append(cur)
                cur = word
                cur_w = w
        lines.append(cur)
    return lines


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
    # Unknown — show the whole message as plain text without quotes, no cut-off.
    return record.strip().strip('"')


# Number of lines the last status block took (for redrawing).
_STATUS_PREV_LINES = 0

# curses stdscr when running in an interactive terminal; None when stdout is not
# a tty (piped, --log in background) — then we keep plain output and no in-place
# redraw, so nothing is ever erased.
_CURSES_STDSCR = None
_CURSES_LOG = list()  # scrollable log lines shown above the status bar.


def _stamp() -> str:
    """Timestamp for a status line (e.g. 2026-09-12 14:32:05)."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _ui_paint() -> None:
    """Repaint the curses screen: scrollable log above, status row on the bottom.

    Only the two regions owned by curses are touched — the status row and the
    log area. Ordinary output sits in the log region, so a status repaint never
    erases it (the whole point of the curses approach).
    """
    global _CURSES_LOG
    std = _CURSES_STDSCR
    if std is None:
        return
    h, w = std.getmaxyx()
    # Log region: rows 0..h-2, keep the newest `h-1` lines.
    _CURSES_LOG = _CURSES_LOG[-(h - 1):]
    for y, line in enumerate(_CURSES_LOG):
        std.move(y, 0)
        std.clrtoeol()
        std.addnstr(y, 0, line[: w - 1], w - 1)
    # Status row: bottom row, latest state, redrawn on every paint.
    std.move(h - 1, 0)
    std.clrtoeol()
    std.addnstr(h - 1, 0, _status_display[: w - 1], w - 1)
    std.refresh()


# The current single-line status text shown on the bottom row (curses mode).
_status_display = ""


def _draw_status(lines: list[str]) -> None:
    """Redraw the status in place without erasing ordinary output.

    In curses mode the status is a pinned bottom row and the error/log lines go
    to the scrollable region above, so nothing is ever wiped. Outside curses
    (piped / --log in background) it keeps the old in-place ANSI block; there is
    no interleaved output in that case, so the move-up/clear rewrite is safe.
    """
    global _STATUS_PREV_LINES, _status_display
    if _CURSES_STDSCR is not None:
        # First row = the working/error state (one line); the rest (wrapped error
        # rows) are pushed into the log area so they don't collide with the bar.
        _status_display = _strip_color(lines[0]) if lines else ""
        if len(lines) > 1:
            for row in lines[1:]:
                ui_log(row)
        _ui_paint()
        _STATUS_PREV_LINES = 1
        return
    out = sys.stdout
    n = len(lines)
    if _STATUS_PREV_LINES:
        out.write(f"\033[{_STATUS_PREV_LINES}F")
    for i, line in enumerate(lines):
        out.write("\r\033[2K" + _fit_width(line))
        if i < n - 1:
            out.write("\n")
    # If the block shrank (error cleared), clear the leftover rows below it and
    # move the cursor back up to the last block row.
    extra = max(0, _STATUS_PREV_LINES - n)
    for _ in range(extra):
        out.write("\n\r\033[2K")
    if extra:
        out.write(f"\033[{extra}F")
    out.flush()
    _STATUS_PREV_LINES = n


def _status_lines(err_display: str, err_ts: float, err_active: bool, work_ts: float) -> list[str]:
    """Assemble the status block rows — only the CURRENT state, one icon at a time.

    Only the active state is shown, with its icon: if there's a problem now
    (err_active=True) — the ❌ error (and "Working" without 🟢); if all is well —
    🟢 "Working" only, and the diagnostic error is hidden. So 🟢 and ❌ never
    overlap on screen: no stale error history next to a healthy status.
    work_ts — the time the "working" state was established (NOT ticking every
    loop, otherwise the line would change and the block would keep redrawing).
    """
    color = _use_color()
    def fmt(t: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
    work_ts_s = fmt(work_ts)
    if err_active:
        work = (
            f"\033[32m{i18n.t('status.working')}\033[0m [{work_ts_s}]"
            if color
            else f"{i18n.t('status.working')} [{work_ts_s}]"
        )
        return [work] + _error_lines(err_display, fmt(err_ts), color)
    if color:
        work = f"\033[32m{i18n.t('status.working_ok')}\033[0m [{work_ts_s}]"
    else:
        work = f"{i18n.t('status.working_ok')} [{work_ts_s}]"
    return [work]


def _error_lines(err_display: str, ts: str, color: bool) -> list[str]:
    """Build the error rows: the timestamp right after "Error:", then the message.

    The whole message is wrapped to the terminal width (never truncated), so a
    long Pyrogram error continues on the next rows instead of being cut. The
    first row carries the red "Error: [ts]" prefix; the continuation rows are
    plain (no re-applied prefix).
    """
    prefix = i18n.t("status.error", err="")
    head = f"{prefix}[{ts}]" if ts else prefix
    head = f"{head} " if head and not head.endswith(" ") else head
    wrapped = _wrap(err_display) or [""]
    first = head + wrapped[0]
    if color:
        return [f"\033[31m{first}\033[0m"] + wrapped[1:]
    return [first] + wrapped[1:]


async def _status_loop(stop: asyncio.Event) -> None:
    """Background: every 1.5s keeps a block — the current state only.

    The ❌/🟢 icon reflects exactly the current state, never both: ❌ on an active
    error (the "Working" row loses its 🟢), 🟢 on a healthy one (the stale error
    is hidden, so its diagnostic text and ❌ disappear once the bot recovers).
    The block is redrawn only when the content changes.
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