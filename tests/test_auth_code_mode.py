"""AUTH_CODE_MODE drives the login-code channel (auto/sms/call)."""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_tg_bot import client  # noqa: E402
from claude_tg_bot import config  # noqa: E402


class _FakeClient:
    async def send_phone_number_code(self, phone_number, *args, **kwargs):
        self.last_kwargs = kwargs
        return "SENT"


class TestAuthCodeMode(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = _FakeClient()
        self.fake.send_phone_number_code = types.MethodType(
            client._sms_first_send_phone_number_code, self.fake
        )
        # The wrapper calls Client.send_phone_number_code(self, ...); point it at
        # the fake so no real network call happens and kwargs are captured.
        self._orig_client = client.Client
        client.Client = _FakeClient

    def tearDown(self):
        client.Client = self._orig_client
        config.AUTH_CODE_MODE = "auto"

    async def _run(self, mode):
        config.AUTH_CODE_MODE = mode
        return await self.fake.send_phone_number_code("+70000000000")

    async def test_auto_no_settings(self):
        await self._run("auto")
        self.assertNotIn("settings", self.fake.last_kwargs)

    async def test_sms_forces_no_call(self):
        await self._run("sms")
        s = self.fake.last_kwargs.get("settings")
        self.assertIsNotNone(s)
        self.assertFalse(s.allow_missed_call)
        self.assertFalse(s.is_current_phone_number)

    async def test_call_allows_missed_call(self):
        await self._run("call")
        s = self.fake.last_kwargs.get("settings")
        self.assertIsNotNone(s)
        self.assertTrue(s.allow_missed_call)

    async def test_deprecated_current_number_dropped(self):
        await self._run("sms")
        self.assertNotIn("current_number", self.fake.last_kwargs)


if __name__ == "__main__":
    unittest.main()