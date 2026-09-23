"""Unit tests for the unified console status (Pyrogram log interception).

We check that the interceptor remembers the last error and does NOT stamp it to
stderr, and that the status loop shows "🟢 Working" when healthy and "❌ Error"
(with ❌) when an error is present — fresh or as history.
"""

import asyncio
import logging
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import status as st  # noqa: E402


def _record(msg: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("pyrogram.connection", level, "f", 1, msg, None, None)


class TestStatusFilter(unittest.TestCase):
    """Interceptor: stores the last error, ignores weak records."""

    def test_captures_error(self):
        sf = st._StatusFilter()
        sf.emit(_record("Connection failed: gaierror boom"))
        err, ts = sf.snapshot()
        self.assertIsNotNone(err)
        self.assertIn("gaierror", err)
        self.assertGreater(ts, 0)

    def test_ignores_info(self):
        sf = st._StatusFilter()
        sf.emit(_record("some info", level=logging.INFO))
        err, _ = sf.snapshot()
        self.assertIsNone(err)

    def test_new_error_overwrites(self):
        sf = st._StatusFilter()
        sf.emit(_record("first error"))
        sf.emit(_record("second error"))
        err, _ = sf.snapshot()
        self.assertIn("second error", err)

    def test_install_intercepts_pyrogram_loggers(self):
        # install() must route pyrogram.* records into the interceptor and keep
        # them off stderr, even for lazily-created child loggers. A non-pyrogram
        # record still reaches the app's normal output.
        import io
        import sys as _sys

        logging.getLogger("claude_tg_bot").addHandler(
            logging.FileHandler("/tmp/_status_test.log", mode="w")
        )
        root = logging.getLogger()
        root_handlers = list(root.handlers)
        root_level = root.level
        try:
            st._STATUS_FILTER.install()
            buf = io.StringIO()
            real = _sys.stderr
            _sys.stderr = buf
            logging.getLogger("pyrogram.connection.transport.tcp").warning(
                "Connection failed: gaierror [Errno 8] nodename"
            )
            _sys.stderr = real
            # raw message must NOT appear on stderr, but is captured
            self.assertNotIn("Connection failed", buf.getvalue())
            err, _ = st._STATUS_FILTER.snapshot()
            self.assertIn("Connection failed", err)
            # claude_tg_bot diagnostics (launch errors) must not leak to stderr
            buf2 = io.StringIO()
            _sys.stderr = buf2
            logging.getLogger("claude_tg_bot").error(
                "Claude launch finished with code 1: API Error: 502"
            )
            _sys.stderr = real
            self.assertNotIn("Claude launch", buf2.getvalue())
        finally:
            _sys.stderr = real
            logging.getLogger("claude_tg_bot").handlers.clear()
            # restore root logging so we don't disturb other tests
            for h in list(root.handlers):
                root.removeHandler(h)
            for h in root_handlers:
                root.addHandler(h)
            root.setLevel(root_level)
            st._STATUS_FILTER._last_error = None
            st._STATUS_FILTER._last_error_ts = 0.0


class TestFriendly(unittest.TestCase):
    """Converting raw Pyrogram messages into readable text."""

    def test_timeout(self):
        self.assertIn("no response", st._friendly('Retrying "updates.GetState" due to: Request timed out'))

    def test_gaierror(self):
        self.assertIn("resolve host", st._friendly("Connection failed: gaierror [Errno 8] nodename"))

    def test_connection(self):
        self.assertIn("No connection", st._friendly("Connection failed: Connection reset"))

    def test_interdc(self):
        self.assertIn("Telegram", st._friendly("An error occurred while Telegram was intercommunicating with DC4"))

    def test_unknown_short(self):
        self.assertEqual(st._friendly("something unclear"), "something unclear")


class TestStatusLoop(unittest.TestCase):
    """The status loop prints "✗ Error" / "● Working" on state change."""

    def setUp(self):
        st._STATUS_FILTER._last_error = None
        st._STATUS_FILTER._last_error_ts = 0.0
        st._report_run_error("")

    def test_shows_error_for_fresh(self):
        async def scenario():
            printed = []
            st._draw_status = lambda lines: printed.append(lines)
            st._STATUS_FILTER.emit(_record("Connection failed: gaierror boom"))
            stop = asyncio.Event()
            task = asyncio.create_task(st._status_loop(stop))
            await asyncio.sleep(0.35)
            task.cancel()
            return printed

        printed = asyncio.run(scenario())
        self.assertTrue(printed)
        last = printed[-1]
        self.assertEqual(len(last), 2)
        self.assertIn("Working", last[0])
        self.assertNotIn("🟢", last[0])          # problem — no 🟢 on Working
        self.assertIn("❌", last[1])             # error is fresh — with ❌
        self.assertIn("resolve host", last[1])

    def test_returns_ok_when_stale(self):
        async def scenario():
            printed = []
            st._draw_status = lambda lines: printed.append(lines)
            st._STATUS_FILTER.emit(_record("old error"))
            st._STATUS_FILTER._last_error_ts = 0  # "long ago" — stale
            stop = asyncio.Event()
            task = asyncio.create_task(st._status_loop(stop))
            await asyncio.sleep(0.35)
            task.cancel()
            return printed

        printed = asyncio.run(scenario())
        self.assertTrue(printed)
        last = printed[-1]
        self.assertEqual(len(last), 2)
        self.assertIn("🟢", last[0])             # ok — Working with 🟢
        self.assertIn("❌", last[1])             # error always with ❌
        self.assertIn("Error", last[1])


class TestRunError(unittest.TestCase):
    """A Claude launch error is reflected in the console status (model drop)."""

    def test_is_run_error_by_exit_code_and_markers(self):
        self.assertTrue(st._is_run_error(type("R", (), {"exit_code": 1, "text": "..."})()))
        self.assertTrue(st._is_run_error(type("R", (), {"exit_code": 0, "text": "API Error: 502 ..."})()))
        self.assertFalse(st._is_run_error(type("R", (), {"exit_code": 0, "text": "Exchange rate ..."})()))

    def test_report_and_snapshot(self):
        st._report_run_error("API Error: 502")
        text, ts = st._snapshot_run_error()
        self.assertEqual(text, "API Error: 502")
        self.assertGreater(ts, 0)

    def test_status_loop_shows_run_error(self):
        st._STATUS_FILTER._last_error = None  # Telegram link is fine

        async def scenario():
            printed = []
            st._draw_status = lambda lines: printed.append(lines)
            st._report_run_error("API Error: 502 Cannot connect")
            stop = asyncio.Event()
            task = asyncio.create_task(st._status_loop(stop))
            await asyncio.sleep(0.35)
            task.cancel()
            return printed

        printed = asyncio.run(scenario())
        self.assertTrue(printed)
        last = printed[-1]
        self.assertEqual(len(last), 2)
        self.assertNotIn("🟢", last[0])
        self.assertIn("❌", last[1])
        self.assertIn("API Error", last[1])


class TestModelUnavailable(unittest.TestCase):
    """_is_model_unavailable: determines model unavailability (issue #5)."""

    def _res(self, text, exit_code=0):
        return type("R", (), {"text": text, "exit_code": exit_code})()

    def test_model_unavailable_markers(self):
        self.assertTrue(st._is_model_unavailable(self._res("API Error: 502 Cannot connect ...")))
        self.assertTrue(st._is_model_unavailable(self._res("Cannot connect to host")))
        self.assertTrue(st._is_model_unavailable(self._res("... 503 Service Unavailable")))
        self.assertTrue(st._is_model_unavailable(self._res("model not found")))

    def test_normal_answer_not_unavailable(self):
        self.assertFalse(st._is_model_unavailable(self._res("Exchange rate for tomorrow...")))
        self.assertFalse(st._is_model_unavailable(self._res("")))

    def test_other_error_not_unavailable(self):
        self.assertFalse(st._is_model_unavailable(self._res("Error: prompt too long")))


class TestDrawStatus(unittest.TestCase):
    """_draw_status redraws the block in place, not accumulating a cascade of lines."""

    def _draw_sequence(self, states):
        """Run a series of _draw_status, return the concatenated ANSI sequence."""
        buf: list[str] = []
        st._use_color = lambda: True
        orig_write = sys.stdout.write
        try:
            sys.stdout.write = buf.append
            for s in states:
                st._draw_status(s)
        finally:
            sys.stdout.write = orig_write
        return "".join(buf)

    def test_redraw_clears_previous_block(self):
        out = self._draw_sequence([
            ["\x1b[32mWorking [t1]"],
            ["\x1b[32mWorking [t2]", "\x1b[31m❌ Error: failure [t2]\x1b[0m"],
            ["\x1b[32mWorking [t3]", "\x1b[31m❌ Error: failure [t3]\x1b[0m"],
        ])
        self.assertIn("\x1b[J", out)
        self.assertIn("\x1b[2F", out)
        self.assertGreaterEqual(out.count("\x1b[J"), 2)

    def test_prev_lines_tracks_height(self):
        buf: list[str] = []
        st._use_color = lambda: True
        orig = sys.stdout.write
        try:
            sys.stdout.write = buf.append
            st._draw_status(["\x1b[32mWorking [t1]"])
            self.assertEqual(st._STATUS_PREV_LINES, 1)
            st._draw_status(["\x1b[32mWorking [t2]", "❌ Error [t2]"])
            self.assertEqual(st._STATUS_PREV_LINES, 2)
            st._draw_status(["\x1b[32mWorking [t3]", "❌ Error [t3]"])
            self.assertEqual(st._STATUS_PREV_LINES, 2)
        finally:
            sys.stdout.write = orig

    def test_redraw_resets_column(self):
        # A redraw must return the cursor to column 0 (\\r) before drawing each
        # row: \\033[F moves up but keeps the column, so without \\r the new block
        # is drawn offset and the old one is left visible (two "Working" lines).
        out = self._draw_sequence([
            ["🟢 Working [t1]"],
            ["🟢 Working [t2]", "❌ Error: failure [t2]"],
        ])
        self.assertIn("\r\x1b[2K", out)

    def test_draw_status_trims_long_line(self):
        # A long line would wrap and break the block height tracking (a wrapped
        # row occupies an extra visual line). _fit_width trims it to the width.
        long = "x" * 500
        out = self._draw_sequence([["🟢 Working [t1]", long]])
        self.assertNotIn(long, out)

    def test_fit_width_short(self):
        self.assertEqual(st._fit_width("short line"), "short line")

    def test_fit_width_long_truncates(self):
        self.assertEqual(st._fit_width("a" * 500)[-1], "…")
        self.assertLess(len(st._fit_width("a" * 500)), 500)


if __name__ == "__main__":
    unittest.main()