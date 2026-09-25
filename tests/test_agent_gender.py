"""Tests for the agent gender config, gender prompt, and data-dir persistence."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import config, i18n  # noqa: E402
from claude_tg_bot.runner import _gender_prompt  # noqa: E402


class TestAgentGender(unittest.TestCase):
    def test_gender_prompt_key_male(self):
        self.assertEqual(_gender_prompt("male"), i18n.t("gender.prompt_male"))

    def test_gender_prompt_key_female(self):
        self.assertEqual(_gender_prompt("female"), i18n.t("gender.prompt_female"))

    def test_default_is_male(self):
        self.assertIn(config.AGENT_GENDER, {"male", "female"})

    def test_state_file_in_data_dir(self):
        self.assertEqual(config.STATE_FILE, Path.home() / ".claude-tg-bot" / "state.json")

    def test_data_dir_is_home(self):
        self.assertEqual(config.DATA_DIR, Path.home() / ".claude-tg-bot")


if __name__ == "__main__":
    unittest.main()