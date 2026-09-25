"""Tests for the /config editable-settings allowlist and live config helpers."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import config  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()