"""Юнит-тесты claude_runner.py: сборка команды запуска Claude."""

import shlex
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from claude_runner import _build_command  # noqa: E402


class TestBuildCommand(unittest.TestCase):
    """Формирование argv для Claude."""

    def test_basic_command(self):
        cmd = _build_command("тестовый промпт", Path("/tmp"), None)
        self.assertEqual(cmd[0], config.CLAUDE_COMMAND)
        self.assertIn("--print", cmd)
        self.assertIn("тестовый промпт", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        # Авто-режим должен передаваться
        self.assertIn("--permission-mode", cmd)
        self.assertIn(config.CLAUDE_PERMISSION_MODE, cmd)

    def test_continue(self):
        cmd = _build_command("промпт", Path("/tmp"), continue_session=True)
        self.assertIn("--continue", cmd)

    def test_no_continue_without_session(self):
        cmd = _build_command("промпт", Path("/tmp"), continue_session=False)
        self.assertFalse(any(c.startswith("--continue") for c in cmd))

    def test_command_args_appended_when_configured(self):
        cmd = _build_command("промпт", Path("/tmp"), None)
        extra = shlex.split(config.COMMAND_ARGS)
        if extra:
            for arg in extra:
                self.assertIn(arg, cmd)

    def test_system_prompt_added_when_set(self):
        cmd = _build_command("промпт", Path("/tmp"), None)
        if config.CLAUDE_SYSTEM_PROMPT:
            self.assertIn("--append-system-prompt", cmd)
            self.assertIn(config.CLAUDE_SYSTEM_PROMPT, cmd)
        else:
            self.assertNotIn("--append-system-prompt", cmd)


if __name__ == "__main__":
    unittest.main()