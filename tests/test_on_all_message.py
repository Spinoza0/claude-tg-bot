"""Tests for the top-level message filter (on_all_message): routing + access."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import handlers  # noqa: E402


def _msg(text=None, caption=None, photo=None, video=None, video_note=None,
         audio=None, voice=None, document=None, animation=None, sticker=None,
         poll=None, location=None, venue=None, contact=None, chat_id=99,
         outgoing=False, reply_to_message_id=None):
    m = SimpleNamespace(
        text=text, caption=caption,
        photo=photo, video=video, video_note=video_note, audio=audio, voice=voice,
        document=document, animation=animation, sticker=sticker,
        poll=poll, location=location, venue=venue, contact=contact,
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=1),
        outgoing=outgoing, reply_to_message_id=reply_to_message_id,
    )
    return m


class TestOnAllMessageRouting(unittest.TestCase):
    """Route the right incoming message to the right handler."""

    def _run(self, message, sandbox_msg=False, allowed=True, allowed_user=True,
             is_bot=False, has_attach=None, routed=None, **dispatch_mocks):
        with patch.object(handlers, "is_bot_message", return_value=is_bot), \
             patch.object(handlers, "_author", return_value=1), \
             patch.object(handlers, "_allowed", return_value=allowed), \
             patch.object(handlers, "_is_allowed_user", return_value=allowed_user), \
             patch.object(handlers, "_is_sandbox_message", return_value=sandbox_msg):
            mocks = {
                "on_sandbox": AsyncMock(return_value=None),
                "on_chat": AsyncMock(return_value=None),
                "on_photo": AsyncMock(return_value=None),
                "on_video": AsyncMock(return_value=None),
                "on_video_note": AsyncMock(return_value=None),
                "on_audio": AsyncMock(return_value=None),
                "on_document": AsyncMock(return_value=None),
                "on_sticker": AsyncMock(return_value=None),
                "on_command": AsyncMock(return_value=None),
                "_handle_textual_attach": AsyncMock(return_value=None),
                "_on_unknown_attach": AsyncMock(return_value=None),
            }
            mocks.update(dispatch_mocks)
            with patch.multiple(handlers, **{k: v for k, v in mocks.items()}):
                asyncio.run(handlers.on_all_message(None, message))
            if routed is not None:
                for name in routed:
                    mocks[name].assert_called_once()
        return mocks

    def test_bot_own_message_ignored(self):
        mocks = self._run(_msg(text="hi"), is_bot=True)
        for m in mocks.values():
            m.assert_not_called()

    def test_sandbox_mention_routes_to_on_sandbox(self):
        self._run(_msg(text="@leg"), sandbox_msg=True, routed=["on_sandbox"])

    def test_sandbox_mention_denied_user_ignored(self):
        mocks = self._run(_msg(text="@leg"), sandbox_msg=True, allowed_user=False)
        mocks["on_sandbox"].assert_not_called()

    def test_photo_routes(self):
        self._run(_msg(photo=SimpleNamespace()), routed=["on_photo"])

    def test_video_routes(self):
        self._run(_msg(video=SimpleNamespace()), routed=["on_video"])

    def test_video_note_routes(self):
        self._run(_msg(video_note=SimpleNamespace()), routed=["on_video_note"])

    def test_sticker_routes(self):
        self._run(_msg(sticker=SimpleNamespace()), routed=["on_sticker"])

    def test_audio_routes(self):
        self._run(_msg(audio=SimpleNamespace()), routed=["on_audio"])

    def test_document_routes(self):
        self._run(_msg(document=SimpleNamespace()), routed=["on_document"])

    def test_clear_routes_to_on_chat(self):
        self._run(_msg(text="/clear"), routed=["on_chat"])


if __name__ == "__main__":
    unittest.main()