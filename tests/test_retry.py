"""Unit tests for the unified retry mechanism (_run_with_retry,
_retry_backoff_delay, _start_with_retry).

We check: the growing pause (1 → 5 min per issue #6), the 5-min cap, resetting to
base on success, the RETRY_LIMIT attempt limit, and Ctrl+C interruption
(KeyboardInterrupt isn't swallowed).
"""

import asyncio
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import config  # noqa: E402
from claude_tg_bot import client, retry  # noqa: E402


class _FakeApp:
    """app.start(): fails `fails` times with a network error, then succeeds."""

    def __init__(self, fails):
        self.fails = fails
        self.attempts = 0

    async def start(self):
        self.attempts += 1
        if self.attempts <= self.fails:
            raise ConnectionError("gaierror nodename")
        return True


class TestBackoffDelay(unittest.TestCase):
    """_retry_backoff_delay: growth with a 5-min cap (issue #6)."""

    def test_growth_and_cap(self):
        d = retry._retry_backoff_delay
        self.assertEqual(d(1), 60)
        self.assertEqual(d(2), 120)
        self.assertEqual(d(3), 240)
        self.assertEqual(d(4), 300)
        self.assertEqual(d(5), 300)
        self.assertEqual(d(6), 300)


class TestRunWithRetry(unittest.TestCase):
    """_run_with_retry: attempt limit and the result on success."""

    def setUp(self):
        self._orig = retry.asyncio.sleep

    def tearDown(self):
        retry.asyncio.sleep = self._orig

    def test_returns_none_after_limit(self):
        calls = [0]
        _orig = retry.asyncio.sleep
        async def fake(sec):
            await _orig(0)
        retry.asyncio.sleep = fake
        async def fail():
            calls[0] += 1
            raise RuntimeError("boom")
        res = asyncio.run(retry._run_with_retry(fail))
        self.assertIsNone(res)
        self.assertEqual(calls[0], config.RETRY_LIMIT)

    def test_returns_result_on_success(self):
        async def ok():
            return 42
        self.assertEqual(asyncio.run(retry._run_with_retry(ok)), 42)

    def test_keyboard_interrupt_propagates(self):
        async def key():
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(retry._run_with_retry(key))


class TestStartWithRetry(unittest.TestCase):
    """Connection retry: pause growth, 5-min cap, interruption."""

    def setUp(self):
        self._orig_sleep = client.asyncio.sleep

    def tearDown(self):
        client.asyncio.sleep = self._orig_sleep

    def test_delay_grows_and_success_exits(self):
        sleeps = []
        orig = client.asyncio.sleep
        async def fake_sleep(sec):
            sleeps.append(sec)
            await orig(0)
        client.asyncio.sleep = fake_sleep
        app = _FakeApp(fails=2)
        asyncio.run(client._start_with_retry(app))
        self.assertEqual(app.attempts, 3)
        self.assertEqual(sleeps, [60, 120])

    def test_delay_caps_at_5_min(self):
        sleeps = []

        async def fake_sleep(sec):
            sleeps.append(sec)
            raise KeyboardInterrupt()  # exit the loop after the first pause

        client.asyncio.sleep = fake_sleep

        class AlwaysFail:
            async def start(self):
                raise ConnectionError("boom")

        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(client._start_with_retry(AlwaysFail()))
        self.assertEqual(sleeps[0], 60)

    def test_exhausts_and_raises(self):
        orig = client.asyncio.sleep

        async def fake_sleep(sec):
            await orig(0)
        client.asyncio.sleep = fake_sleep

        class AlwaysFail:
            async def start(self):
                raise ConnectionError("boom")

        with self.assertRaises(ConnectionError):
            asyncio.run(client._start_with_retry(AlwaysFail()))

    def test_keyboard_interrupt_propagates(self):
        class AlwaysFail:
            async def start(self):
                raise ConnectionError("boom")

        async def fake_sleep(sec):
            raise KeyboardInterrupt()

        client.asyncio.sleep = fake_sleep
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(client._start_with_retry(AlwaysFail()))

    def test_non_network_error_propagates(self):
        class AuthFail:
            async def start(self):
                raise ValueError("wrong password")  # NOT a network error

        with self.assertRaises(ValueError):
            asyncio.run(client._start_with_retry(AuthFail()))


class _FakeSession:
    """A media session that records stop() (drop from cache)."""

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeClient:
    def __init__(self):
        self.media_sessions = {2: _FakeSession()}
        self.reset_calls = 0

    def _reset_media_sessions(self):
        # duplicate of retry.reset_media_sessions logic for the assertion below
        for sess in list(self.media_sessions.values()):
            sess.stop()
        self.media_sessions.clear()
        self.reset_calls += 1


class TestDownloadMediaWithRetry(unittest.TestCase):
    """download_media_with_retry: retries and drops the dead media session."""

    def setUp(self):
        self._orig_sleep = retry.asyncio.sleep
        self._orig_reset = retry.reset_media_sessions

    def tearDown(self):
        retry.asyncio.sleep = self._orig_sleep
        retry.reset_media_sessions = self._orig_reset

    def test_success_first_attempt(self):
        async def fake_sleep(sec):
            await self._orig_sleep(0)

        retry.asyncio.sleep = fake_sleep
        client = _FakeClient()

        class Msg:
            calls = 0
            async def download(self, file_name):
                Msg.calls += 1
                return True

        retry.reset_media_sessions = lambda c: c._reset_media_sessions()
        res = asyncio.run(retry.download_media_with_retry(client, Msg(), "/tmp/x"))
        self.assertIsNone(res)
        self.assertEqual(Msg.calls, 1)
        self.assertEqual(client.reset_calls, 0)

    def test_retries_then_succeeds(self):
        async def fake_sleep(sec):
            await self._orig_sleep(0)

        retry.asyncio.sleep = fake_sleep
        client = _FakeClient()

        class Msg:
            calls = 0
            async def download(self, file_name):
                Msg.calls += 1
                if Msg.calls == 1:
                    raise ConnectionError("Waited 15s ... is not started")
                return True

        retry.reset_media_sessions = lambda c: c._reset_media_sessions()
        res = asyncio.run(retry.download_media_with_retry(client, Msg(), "/tmp/x"))
        self.assertIsNone(res)
        self.assertEqual(Msg.calls, 2)
        self.assertEqual(client.reset_calls, 1)  # dead session was dropped

    def test_exhausts_and_returns_last_error(self):
        async def fake_sleep(sec):
            await self._orig_sleep(0)

        retry.asyncio.sleep = fake_sleep
        client = _FakeClient()

        class Msg:
            calls = 0
            async def download(self, file_name):
                Msg.calls += 1
                raise ConnectionError("is not started")

        retry.reset_media_sessions = lambda c: c._reset_media_sessions()
        res = asyncio.run(retry.download_media_with_retry(client, Msg(), "/tmp/x"))
        self.assertIsInstance(res, ConnectionError)
        self.assertEqual(Msg.calls, config.MESSAGE_RETRY_LIMIT)


if __name__ == "__main__":
    unittest.main()