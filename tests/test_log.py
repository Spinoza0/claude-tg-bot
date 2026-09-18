"""Logging tests (issue #12): the --log flag, levels, the file handler."""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_tg_bot import log


class TestParseLogFlag(unittest.TestCase):
    """parse_log_flag parses the --log[=level] flag from argv."""

    def test_no_flag_disabled(self):
        enabled, level = log.parse_log_flag(["python", "-m", "claude_tg_bot"])
        self.assertFalse(enabled)
        self.assertEqual(level, logging.ERROR)

    def test_bare_flag_defaults_to_error(self):
        enabled, level = log.parse_log_flag(["--log"])
        self.assertTrue(enabled)
        self.assertEqual(level, logging.ERROR)

    def test_log_equals_info(self):
        enabled, level = log.parse_log_flag(["--log=info"])
        self.assertTrue(enabled)
        self.assertEqual(level, logging.INFO)

    def test_log_equals_debug(self):
        enabled, level = log.parse_log_flag(["--log=debug"])
        self.assertTrue(enabled)
        self.assertEqual(level, logging.DEBUG)

    def test_log_equals_error(self):
        enabled, level = log.parse_log_flag(["--log=error"])
        self.assertTrue(enabled)
        self.assertEqual(level, logging.ERROR)

    def test_unknown_level_falls_back_to_error(self):
        enabled, level = log.parse_log_flag(["--log=verbose"])
        self.assertTrue(enabled)
        self.assertEqual(level, logging.ERROR)

    def test_among_other_args(self):
        enabled, level = log.parse_log_flag(["--log=info", "--verbose"])
        self.assertTrue(enabled)
        self.assertEqual(level, logging.INFO)


class TestParseLevel(unittest.TestCase):
    """parse_level converts a string to a logging level."""

    def test_none_is_error(self):
        self.assertEqual(log.parse_level(None), logging.ERROR)

    def test_empty_is_error(self):
        self.assertEqual(log.parse_level(""), logging.ERROR)

    def test_case_insensitive(self):
        self.assertEqual(log.parse_level("INFO"), logging.INFO)
        self.assertEqual(log.parse_level("Debug"), logging.DEBUG)

    def test_warning_alias(self):
        self.assertEqual(log.parse_level("warn"), logging.WARNING)


class TestSetupLogging(unittest.TestCase):
    """setup_logging creates a file and attaches a FileHandler."""

    def test_creates_file_and_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "bot.log"
            returned = log.setup_logging(logging.INFO, log_path)
            logger = logging.getLogger("claude_tg_bot")
            self.assertTrue(log_path.exists())
            self.assertEqual(returned, log_path)
            self.assertTrue(any(isinstance(h, logging.FileHandler) for h in logger.handlers))
            logger.info("test-message")
            logger.handlers[-1].flush()
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("test-message", content)


class TestOldLogsAndCleanup(unittest.TestCase):
    """old_logs and maybe_cleanup_old_logs."""

    def _make_logs(self, tmp, names=("claude-tg-bot-2026-09-15.log",)):
        d = Path(tmp)
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_text("x", encoding="utf-8")
        return d

    def test_old_logs_lists_only_log_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_logs(tmp, ["claude-tg-bot-a.log", "other.txt"])
            with mock.patch.object(log, "log_dir", return_value=d):
                logs = log.old_logs()
            self.assertEqual([p.name for p in logs], ["claude-tg-bot-a.log"])


if __name__ == "__main__":
    unittest.main()