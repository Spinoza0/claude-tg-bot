"""Tests for processing a sandbox command with no text but a replied message.

Replying to a message (e.g. a voice note) and sending just the sandbox trigger
("@leg") carries content via the reply context — the bot must process it, not
ignore it as an empty message.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock  # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import handlers  # noqa: E402


def _msg(text=None, caption=None, reply_to=None, from_user=None, mid=1, **attrs):
    m = SimpleNamespace(
        id=mid,
        text=text,
        caption=caption,
        from_user=from_user or SimpleNamespace(id=1),
        reply_to_message=reply_to,
        photo=None, video=None, video_note=None,
        audio=None, voice=None, document=None, animation=None,
        sticker=None, contact=None, venue=None, location=None, poll=None,
        chat=SimpleNamespace(id=99),
    )
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _st():
    st = MagicMock()
    st.project_root = "/tmp/proj"
    st.get_active_root.return_value = "/tmp/proj"
    return st


class TestSandboxEmptyReply(unittest.TestCase):
    def setUp(self):
        self._store = patch.object(handlers, "store")
        self._st = self._store.start()
        self._st.get.return_value = _st()
        self.addCleanup(self._store.stop)

    def test_on_chat_empty_text_with_reply_not_crash(self):
        """on_chat with an empty text must not IndexError on the /clear check."""
        message = _msg(text="", reply_to=_msg(text="hello"))
        started = []
        with patch.object(handlers, "_start_bg", lambda coro: started.append(coro)), \
             patch.object(handlers, "_run_and_reply", MagicMock(return_value=None)):
            asyncio.run(handlers.on_chat(SimpleNamespace(), message, "", sandbox=False))
        self.assertEqual(len(started), 1)

    def test_on_sandbox_empty_rest_with_reply_forwards_to_on_chat(self):
        """"@leg" (empty rest) replying to a message must be sent to on_chat, not ignored."""
        message = _msg(text="@leg", reply_to=_msg(text="voice/caption content"))
        on_chat = AsyncMock(return_value=None)
        with patch.object(handlers, "on_chat", on_chat), \
             patch.object(handlers, "_has_any_attach", return_value=False):
            asyncio.run(handlers.on_sandbox(SimpleNamespace(), message))
            on_chat.assert_called_once()
            self.assertEqual(on_chat.call_args.args[1].text, "@leg")

    def test_on_sandbox_empty_rest_no_reply_ignored(self):
        """"@leg" alone with nothing to reply to is ignored (no on_chat call)."""
        message = _msg(text="@leg")
        on_chat = AsyncMock(return_value=None)
        with patch.object(handlers, "on_chat", on_chat), \
             patch.object(handlers, "_has_any_attach", return_value=False):
            asyncio.run(handlers.on_sandbox(SimpleNamespace(), message))
            on_chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()