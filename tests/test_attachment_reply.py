"""Tests for sending result attachments back and the marker parsing (issue: attachments in replies)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from claude_tg_bot.reply import _attachment_kind, _send_attachment
from claude_tg_bot.runner import extract_file_markers
from claude_tg_bot.handlers import _resolve_attachment_path


class TestExtractFileMarkers(unittest.TestCase):
    def test_no_markers(self):
        self.assertEqual(extract_file_markers("just text"), ([], "just text"))

    def test_single_marker(self):
        paths, text = extract_file_markers("Done.\n[FILE: img.png]")
        self.assertEqual(paths, ["img.png"])
        self.assertEqual(text, "Done.")

    def test_multiple_markers(self):
        paths, text = extract_file_markers(
            "[FILE: a.png]\ntext\n[FILE: .claude_tg_bot_attach/b.mp4]"
        )
        self.assertEqual(paths, ["a.png", ".claude_tg_bot_attach/b.mp4"])
        self.assertNotIn("[FILE:", text)
        self.assertIn("text", text)

    def test_marker_with_spaces_and_path(self):
        paths, text = extract_file_markers("result: [FILE: .claude_tg_bot_attach/out.png]")
        self.assertEqual(paths, [".claude_tg_bot_attach/out.png"])
        self.assertEqual(text, "result:")

    def test_empty_text(self):
        self.assertEqual(extract_file_markers(""), ([], ""))

    def test_marker_keeps_surrounding_text(self):
        paths, text = extract_file_markers("Here it is [FILE: x.png] thanks")
        self.assertEqual(paths, ["x.png"])
        self.assertIn("Here it is", text)
        self.assertIn("thanks", text)


class TestAttachmentKind(unittest.TestCase):
    def test_image(self):
        self.assertEqual(_attachment_kind("a.png"), "photo")
        self.assertEqual(_attachment_kind("a.JPG"), "photo")

    def test_video(self):
        self.assertEqual(_attachment_kind("a.mp4"), "video")
        self.assertEqual(_attachment_kind("a.mov"), "video")

    def test_audio(self):
        self.assertEqual(_attachment_kind("a.mp3"), "audio")
        self.assertEqual(_attachment_kind("a.ogg"), "audio")

    def test_document_fallback(self):
        self.assertEqual(_attachment_kind("a.pdf"), "document")
        self.assertEqual(_attachment_kind("a.zip"), "document")
        self.assertEqual(_attachment_kind("a.txt"), "document")
        self.assertEqual(_attachment_kind("no_ext"), "document")


class TestSendAttachment(unittest.TestCase):
    def _message(self):
        msg = mock.Mock()
        msg.chat.id = 123
        return msg

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_photo_method(self):
        client = mock.AsyncMock()
        path = Path("/tmp/a.png")
        self.assertIsNone(self._run(_send_attachment(client, self._message(), path)))
        client.send_photo.assert_awaited_once_with(123, str(path))

    def test_video_method(self):
        client = mock.AsyncMock()
        path = Path("/tmp/a.mp4")
        self.assertIsNone(self._run(_send_attachment(client, self._message(), path)))
        client.send_video.assert_awaited_once_with(123, str(path))

    def test_audio_method(self):
        client = mock.AsyncMock()
        path = Path("/tmp/a.mp3")
        self.assertIsNone(self._run(_send_attachment(client, self._message(), path)))
        client.send_audio.assert_awaited_once_with(123, str(path))

    def test_document_method(self):
        client = mock.AsyncMock()
        path = Path("/tmp/a.pdf")
        self.assertIsNone(self._run(_send_attachment(client, self._message(), path)))
        client.send_document.assert_awaited_once_with(123, str(path))

    def test_failure_returns_error_string(self):
        # When every retry fails _run_with_retry returns None -> an error string.
        client = mock.AsyncMock()
        path = Path("/tmp/a.png")
        with mock.patch("claude_tg_bot.reply._run_with_retry", new=mock.AsyncMock(return_value=None)):
            err = self._run(_send_attachment(client, self._message(), path))
        self.assertIsInstance(err, str)
        self.assertTrue(err)


class TestResolveAttachmentPath(unittest.TestCase):
    def test_relative_resolves_inside(self):
        with TemporaryDirectory() as d:
            project = Path(d)
            self.assertEqual(
                _resolve_attachment_path(project, "img.png"),
                (project / "img.png").resolve(),
            )

    def test_attach_subfolder(self):
        with TemporaryDirectory() as d:
            project = Path(d)
            self.assertEqual(
                _resolve_attachment_path(project, ".claude_tg_bot_attach/a.png"),
                (project / ".claude_tg_bot_attach" / "a.png").resolve(),
            )

    def test_absolute_rejected(self):
        with TemporaryDirectory() as d:
            project = Path(d)
            self.assertIsNone(_resolve_attachment_path(project, "/etc/passwd"))

    def test_parent_escape_rejected(self):
        with TemporaryDirectory() as d:
            project = Path(d)
            self.assertIsNone(_resolve_attachment_path(project, "../outside.txt"))

    def test_empty_rejected(self):
        with TemporaryDirectory() as d:
            self.assertIsNone(_resolve_attachment_path(Path(d), ""))


if __name__ == "__main__":
    unittest.main()