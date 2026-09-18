"""Unit tests for the bot's pure logic (no Telegram/network).

We cover only deterministic functions: @helpbot parsing, attachment extension
detection, sending with retries, attachment routing. We import the needed
modules from the claude_tg_bot package.
"""

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import handlers, media, reply, sandbox  # noqa: E402


class TestAgentParsing(unittest.TestCase):
    """Parsing @helpbot messages."""

    @classmethod
    def setUpClass(cls):
        sandbox.SANDBOX_PREFIX = "@helpbot"

    def test_is_sandbox_message_true(self):
        self.assertTrue(sandbox._is_sandbox_message("@helpbot"))
        self.assertTrue(sandbox._is_sandbox_message("@helpbot exchange rate"))
        self.assertTrue(sandbox._is_sandbox_message("  @helpbot /status"))

    def test_is_sandbox_message_false(self):
        self.assertFalse(sandbox._is_sandbox_message("@helpbotxyz"))
        self.assertFalse(sandbox._is_sandbox_message("@helpbotfoo bar"))
        self.assertFalse(sandbox._is_sandbox_message("just text"))
        self.assertFalse(sandbox._is_sandbox_message("/status"))
        self.assertFalse(sandbox._is_sandbox_message("/agent"))
        self.assertFalse(sandbox._is_sandbox_message(""))

    def test_strip_sandbox_prefix(self):
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot rate"), "rate")
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot /status"), "/status")
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot   many spaces"), "many spaces")
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot\ttab"), "tab")
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot"), "")
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot   "), "")


class _FakeMedia:
    """Fake attachment object with file_name/mime_type."""

    def __init__(self, file_name=None, mime_type=None):
        self.file_name = file_name
        self.mime_type = mime_type


class TestMediaExt(unittest.TestCase):
    """Attachment extension detection."""

    def test_extension_from_filename(self):
        self.assertEqual(media._media_ext(_FakeMedia("clip.mp4", "video/mp4")), ".mp4")
        self.assertEqual(media._media_ext(_FakeMedia("pic.JPG", "image/jpeg")), ".jpg")
        self.assertEqual(media._media_ext(_FakeMedia("song.MP3", "audio/mpeg")), ".mp3")

    def test_extension_from_mime(self):
        self.assertEqual(media._media_ext(_FakeMedia(None, "video/webm")), ".webm")
        self.assertEqual(media._media_ext(_FakeMedia(None, "audio/ogg")), ".ogg")
        self.assertEqual(media._media_ext(_FakeMedia(None, "application/pdf")), ".pdf")

    def test_extension_default_bin(self):
        self.assertEqual(media._media_ext(_FakeMedia(None, None)), ".bin")

    def test_extension_filename_without_dot(self):
        self.assertEqual(media._media_ext(_FakeMedia("noext", "image/png")), ".png")


class TestSniffImageExt(unittest.TestCase):
    """Image extension detection by magic bytes."""

    def _write(self, data: bytes) -> Path:
        fd, name = tempfile.mkstemp(prefix="sniff_", suffix=".tmp")
        os.close(fd)
        p = Path(name)
        p.write_bytes(data)
        self.addCleanup(p.unlink, missing_ok=True)
        return p

    def test_png(self):
        p = self._write(b"\x89PNG\r\n\x1a\nrest")
        self.assertEqual(media._sniff_image_ext(p), ".png")

    def test_jpg(self):
        p = self._write(b"\xff\xd8\xff\xe0rest")
        self.assertEqual(media._sniff_image_ext(p), ".jpg")

    def test_webp(self):
        p = self._write(b"RIFFxxxxWEBPdata")
        self.assertEqual(media._sniff_image_ext(p), ".webp")

    def test_gif(self):
        p = self._write(b"GIF89a....")
        self.assertEqual(media._sniff_image_ext(p), ".gif")

    def test_unknown_fallback_jpg(self):
        p = self._write(b"not-an-image-at-all")
        self.assertEqual(media._sniff_image_ext(p), ".jpg")


class _FlakyMessage:
    """Message whose reply_text fails the first `fails` times."""

    def __init__(self, fails):
        self.fails = fails
        self.calls = 0
        self.replied = None

    async def reply_text(self, text):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("Telegram says: [500 INTERDC_X_CALL_ERROR] boom")
        self.replied = text
        return type("R", (), {"chat": type("C", (), {"id": 1})(), "id": 1})()


class TestSendWithRetry(unittest.TestCase):
    """Sending a reply with retries."""

    def test_success_after_transient_errors(self):
        m = _FlakyMessage(fails=2)
        res = asyncio.run(reply._send_with_retry(m, "answer", attempts=3, delay=0))
        self.assertIsNotNone(res)
        self.assertEqual(m.calls, 3)
        self.assertEqual(m.replied, "🤖 answer")

    def test_all_fail_returns_none(self):
        m = _FlakyMessage(fails=99)
        res = asyncio.run(reply._send_with_retry(m, "text", attempts=2, delay=0))
        self.assertIsNone(res)
        self.assertEqual(m.calls, 2)


class TestSplitRouting(unittest.TestCase):
    """Separate handlers (photo/video/audio/file) route correctly.

    on_photo should go to _handle_attachment with sniff_ext=True, while
    on_video/on_audio/on_document use their own extension from _media_ext. We
    verify this via delegation to _handle_attachment (mocked).
    """

    def _make_msg(self, photo=None, video=None, audio=None, voice=None, document=None):
        m = SimpleNamespace()
        m.photo, m.video, m.audio, m.voice, m.document = photo, video, audio, voice, document
        m.caption = ""
        return m

    def _mock_handle(self):
        calls = []
        async def mock(*a, **k):
            calls.append((a, k))
        handlers._handle_attachment = mock
        return calls

    def test_photo_uses_sniff(self):
        calls = self._mock_handle()
        m = self._make_msg(photo=_FakeMedia())
        asyncio.run(handlers.on_photo(None, m))
        self.assertEqual(len(calls), 1)
        _, kw = calls[0]
        self.assertEqual(kw["sniff_ext"], True)

    def test_video_ext(self):
        calls = self._mock_handle()
        m = self._make_msg(video=_FakeMedia("c.mp4", "video/mp4"))
        asyncio.run(handlers.on_video(None, m))
        self.assertEqual(calls[0][0][3], ".mp4")

    def test_audio_ext(self):
        calls = self._mock_handle()
        m = self._make_msg(audio=_FakeMedia("s.mp3", "audio/mpeg"))
        asyncio.run(handlers.on_audio(None, m))
        self.assertEqual(calls[0][0][3], ".mp3")

    def test_document_ext(self):
        calls = self._mock_handle()
        m = self._make_msg(document=_FakeMedia("doc.pdf", "application/pdf"))
        asyncio.run(handlers.on_document(None, m))
        self.assertEqual(calls[0][0][3], ".pdf")

    def test_video_note_as_video(self):
        calls = self._mock_handle()
        m = SimpleNamespace(video_note=_FakeMedia(None, "video/mp4"),
                            photo=None, video=None, audio=None, voice=None,
                            document=None, caption="")
        asyncio.run(handlers.on_video_note(None, m))
        self.assertEqual(calls[0][0][3], ".mp4")
        self.assertEqual(calls[0][0][2], "video message (circle)")


class TestTextualMediaPrompt(unittest.TestCase):
    """_textual_media_prompt builds a description for a poll/geo/contact."""

    def _msg(self, **kw):
        d = dict(photo=None, video=None, video_note=None, audio=None, voice=None,
                 document=None, animation=None, sticker=None, contact=None,
                 location=None, venue=None, poll=None, dice=None, game=None,
                 web_app_data=None, paid_media=None)
        d.update(kw)
        return SimpleNamespace(**d)

    def _poll(self, question, options):
        return SimpleNamespace(question=SimpleNamespace(text=question),
                               options=[SimpleNamespace(text=o) for o in options])

    def test_poll(self):
        p = self._poll("How are you?", ["Great", "Fine"])
        prompt = media._textual_media_prompt(self._msg(poll=p))
        self.assertIn("How are you?", prompt)
        self.assertIn("Great", prompt)
        self.assertIn("Fine", prompt)

    def test_contact(self):
        c = SimpleNamespace(first_name="John", last_name="Doe", phone_number="+12345678900")
        prompt = media._textual_media_prompt(self._msg(contact=c))
        self.assertIn("John", prompt)
        self.assertIn("12345678900", prompt)

    def test_location(self):
        loc = SimpleNamespace(latitude=55.75, longitude=37.61)
        prompt = media._textual_media_prompt(self._msg(location=loc))
        self.assertIn("55.75", prompt)

    def test_none_for_other(self):
        self.assertIsNone(media._textual_media_prompt(self._msg(photo=object())))


class TestStickerAsFile(unittest.TestCase):
    """A sticker is sent as a file attachment to claude (not "can't process")."""

    def test_sticker_routes_to_attachment(self):
        calls = []
        async def mock(*a, **k):
            calls.append((a, k))
        handlers._handle_attachment = mock
        class St:
            file_name = "sticker.webp"
            mime_type = "image/webp"
        m = SimpleNamespace(sticker=St(), photo=None, video=None, video_note=None,
                            audio=None, voice=None, document=None, animation=None,
                            caption="")
        asyncio.run(handlers.on_sticker(None, m))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][3], ".webp")


if __name__ == "__main__":
    unittest.main()