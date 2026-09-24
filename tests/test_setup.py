"""Tests for the setup script (issue #4): pure functions without interactive input."""

import unittest
from unittest import mock

from claude_tg_bot.setup import BOT_LANG_CHOICES, DEFAULT_CHOICES, GROUPS, _ask, _reserved_collision, _sanitize, _strip_inline_comment


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


class TestBehaviorFlags(unittest.TestCase):
    """The "hidden" runtime flags are now asked at setup (KEEP_AWAKE etc.)."""

    def test_flags_in_groups(self):
        group_keys = {k for g in GROUPS for k, _r, _d, _h in g[1]}
        for key in ("KEEP_AWAKE", "AUTO_DELETE_ATTACH", "DELETE_MODE", "CLAUDE_PERMISSION_MODE"):
            self.assertIn(key, group_keys)

    def test_every_choice_setting_has_choices(self):
        for key, choices in DEFAULT_CHOICES.items():
            self.assertGreater(len(choices), 0)

    def test_bool_choice_validates(self):
        # invalid bool value is rejected, then "true" accepted
        with mock.patch("builtins.input", side_effect=["maybe", "true"]):
            self.assertEqual(
                _ask("KEEP_AWAKE", "desc", "", "false", False, choices=["true", "false"]),
                "true",
            )


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


class TestReservedCollision(unittest.TestCase):
    """SANDBOX_COMMAND must not shadow a reserved bot command (issue #35)."""

    def test_plain_reserved_collides(self):
        self.assertTrue(_reserved_collision("/status"))

    def test_at_prefix_collides(self):
        self.assertTrue(_reserved_collision("@status"))

    def test_at_slash_collides(self):
        self.assertTrue(_reserved_collision("@/status"))

    def test_case_insensitive_collides(self):
        self.assertTrue(_reserved_collision("STATUS"))

    def test_default_helpbot_ok(self):
        self.assertFalse(_reserved_collision("@helpbot"))

    def test_custom_trigger_ok(self):
        self.assertFalse(_reserved_collision("@mytool"))

    def test_empty_ok(self):
        self.assertFalse(_reserved_collision(""))


if __name__ == "__main__":
    unittest.main()