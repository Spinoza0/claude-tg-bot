"""Tests for splitting long text into messages (Telegram limit 4096)."""

import asyncio
import unittest
from unittest.mock import AsyncMock

from claude_tg_bot.reply import _split_text, _mask_cwd


class TestMaskCwd(unittest.TestCase):
    """_mask_cwd hides the working-dir prefix, keeping paths relative to it."""

    CWD = "/Users/ivan/StudioProjects/wb/test"
    PROJ = "test"

    def test_file_path_hidden_to_relative(self):
        text = f"Save ok: {self.CWD}/.claude_tg_bot_attach/a.ogg"
        self.assertEqual(
            _mask_cwd(text, self.CWD),
            "Save ok: /.claude_tg_bot_attach/a.ogg",
        )

    def test_double_slash_avoided_on_consecutive(self):
        # "<cwd>/X" -> "/X": no leading double slash.
        self.assertEqual(_mask_cwd(f"{self.CWD}/x/../y", self.CWD), "/x/../y")

    def test_path_at_end_of_line_masked(self):
        # A path at the very end (before a period) has no trailing "/" — the old
        # code missed it, so the full project path leaked. Now it is hidden (to "/").
        text = f"Current directory — {self.CWD}. I'll create the attachments folder."
        self.assertEqual(_mask_cwd(text, self.CWD), "Current directory — /. I'll create the attachments folder.")

    def test_path_at_end_of_string_masked(self):
        self.assertEqual(_mask_cwd(f"Current directory — {self.CWD}", self.CWD), "Current directory — /")

    def test_path_before_space_masked(self):
        self.assertEqual(_mask_cwd(f"dir {self.CWD} and next", self.CWD), "dir / and next")

    def test_show_name_keeps_project_in_status_help(self):
        # /status, /help keep the project name (with a leading "/") instead of "/".
        self.assertEqual(
            _mask_cwd(f"Current directory — {self.CWD}.", self.CWD, show_name=True),
            f"Current directory — /{self.PROJ}.",
        )

    def test_show_name_keeps_project_in_under_path(self):
        self.assertEqual(
            _mask_cwd(f"save {self.CWD}/.claude_tg_bot_attach/a.ogg", self.CWD, show_name=True),
            f"save /{self.PROJ}/.claude_tg_bot_attach/a.ogg",
        )

    def test_other_roots_untouched(self):
        out = _mask_cwd("root is /Users/ivan/.claude-tg-bot/sandbox", self.CWD)
        self.assertIn("/Users/ivan/.claude-tg-bot/sandbox", out)

    def test_no_match_passthrough(self):
        text = "no path here"
        self.assertEqual(_mask_cwd(text, self.CWD), text)

    def test_empty_cwd_noop(self):
        text = f"a {self.CWD}/x b"
        self.assertEqual(_mask_cwd(text, ""), text)

    def test_reply_masks_active_cwd_globally(self):
        # _reply hides the active working dir even when `cwd` is not passed: the
        # handlers set it via set_active_cwd for the message being processed.
        import claude_tg_bot.reply as rp
        rp.set_active_cwd(self.CWD)
        try:
            message = AsyncMock()
            message.reply_text = AsyncMock()
            asyncio.run(rp._reply(message, f"dir {self.CWD} and next"))
            sent = message.reply_text.await_args.args[0]
            self.assertNotIn(self.CWD, sent)
            self.assertIn("dir / and next", sent)
        finally:
            rp._active_cwd.set("")


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