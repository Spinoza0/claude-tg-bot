"""Тесты setup-скрипта (issue #4): чистые функции без интерактивного ввода."""

import unittest

from claude_tg_bot.setup import _sanitize, _strip_inline_comment


class TestStripInlineComment(unittest.TestCase):
    def test_no_comment(self):
        self.assertEqual(_strip_inline_comment("12345"), "12345")

    def test_inline_comment(self):
        self.assertEqual(_strip_inline_comment("5 # попыток"), "5")

    def test_comment_inside_quotes_kept(self):
        # # внутри кавычек — не комментарий
        self.assertEqual(_strip_inline_comment('"a # b"'), '"a # b"')

    def test_quoted_then_comment(self):
        self.assertEqual(_strip_inline_comment('"что-то" # хвост'), '"что-то"')


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