"""Tests for the setup script (issue #4): pure functions without interactive input."""

import unittest

from claude_tg_bot.setup import GROUPS, _sanitize, _strip_inline_comment


class TestGroupRequired(unittest.TestCase):
    """Mandatory parameters are marked required=True."""

    def test_projects_root_required(self):
        required_keys = {k for g in GROUPS for k, req, _d, _h in g[1] if req}
        self.assertIn("PROJECTS_ROOT", required_keys)

    def test_required_keys_present(self):
        for key in ("API_ID", "API_HASH", "PHONE", "ALLOWED_USERS", "PROJECTS_ROOT"):
            self.assertIn(key, {k for g in GROUPS for k, _r, _d, _h in g[1]})


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