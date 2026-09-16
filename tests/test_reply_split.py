"""Тесты разбиения длинного текста на сообщения (лимит Telegram 4096)."""

import unittest

from claude_tg_bot.reply import _split_text


class TestSplitText(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(_split_text("короткий текст"), ["короткий текст"])

    def test_whitespace_stripped(self):
        self.assertEqual(_split_text("  текст  "), ["текст"])

    def test_long_text_splits_into_multiple(self):
        text = "\n\n".join(f"абзац {i}: " + "слово " * 400 for i in range(10))
        chunks = _split_text(text)
        self.assertGreater(len(chunks), 1)
        # ни один кусок не превышает лимит
        for c in chunks:
            self.assertLessEqual(len(c), 4000)

    def test_no_midword_cut(self):
        # Длинное слово не должно рваться посередине; куски по границам пробелов
        text = ("слово " * 1000).strip()
        chunks = _split_text(text)
        for c in chunks:
            self.assertTrue(c.endswith("слово") or c.isspace() or c == "")

    def test_concatenation_preserves_content(self):
        text = "абзац один\n\nабзац два\n\nабзац три, но очень длинный на несколько строк"
        chunks = _split_text(text)
        # в переводе обратно в один текст не теряем ключевые слова
        joined = " ".join(chunks)
        for word in ("абзац", "два", "три", "несколько"):
            self.assertIn(word, joined)


if __name__ == "__main__":
    unittest.main()