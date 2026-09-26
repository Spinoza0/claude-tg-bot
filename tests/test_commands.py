"""Tests for the slash-command handler (on_command)."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import commands  # noqa: E402
from claude_tg_bot.commands import on_command  # noqa: E402


def _st(active="/proj/p1", name="p1"):
    st = MagicMock()
    st.get_active_root.return_value = active
    st.active_name.return_value = name
    st.set_active = MagicMock()
    return st


class _FakeMsg:
    def __init__(self, text):
        self.text = text
        self.chat = SimpleNamespace(id=1)
        self.id = 10
        self.reply_text = AsyncMock()
        self.from_user = SimpleNamespace(id=777)


class TestOnCommand(unittest.TestCase):
    def setUp(self):
        self._store = patch.object(commands, "store")
        self.store = self._store.start()
        self.addCleanup(self._store.stop)
        self._reply = patch.object(commands, "_reply", AsyncMock(return_value=None))
        self.reply = self._reply.start()
        self.addCleanup(self._reply.stop)
        self._hint = patch.object(commands, "format_command_hint", return_value="hint")
        self._hint.start()
        self.addCleanup(self._hint.stop)
        self._sess = patch.object(commands, "find_latest_session", return_value="sess1")
        self._sess.start()
        self.addCleanup(self._sess.stop)
        self._kill = patch.object(commands, "kill_bot_procs", return_value=(2, []))
        self._kill.start()
        self.addCleanup(self._kill.stop)
        self._clear = patch.object(commands, "_clear_attach", AsyncMock(return_value=None))
        self.clear_mock = self._clear.start()
        self.addCleanup(self._clear.stop)
        self._size = patch.object(commands, "_attach_size", AsyncMock(return_value=None))
        self.size_mock = self._size.start()
        self.addCleanup(self._size.stop)
        self._pools = patch.object(commands, "_bot_proc_pids", [])
        self._pools.start()
        self.addCleanup(self._pools.stop)
        self._tasks = patch.object(commands, "_active_tasks", set())
        self._tasks.start()
        self.addCleanup(self._tasks.stop)

    def _run(self, text, sandbox=False, projects_root="/root", sandbox_root="/sroot"):
        root = sandbox_root if sandbox else projects_root
        with patch.object(commands.config, "PROJECTS_ROOT", Path(projects_root)), \
             patch.object(commands.config, "SANDBOX_ROOT", Path(sandbox_root)):
            st = _st(active=f"{root}/p1", name="p1")
            self.store.get_or_init.return_value = st
            self.store.get.return_value = st
            self.store.list_projects.return_value = [Path(f"{root}/p1")]
            asyncio.run(on_command(SimpleNamespace(), _FakeMsg(text), text, sandbox=sandbox))
        return st

    def test_help_without_project(self):
        self._run("/help")
        self.reply.assert_called_once()
        text = self.reply.call_args.args[1]
        self.assertIn("hint", text)

    def test_help_with_sandbox_mentions_mode(self):
        self._run("/help", sandbox=True)
        text = self.reply.call_args.args[1]
        self.assertIn("sandbox", text.lower())

    def test_list_empty(self):
        self.store.list_projects.return_value = []
        self._run("/list")
        self.reply.assert_called_once()

    def test_switch_valid(self):
        with patch.object(commands, "is_safe_project_name", return_value=True), \
             patch.object(Path, "is_dir", return_value=True):
            self._run("/switch p1")
        self.assertEqual(self.store.get_or_init.return_value.set_active.call_count, 1)
        self.reply.assert_called_once()

    def test_switch_invalid_name_rejected(self):
        with patch.object(commands, "is_safe_project_name", return_value=False):
            self._run("/switch bad/name")
        self.reply.assert_called_once()

    def test_switch_missing_dir(self):
        with patch.object(commands, "is_safe_project_name", return_value=True):
            with patch.object(Path, "is_dir", return_value=False):
                self._run("/switch nope")
        # set_active is NOT called when the dir is missing.
        self.store.get_or_init.return_value.set_active.assert_not_called()

    def test_new_creates_project(self):
        with patch.object(commands, "is_safe_project_name", return_value=True), \
             patch.object(Path, "mkdir", return_value=None):
            self._run("/new demo")
        st = self.store.get_or_init.return_value
        st.set_active.assert_called_once()

    def test_status(self):
        self._run("/status")
        self.reply.assert_called_once()
        text = self.reply.call_args.args[1]
        self.assertIn("sess1", text)

    def test_config_denied_in_sandbox(self):
        self._run("/config", sandbox=True)
        self.reply.assert_called_once()

    def test_clearattach_routes(self):
        self._run("/clearattach")
        self.clear_mock.assert_called_once()

    def test_attachsize_routes(self):
        self._run("/attachsize")
        self.size_mock.assert_called_once()

    def test_unknown_command_silent(self):
        self._run("/nope")
        self.reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()