"""Tests for splitting long text into messages (Telegram limit 4096)."""

import unittest

from claude_tg_bot.reply import _split_text, _mask_cwd


class TestMaskCwd(unittest.TestCase):
    """_mask_cwd hides the working-dir prefix, keeping paths relative to it."""

    CWD = "/Users/ivan/.claude-tg-bot/sandbox/helper"

    def test_file_path_hidden_to_relative(self):
        text = "Save ok: /Users/ivan/.claude-tg-bot/sandbox/helper/.claude_tg_bot_attach/a.ogg"
        self.assertEqual(
            _mask_cwd(text, self.CWD),
            "Save ok: /.claude_tg_bot_attach/a.ogg",
        )

    def test_double_slash_avoided_on_consecutive(self):
        # "<cwd>/X" -> "/X": no leading double slash.
        self.assertEqual(_mask_cwd(f"{self.CWD}/x/../y", self.CWD), "/x/../y")

    def test_other_roots_untouched(self):
        out = _mask_cwd("root is /Users/ivan/.claude-tg-bot/sandbox", self.CWD)
        self.assertIn("/Users/ivan/.claude-tg-bot/sandbox", out)

    def test_no_match_passthrough(self):
        text = "no path here"
        self.assertEqual(_mask_cwd(text, self.CWD), text)

    def test_empty_cwd_noop(self):
        text = f"a {self.CWD}/x b"
        self.assertEqual(_mask_cwd(text, ""), text)


class TestSplitText(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(_split_text("short text"), ["short text"])

    def test_whitespace_stripped(self):
        self.assertEqual(_split_text("  text  "), ["text"])

    def test_long_text_splits_into_multiple(self):
        text = "\n\n".join(f"paragraph {i}: " + "word " * 400 for i in range(10))
        chunks = _split_text(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 4000)

    def test_no_midword_cut(self):
        text = ("word " * 1000).strip()
        chunks = _split_text(text)
        for c in chunks:
            self.assertTrue(c.endswith("word") or c.isspace() or c == "")

    def test_concatenation_preserves_content(self):
        text = "paragraph one\n\nparagraph two\n\nparagraph three, but very long over several lines"
        chunks = _split_text(text)
        joined = " ".join(chunks)
        for word in ("paragraph", "two", "three", "several"):
            self.assertIn(word, joined)


if __name__ == "__main__":
    unittest.main()