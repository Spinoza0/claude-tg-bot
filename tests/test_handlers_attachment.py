"""Tests for the common attachment handler (_handle_attachment) and routing."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import handlers  # noqa: E402


def _msg(caption=None, photo=None, audio=None, voice=None, video=None,
         video_note=None, document=None, animation=None, sticker=None):
    m = SimpleNamespace(
        caption=caption,
        photo=photo, audio=audio, voice=voice, video=video,
        video_note=video_note, document=document, animation=animation, sticker=sticker,
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=99),
    )
    m.download = AsyncMock(return_value="/tmp/dummy")
    m.unlink = lambda *a, **k: None
    return m


def _st():
    st = MagicMock()
    st.get_active_root.return_value = "/tmp/proj"
    return st


def _run_finished(coro):
    """Run a coroutine and drain the background task it may start."""
    return asyncio.run(coro)


class TestHandleAttachment(unittest.TestCase):
    def setUp(self):
        self._author = patch.object(handlers, "_author", return_value=1)
        self._author.start()
        self.addCleanup(self._author.stop)
        self._store = patch.object(handlers, "store")
        self.store = self._store.start()
        self.store.get.return_value = _st()
        self.addCleanup(self._store.stop)
        self._reply = patch.object(handlers, "_reply", AsyncMock(return_value=None))
        self.reply = self._reply.start()
        self.addCleanup(self._reply.stop)
        self._sniff = patch.object(handlers, "_sniff_image_ext", return_value=".png")
        self._sniff.start()
        self.addCleanup(self._sniff.stop)
        self._bg = patch.object(handlers, "_start_bg", lambda coro: coro)
        self._bg.start()
        self.addCleanup(self._bg.stop)
        self._run = patch.object(handlers, "_run_and_reply", AsyncMock(return_value=None))
        self.run_mock = self._run.start()
        self.addCleanup(self._run.stop)
        self._cmd = patch.object(handlers, "on_command", AsyncMock(return_value=None))
        self.cmd = self._cmd.start()
        self.addCleanup(self._cmd.stop)

    def test_no_project_replies_select(self):
        with patch.object(handlers, "store") as st_mock:
            st_mock.get.return_value = None
            asyncio.run(handlers._handle_attachment(None, _msg(), "photo", ".jpg"))
            self.reply.assert_called_once()

    def test_download_failure_replies(self):
        msg = _msg()
        msg.download = AsyncMock(side_effect=Exception("net down"))
        asyncio.run(handlers._handle_attachment(None, msg, "photo", ".jpg"))
        self.reply.assert_called_once()

    def test_caption_command_routes_to_on_command(self):
        msg = _msg(caption="/status")
        asyncio.run(handlers._handle_attachment(None, msg, "photo", ".jpg"))
        self.cmd.assert_called_once()

    def test_normal_caption_sends_to_claude(self):
        msg = _msg(caption="describe this")
        asyncio.run(handlers._handle_attachment(None, msg, "photo", ".jpg", sniff_ext=True))
        self.run_mock.assert_called_once()

    def test_sniff_renames_to_real_ext(self):
        msg = _msg(caption="x")
        asyncio.run(handlers._handle_attachment(None, msg, "photo", ".img", sniff_ext=True))
        # The downloaded path should have been renamed to the sniffed extension.
        self.run_mock.assert_called_once()


class TestRouting(unittest.TestCase):
    """Each on_* handler must early-return when its attachment field is absent."""

    def test_on_photo_none_returns(self):
        with patch.object(handlers, "_handle_attachment", AsyncMock()) as h:
            asyncio.run(handlers.on_photo(None, _msg()))
            h.assert_not_called()

    def test_on_audio_none_returns(self):
        with patch.object(handlers, "_handle_attachment", AsyncMock()) as h:
            asyncio.run(handlers.on_audio(None, _msg()))
            h.assert_not_called()

    def test_on_video_none_returns(self):
        with patch.object(handlers, "_handle_attachment", AsyncMock()) as h:
            asyncio.run(handlers.on_video(None, _msg()))
            h.assert_not_called()

    def test_on_video_note_none_returns(self):
        with patch.object(handlers, "_handle_attachment", AsyncMock()) as h:
            asyncio.run(handlers.on_video_note(None, _msg()))
            h.assert_not_called()

    def test_on_document_none_returns(self):
        with patch.object(handlers, "_handle_attachment", AsyncMock()) as h:
            asyncio.run(handlers.on_document(None, _msg()))
            h.assert_not_called()

    def test_on_sticker_none_returns(self):
        with patch.object(handlers, "_handle_attachment", AsyncMock()) as h:
            asyncio.run(handlers.on_sticker(None, _msg()))
            h.assert_not_called()

    def test_on_photo_present_dispatches(self):
        msg = _msg(photo=SimpleNamespace())
        with patch.object(handlers, "_handle_attachment", AsyncMock()) as h:
            asyncio.run(handlers.on_photo(None, msg))
            h.assert_called_once()


if __name__ == "__main__":
    unittest.main()