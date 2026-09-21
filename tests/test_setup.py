"""Tests for the setup script (issue #4): pure functions without interactive input."""

import unittest
from unittest import mock

from claude_tg_bot.setup import BOT_LANG_CHOICES, GROUPS, _ask, _sanitize, _strip_inline_comment


class TestGroupRequired(unittest.TestCase):
    """Mandatory parameters are marked required=True."""

    def test_projects_root_required(self):
        required_keys = {k for g in GROUPS for k, req, _d, _h in g[1] if req}
        self.assertIn("PROJECTS_ROOT", required_keys)

    def test_required_keys_present(self):
        for key in ("API_ID", "API_HASH", "PHONE", "ALLOWED_USERS", "PROJECTS_ROOT"):
            self.assertIn(key, {k for g in GROUPS for k, _r, _d, _h in g[1]})


class TestBotLangChoice(unittest.TestCase):
    """BOT_LANG is asked in GROUPS so the language can be chosen at setup."""

    def test_bot_lang_in_groups(self):
        group_keys = {k for g in GROUPS for k, _r, _d, _h in g[1]}
        self.assertIn("BOT_LANG", group_keys)

    def test_ask_validates_choices(self):
        with mock.patch("builtins.input", side_effect=["zz", "ru"]):
            self.assertEqual(_ask("BOT_LANG", "desc", "", "en", True, choices=["en", "ru"]), "ru")

    def test_ask_keeps_current_on_enter(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(_ask("BOT_LANG", "desc", "en", "en", True, choices=["en", "ru"]), "en")

    def test_choices_match_available(self):
        self.assertIn("en", BOT_LANG_CHOICES)


class TestStripInlineComment(unittest.TestCase):
    def test_no_comment(self):
        self.assertEqual(_strip_inline_comment("12345"), "12345")

    def test_inline_comment(self):
        self.assertEqual(_strip_inline_comment("5 # attempts"), "5")

    def test_comment_inside_quotes_kept(self):
        self.assertEqual(_strip_inline_comment('"a # b"'), '"a # b"')

    def test_quoted_then_comment(self):
        self.assertEqual(_strip_inline_comment('"something" # tail'), '"something"')


class TestSanitize(unittest.TestCase):
    def test_plain_value(self):
        self.assertEqual(_sanitize("claude"), "claude")

    def test_value_with_spaces_quoted(self):
        self.assertEqual(_sanitize("--provider openai"), '"--provider openai"')

    def test_value_with_hash_quoted(self):
        self.assertEqual(_sanitize("a#b"), '"a#b"')

    def test_empty_kept_empty(self):
        self.assertEqual(_sanitize(""), "")


if __name__ == "__main__":
    unittest.main()