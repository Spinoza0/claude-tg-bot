"""Unit tests for sessions.py: user state and project-name validation."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import sessions as _s  # noqa: E402
from claude_tg_bot.sessions import (  # noqa: E402
    UserState,
    claude_project_dir,
    find_latest_session,
    has_session,
    is_safe_project_name,
)
from claude_tg_bot import handlers as _handlers_mod  # noqa: E402

# Snapshot the original on_chat/on_command at import time: another test in the
# suite rewrites handlers.on_chat without restoring it, which would leak into
# this module. Keep the pristine versions to pin back in setUp.
_ORIG_ON_CHAT = _handlers_mod.on_chat
_ORIG_ON_COMMAND = _handlers_mod.on_command


class TestSandboxRequiresProject(unittest.TestCase):
    """on_chat must require a chosen project in sandbox mode too, like the regular mode."""

    def setUp(self):
        import asyncio
        from unittest import mock
        from claude_tg_bot import handlers

        self._asyncio = asyncio
        self._handlers = handlers

        # A bare message carrying only the sender id that _author() reads.
        self.message = mock.Mock()
        self.message.from_user.id = 123
        self.message.text = "пришли первый кадр"
        self.message.caption = None

        # Swap handlers' dependencies directly (preserving the originals) so
        # on_chat is isolated; restored in tearDown. Some other tests reassign
        # handlers.on_chat without restoring, so we pin it back to the real one
        # (snapshotted at import time, before any test mutated it).
        self._saved = {
            "store": handlers.store,
            "_reply": handlers._reply,
            "_start_bg": handlers._start_bg,
            "_author": handlers._author,
            "on_chat": handlers.on_chat,
            "on_command": handlers.on_command,
        }
        handlers.on_chat = _ORIG_ON_CHAT
        handlers.on_command = _ORIG_ON_COMMAND
        handlers.store = mock.MagicMock()
        handlers._reply = mock.AsyncMock()
        handlers._start_bg = mock.MagicMock()
        handlers._author = lambda m: 123
        self._state = UserState(user_id=123)
        handlers.store.get.return_value = self._state

    def tearDown(self):
        for attr, val in self._saved.items():
            setattr(self._handlers, attr, val)

    def test_sandbox_without_project_replies_and_skips_launch(self):
        response = self._asyncio.run(
            self._handlers.on_chat(None, self.message, "пришли первый кадр", sandbox=True)
        )
        self.assertIsNone(response)
        self._handlers._reply.assert_awaited_once()
        self._handlers._start_bg.assert_not_called()

    def test_sandbox_with_project_launches(self):
        self._state.set_active(sandbox=True, root="/sandbox/proj", name="proj")
        self._asyncio.run(self._handlers.on_chat(None, self.message, "привет", sandbox=True))
        self._handlers._start_bg.assert_called_once()
        # _start_bg is swapped out, so the _run_and_reply coroutine it received is
        # never awaited — close it to avoid an "unawaited coroutine" warning.
        self._handlers._start_bg.call_args[0][0].close()


class TestUserState(unittest.TestCase):
    """Round-trip serialization and separate normal/sandbox mode state."""

    def test_round_trip(self):
        st = UserState(
            user_id=123,
            project_root="/p/one",
            project_name="one",
            sandbox_project_root="/sandbox/foo",
            sandbox_project_name="foo",
        )
        st.last_cwd = "/p/one"
        restored = UserState.from_dict(st.to_dict())
        self.assertEqual(restored.user_id, 123)
        self.assertEqual(restored.project_root, "/p/one")
        self.assertEqual(restored.sandbox_project_root, "/sandbox/foo")
        self.assertEqual(restored.last_cwd, "/p/one")

    def test_separate_sandbox_and_normal_state(self):
        st = UserState(user_id=1)
        st.set_active(sandbox=False, root="/normal/one", name="one")
        st.set_active(sandbox=True, root="/sandbox/foo", name="foo")
        self.assertEqual(st.get_active_root(sandbox=False), "/normal/one")
        self.assertEqual(st.get_active_root(sandbox=True), "/sandbox/foo")
        self.assertEqual(st.active_name(sandbox=False), "one")
        self.assertEqual(st.active_name(sandbox=True), "foo")


class TestHasSession(unittest.TestCase):
    """has_session: whether a session exists in a directory (for --continue by directory)."""

    def test_empty_dir_false(self):
        self.assertFalse(has_session(Path("/nonexistent/xyz-123")))

    def test_some_dir_returns_bool(self):
        r = has_session(PROJECT_ROOT)
        self.assertIsInstance(r, bool)

    def test_slug_replaces_dot_with_dash(self):
        # Claude builds a slug from the absolute path, replacing '/' and '.' with '-'.
        # E.g. /Users/john.doe/... -> -Users-john-doe-... (the dot in the username
        # becomes a dash; previously it was '_', so has_session looked in the wrong
        # directory and --continue didn't trigger).
        d = claude_project_dir(Path("/Users/john.doe/StudioProjects/x"))
        self.assertEqual(
            d.name,
            "-Users-john-doe-StudioProjects-x",
        )


class TestFindLatestSession(unittest.TestCase):
    """find_latest_session: returns the newest .jsonl (for --resume)."""

    def test_returns_none_when_empty(self):
        self.assertIsNone(find_latest_session(Path("/nonexistent/xyz-123")))

    def test_returns_newest_by_mtime(self, ):
        import tempfile
        import os
        import time as _t
        d = claude_project_dir(PROJECT_ROOT)
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "old.jsonl").touch()
            (tmp / "new.jsonl").touch()
            _t.sleep(0.01)  # so the mtimes differ
            os.utime(tmp / "new.jsonl", None)
            orig = _s.claude_project_dir
            _s.claude_project_dir = lambda cwd: tmp
            try:
                self.assertEqual(find_latest_session(PROJECT_ROOT), "new")
            finally:
                _s.claude_project_dir = orig


class TestIsSafeProjectName(unittest.TestCase):
    """Protection against path traversal via the project name."""

    def test_valid(self):
        for name in ("project", "my-project", "my_project", "v1.0", "A1_b2"):
            self.assertTrue(is_safe_project_name(name), name)

    def test_invalid(self):
        for name in ("../evil", "a/b", "..", ".", "", "a b", "a\\b", "a$b", "привет"):
            self.assertFalse(is_safe_project_name(name), name)


if __name__ == "__main__":
    unittest.main()