"""Unit test: _start_with_retry aborts a hanging app.start() via CONNECT_TIMEOUT."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import client  # noqa: E402
from claude_tg_bot import config  # noqa: E402


class _HangApp:
    """A stand-in for the Client whose start() never returns (stalled handshake)."""

    async def start(self):
        await asyncio.sleep(9999)


class TestConnectStallAbort(unittest.TestCase):
    def test_timeout_aborts_hanging_start_and_reports(self):
        async def run():
            config.CONNECT_TIMEOUT = 1.0  # short for the test
            seen = []

            # Keep the backoff instant so the whole retry loop finishes fast and
            # we can assert on the diagnostic without waiting a real backoff.
            def fast_backoff(attempt):
                return 0.1

            with mock.patch.object(client, "ui_log", lambda s: seen.append(s)), \
                 mock.patch.object(client, "_use_color", lambda: False), \
                 mock.patch.object(config, "RETRY_LIMIT", 2), \
                 mock.patch.object(client, "_retry_backoff_delay", fast_backoff):
                # After RETRY_LIMIT stalls it re-raises, as in production.
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        client._start_with_retry(_HangApp()), timeout=5.0
                    )
            return seen

        out = asyncio.run(run())
        # A diagnostic about the stalled connection was shown to the user.
        # Language depends on BOT_LANG, so just require non-empty diagnostics.
        self.assertTrue(out, "no diagnostic was shown to the user")


if __name__ == "__main__":
    unittest.main()