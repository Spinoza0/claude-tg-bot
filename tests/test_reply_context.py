"""Unit tests for the reply quote context (feed quoted content to Claude)."""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot.reply_context import (  # noqa: E402
    MAX_QUOTE_DEPTH,
    QUOTE_TEXT_MAX_LEN,
    _type_kind,
    collect_reply_context,
)


def _user(uid=1, first="Alice", last=None, username=None):
    return SimpleNamespace(id=uid, first_name=first, last_name=last, username=username)


def _msg(text=None, caption=None, from_user=None, reply_to=None, doc=None, mid=1):
    m = SimpleNamespace(
        id=mid,
        text=text,
        caption=caption,
        from_user=from_user,
        reply_to_message=reply_to,
        document=doc or None,
        photo=None, video=None, video_note=None,
        audio=None, voice=None, animation=None, sticker=None,
        contact=None, venue=None, location=None, poll=None, dice=None, game=None,
        web_app_data=None, paid_media=None,
        chat=SimpleNamespace(id=99),
    )
    if doc is not None:
        m.document = doc
    return m


class TestTypeKind(unittest.TestCase):
    def test_text(self):
        self.assertEqual(_type_kind(_msg(text="hi")), "text")

    def test_caption(self):
        self.assertEqual(_type_kind(_msg(caption="cap")), "text")

    def test_document(self):
        # The type name is localized; just assert it's recognized (not None).
        self.assertIsNotNone(_type_kind(_msg(doc=SimpleNamespace(file_name="a.pdf", mime_type="application/pdf"))))

    def test_empty(self):
        self.assertIsNone(_type_kind(_msg()))


class TestCollectReplyContext(unittest.TestCase):
    def test_no_reply(self):
        with TemporaryDirectory() as d:
            rc = self._run(_msg(text="hello"), Path(d))
        self.assertFalse(rc.has_reply)
        self.assertEqual(rc.text_block, "")
        self.assertEqual(rc.image_paths, [])

    def test_text_reply(self):
        quoted = _msg(text="Original message", from_user=_user(username="bob"), mid=2)
        msg = _msg(text="my reply", reply_to=quoted, from_user=_user())
        with TemporaryDirectory() as d:
            rc = self._run(msg, Path(d))
        self.assertTrue(rc.has_reply)
        # Header is localized — just check the leading bracket and the content.
        self.assertTrue(rc.text_block.startswith("["))
        self.assertIn("Original message", rc.text_block)
        self.assertIn("Alice", rc.text_block)
        self.assertEqual(rc.image_paths, [])

    def test_chain_reply_on_reply(self):
        inner = _msg(text="inner", from_user=_user(username="a"), mid=3)
        outer = _msg(text="middle", reply_to=inner, from_user=_user(username="b"), mid=2)
        msg = _msg(text="top", reply_to=outer, from_user=_user(), mid=1)
        with TemporaryDirectory() as d:
            rc = self._run(msg, Path(d))
        self.assertTrue(rc.has_reply)
        self.assertIn("middle", rc.text_block)
        self.assertIn("inner", rc.text_block)

    def test_depth_cap(self):
        # Build a chain longer than MAX_QUOTE_DEPTH, then check we stop at the cap.
        tail = _msg(text="bottom", from_user=_user(), mid=0)
        top = tail
        for i in range(1, MAX_QUOTE_DEPTH + 5):
            top = _msg(text=f"lvl{i}", reply_to=top, from_user=_user(), mid=i)
        msg = _msg(text="user", reply_to=top, from_user=_user(), mid=1000)
        with TemporaryDirectory() as d:
            rc = self._run(msg, Path(d))
        self.assertTrue(rc.has_reply)
        # At most MAX_QUOTE_DEPTH quotes collected (not the whole 25-level chain).
        # Exclude the header line (it also starts with '[').
        quote_lines = [l for l in rc.text_block.splitlines()[1:] if l.startswith("[")]
        self.assertLessEqual(len(quote_lines), MAX_QUOTE_DEPTH)

    def test_truncation(self):
        long_text = "x" * (QUOTE_TEXT_MAX_LEN + 1000)
        quoted = _msg(text=long_text, from_user=_user(), mid=2)
        msg = _msg(text="q", reply_to=quoted, from_user=_user())
        with TemporaryDirectory() as d:
            rc = self._run(msg, Path(d))
        self.assertTrue(rc.text_block)
        self.assertIn("…", rc.text_block)

    def _run(self, message, cwd):
        with mock.patch("claude_tg_bot.reply_context._sniff_image_ext", return_value=".img"):
            # Use asyncio to run the async collector.
            import asyncio
            return asyncio.run(collect_reply_context(message, cwd))


if __name__ == "__main__":
    unittest.main()