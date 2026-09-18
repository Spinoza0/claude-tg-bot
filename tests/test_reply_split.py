"""Tests for splitting long text into messages (Telegram limit 4096)."""

import unittest

from claude_tg_bot.reply import _split_text


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