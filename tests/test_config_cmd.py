"""Tests for the /config editable-settings allowlist and live config helpers."""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import config, commands  # noqa: E402


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
    """/config must apply a change and answer (not crash) for any argument shape.

    We do NOT mock set_config_value / write_config_value here — the point is to
    catch a regression in the in-memory apply (e.g. setattr on the module) and in
    the config.env write. Only _reply (the Telegram send) is faked.
    """

    def setUp(self):
        self.captured = []
        self._reply_saved = commands._reply

        async def fake_reply(message, text):
            self.captured.append(text)

        commands._reply = fake_reply

        # A temp config.env so the test never touches the real one.
        self._cfg = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self._cfg.write("BOT_LANG=ru\nSANDBOX_COMMAND=@Людочка\n")
        self._cfg.close()
        self._env_path_saved = config.CONFIG_ENV_PATH
        config.CONFIG_ENV_PATH = Path(self._cfg.name)

    def tearDown(self):
        commands._reply = self._reply_saved
        config.CONFIG_ENV_PATH = self._env_path_saved

    async def test_valid_sandbox_applies_and_answers(self):
        await commands._handle_config(object(), ["/config", "SANDBOX_COMMAND=@Олег"])
        # Applied in-memory, written to config.env, and answered.
        self.assertEqual(config.SANDBOX_COMMAND, "@Олег")
        self.assertIn("SANDBOX_COMMAND=@Олег", Path(self._cfg.name).read_text())
        self.assertTrue(any("SANDBOX_COMMAND = @Олег" in r for r in self.captured))

    async def test_spaces_around_equals_parse(self):
        await commands._handle_config(object(),
                                      ["/config", "SANDBOX_COMMAND", "=", "@Олег"])
        self.assertEqual(config.SANDBOX_COMMAND, "@Олег")
        self.assertTrue(any("SANDBOX_COMMAND = @Олег" in r for r in self.captured))

    async def test_unknown_key_answers(self):
        await commands._handle_config(object(), ["/config", "FOO=1"])
        # Nothing applied, but the bot still answers.
        self.assertTrue(any("FOO" in r for r in self.captured))

    async def test_invalid_value_answers(self):
        await commands._handle_config(object(), ["/config", "AGENT_GENDER=other"])
        # Bad value — the setting is unchanged, but the bot lists the allowed ones.
        self.assertTrue(any("male" in r for r in self.captured))


if __name__ == "__main__":
    unittest.main()