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
            status_errs = []

            # Keep the backoff instant so the whole retry loop finishes fast and
            # we can assert on the diagnostic without waiting a real backoff.
            def fast_backoff(attempt):
                return 0.1

            with mock.patch.object(client, "ui_log", lambda s: seen.append(s)), \
                 mock.patch.object(client, "_use_color", lambda: False), \
                 mock.patch.object(config, "RETRY_LIMIT", 2), \
                 mock.patch.object(client, "_report_run_error",
                                   lambda note: status_errs.append(note)), \
                 mock.patch.object(client, "_retry_backoff_delay", fast_backoff):
                # After RETRY_LIMIT stalls it re-raises, as in production.
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        client._start_with_retry(_HangApp()), timeout=5.0
                    )
            return seen, status_errs

        out, status_errs = asyncio.run(run())
        # A diagnostic about the stalled connection was shown to the user.
        # Language depends on BOT_LANG, so just require non-empty diagnostics.
        self.assertTrue(out, "no diagnostic was shown to the user")
        # The stall was also pushed to the status bar so it is visible
        # during the connection attempts, not only after they finish.
        self.assertTrue(status_errs, "stall not reported to the status bar")


class _AuthApp:
    """Stand-in Client whose start() raises an auth error (dead session)."""

    async def start(self):
        from pyrogram.errors import AuthKeyUnregistered

        raise AuthKeyUnregistered("AUTH_KEY_UNREGISTERED")


class TestConnectAuthError(unittest.TestCase):
    def test_auth_error_not_retried_and_reported(self):
        # A dead session must NOT be retried (auth can't be fixed by retrying).
        # _start_with_retry raises immediately; main() shows the re-auth message.
        with mock.patch.object(client, "_use_color", lambda: False), \
             mock.patch.object(client, "_retry_backoff_delay",
                               lambda attempt: (_ for _ in ()).throw(
                                   AssertionError("auth must not be retried"))):
            with self.assertRaises(Exception):
                asyncio.run(client._start_with_retry(_AuthApp()))


class _MemoryStorage:
    """Thin stand-in for the pyrogram session storage (connects=False beyond a flag)."""

    def __init__(self, user_id=None):
        self._user_id = user_id
        self._auth_key = b"\x00" if user_id is not None else None
        self._is_bot = False
        self.saved = 0

    async def user_id(self, value=object):
        if value is not object:
            self._user_id = value
        return self._user_id

    async def auth_key(self, value=object):
        if value is not object:
            self._auth_key = value
        return self._auth_key

    async def is_bot(self, value=object):
        if value is not object:
            self._is_bot = value
        return self._is_bot

    async def save(self):
        self.saved += 1


class _AuthApp2:
    """Stand-in Client that connects, has a storage, and probes auth via GetState."""

    def __init__(self, *, dead_session=False, network_fail=False):
        self.storage = _MemoryStorage(user_id=123 if dead_session else None)
        self.is_connected = False
        self.session = object()
        self.dead_session = dead_session
        self.network_fail = network_fail
        self.authorized = 0

    async def connect(self):
        if self.network_fail:
            raise ConnectionError("network down")
        self.is_connected = True
        return bool(self.storage._user_id)

    async def disconnect(self):
        self.is_connected = False

    async def authorize(self):
        self.authorized += 1

    async def invoke(self, fn):
        if self.dead_session:
            from pyrogram.errors import AuthKeyUnregistered

            raise AuthKeyUnregistered("AUTH_KEY_UNREGISTERED")
        return None


class TestEnsureAuthorized(unittest.TestCase):
    def test_dead_session_is_reauthorized(self):
        app = _AuthApp2(dead_session=True)
        asyncio.run(client._ensure_authorized(app))
        # The stale session was cleared and Pyrogram re-authorized (login prompt).
        self.assertIsNone(app.storage._user_id)
        self.assertIsNone(app.storage._auth_key)
        self.assertEqual(app.authorized, 1)
        self.assertEqual(app.storage.saved, 1)

    def test_network_failure_does_not_block_or_authorize(self):
        app = _AuthApp2(network_fail=True)
        asyncio.run(client._ensure_authorized(app))
        # A network error is not a session problem: no authorize, no hang.
        self.assertEqual(app.authorized, 0)


if __name__ == "__main__":
    unittest.main()