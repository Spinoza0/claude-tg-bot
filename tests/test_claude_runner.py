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

    def test_session_resume(self):
        cmd = _build_command("промпт", Path("/tmp"), "session-abc")
        self.assertIn("--resume=session-abc", cmd)

    def test_no_resume_without_session(self):
        cmd = _build_command("промпт", Path("/tmp"), None)
        self.assertFalse(any(c.startswith("--resume") for c in cmd))

    def test_command_args_appended_when_configured(self):
        cmd = _build_command("промпт", Path("/tmp"), None)
        extra = shlex.split(config.COMMAND_ARGS)
        if extra:
            for arg in extra:
                self.assertIn(arg, cmd)


if __name__ == "__main__":
    unittest.main()