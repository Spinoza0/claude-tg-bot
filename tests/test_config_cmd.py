"""Tests for the /config editable-settings allowlist and live config helpers."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import config, i18n  # noqa: E402
from claude_tg_bot import commands  # noqa: E402


class TestConfigEditable(unittest.TestCase):
    def test_editable_keys(self):
        self.assertTrue(config.is_config_key_editable("BOT_LANG"))
        self.assertTrue(config.is_config_key_editable("AGENT_GENDER"))
        self.assertTrue(config.is_config_key_editable("SANDBOX_COMMAND"))

    def test_case_insensitive(self):
        self.assertTrue(config.is_config_key_editable("bot_lang"))
        self.assertTrue(config.is_config_key_editable("sandbox_command"))

    def test_non_editable(self):
        for key in ("API_HASH", "MT_PROXY", "CLAUDE_COMMAND", "COMMAND_ARGS",
                    "ALLOWED_USERS", "RETRY_LIMIT", "KEEP_AWAKE", "PROJECTS_ROOT"):
            self.assertFalse(config.is_config_key_editable(key), key)

    def test_unknown_key(self):
        self.assertFalse(config.is_config_key_editable("FOO_BAR"))


@unittest.skipUnless(hasattr(commands, "_handle_config"), "/config handler absent")
class TestHandleConfig(unittest.IsolatedAsyncioTestCase):
    """/config must answer (not crash) for valid, invalid and unknown keys."""

    async def test_valid_sandbox_changes(self):
        self.captured = []
        saved = commands._reply
        async def fake_reply(message, text):
            self.captured.append(text)
        commands._reply = fake_reply
        saved_write = config.write_config_value
        config.write_config_value = lambda k, v: True
        saved_set = config.set_config_value
        config.set_config_value = lambda k, v: (setattr(config, k, v) or True)
        try:
            await commands._handle_config(object(), ["/config", "SANDBOX_COMMAND=@Олег"])
            self.assertTrue(any("SANDBOX_COMMAND = @Олег" in r for r in self.captured))
        finally:
            commands._reply = saved
            config.write_config_value = saved_write
            config.set_config_value = saved_set

    async def test_unknown_key_answers(self):
        self.captured = []
        saved = commands._reply
        async def fake_reply(message, text):
            self.captured.append(text)
        commands._reply = fake_reply
        saved_write = config.write_config_value
        config.write_config_value = lambda k, v: True
        saved_set = config.set_config_value
        config.set_config_value = lambda k, v: True
        try:
            await commands._handle_config(object(), ["/config", "FOO=1"])
            self.assertTrue(any("FOO" in r for r in self.captured))
        finally:
            commands._reply = saved
            config.write_config_value = saved_write
            config.set_config_value = saved_set


if __name__ == "__main__":
    unittest.main()