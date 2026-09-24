"""Unit tests for cleaning downloaded attachments and deleting to trash/forever.

We check: _env_bool (boolean setting parsing), _fmt_bytes (volume format),
_dir_size, and /clearattach (_clear_attach) — file/volume counting and deletion by
DELETE_MODE (trash or forever, via a _delete_path mock).
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Force English so the assertions on localized strings are deterministic and
# don't depend on the developer's real config.env (which may set BOT_LANG=ru).
os.environ["BOT_LANG"] = "en"

from claude_tg_bot import config  # noqa: E402
from claude_tg_bot import commands, handlers, attach  # noqa: E402


class TestEnvBool(unittest.TestCase):
    """_env_bool converts env strings to a bool."""

    def test_true_values(self):
        for v in ("1", "true", "yes", "on"):
            os.environ["TEST_BOOL"] = v
            self.assertTrue(config._env_bool("TEST_BOOL", False))
        for v in ("0", "false", "no", "off", ""):
            os.environ["TEST_BOOL"] = v
            self.assertFalse(config._env_bool("TEST_BOOL", False))
        os.environ.pop("TEST_BOOL", None)

    def test_default_when_empty(self):
        self.assertTrue(config._env_bool("NONEXISTENT_VAR_XYZ", True))
        self.assertFalse(config._env_bool("NONEXISTENT_VAR_XYZ", False))


class TestFmtBytes(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(attach._fmt_bytes(512), "512 B")

    def test_kb(self):
        self.assertEqual(attach._fmt_bytes(5 * 1024), "5.0 KB")

    def test_mb(self):
        self.assertEqual(attach._fmt_bytes(int(1.3 * 1024 * 1024)), "1.3 MB")

    def test_gb(self):
        self.assertEqual(attach._fmt_bytes(int(2.1 * 1024**3)), "2.1 GB")


class TestDirSize(unittest.TestCase):
    def test_sum(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.bin").write_bytes(b"x" * 100)
            sub = d / "sub"
            sub.mkdir()
            (sub / "b.bin").write_bytes(b"y" * 50)
            self.assertEqual(attach._dir_size(d), 150)


class TestClearAttach(unittest.TestCase):
    """_clear_attach: counts files/volume and deletes by DELETE_MODE."""

    def test_delete_to_trash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            attach_dir = root / ".claude_tg_bot_attach"
            attach_dir.mkdir()
            (attach_dir / "a.jpg").write_bytes(b"x" * 1024)
            (attach_dir / "b.mp4").write_bytes(b"y" * 2048)

            deleted = []
            commands._delete_path = lambda p, mode="": deleted.append((p, mode))
            commands.config.DELETE_MODE = "trash"

            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            commands._reply = fake_reply

            asyncio.run(commands._clear_attach(None, str(root), sandbox=False, root=str(root)))

            self.assertEqual(len(deleted), 1)
            target, mode = deleted[0]
            self.assertEqual(target, attach_dir)
            self.assertEqual(mode, "trash")
            self.assertIn("2", seen["msg"])
            self.assertIn("to trash", seen["msg"])

    def test_silent_no_dir_no_reply(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            commands._reply = fake_reply
            asyncio.run(commands._clear_attach(None, str(root), sandbox=False, root=str(root), silent=True))
            self.assertNotIn("msg", seen)

    def test_silent_empty_dir_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            attach_dir = root / ".claude_tg_bot_attach"
            attach_dir.mkdir()
            deleted = []
            commands._delete_path = lambda p, mode="": deleted.append((p, mode))
            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            commands._reply = fake_reply
            asyncio.run(commands._clear_attach(None, str(root), sandbox=False, root=str(root), silent=True))
            self.assertEqual(len(deleted), 1)
            self.assertEqual(deleted[0][0], attach_dir)
            self.assertIn("Empty", seen["msg"])


class TestAttachSize(unittest.TestCase):
    """_attach_size: counts files/volume of downloaded attachments, deletes nothing."""

    def test_reports_count_and_size(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            attach_dir = root / ".claude_tg_bot_attach"
            attach_dir.mkdir()
            (attach_dir / "a.jpg").write_bytes(b"x" * 1024)
            (attach_dir / "b.mp4").write_bytes(b"y" * 2048)

            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            commands._reply = fake_reply

            asyncio.run(commands._attach_size(None, str(root)))

            self.assertIn("2", seen["msg"])
            self.assertIn("3.0 KB", seen["msg"])
            self.assertNotIn("Deleted", seen["msg"])

    def test_empty_reports_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            commands._reply = fake_reply
            asyncio.run(commands._attach_size(None, str(root)))
            self.assertIn("No downloaded attachments", seen["msg"])


class TestClearAttachRouting(unittest.TestCase):
    """/clearattach must not route into /clear (prefix conflict).

    Previously the check was text.startswith('/clear') — it caught `/clearattach`
    too, which also starts with `/clear`, and routed it into on_chat (the claude
    prompt) instead of on_command. That produced "Unknown command". We verify the
    routing is now exact.
    """

    def _msg(self, text):
        return SimpleNamespace(
            text=text, caption=None, chat=SimpleNamespace(id=123),
            photo=None, video=None, audio=None, voice=None, document=None,
            outgoing=False, reply_to_message_id=None,
        )

    def test_routing(self):
        handlers._allowed = lambda uid, cid: True
        handlers._is_allowed_user = lambda uid: True
        handlers._is_sandbox_message = lambda t: False
        handlers._author = lambda m: 777
        routed = []
        async def fake_on_command(client, message, text, sandbox=False): routed.append(("command", text))
        async def fake_on_chat(client, message, text, sandbox=False): routed.append(("chat", text))
        handlers.on_command = fake_on_command
        handlers.on_chat = fake_on_chat
        asyncio.run(handlers.on_all_message(None, self._msg("/clearattach")))
        asyncio.run(handlers.on_all_message(None, self._msg("/clear")))
        self.assertEqual(routed[0], ("command", "/clearattach"))
        self.assertEqual(routed[1], ("chat", "/clear"))


if __name__ == "__main__":
    unittest.main()