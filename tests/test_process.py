"""Tests for _get_bot_pids and _try_create_lock (single-instance, issue #43)."""

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest import mock

from claude_tg_bot.process import _get_bot_pids, _try_create_lock


def _ps(payload: str):
    """Return a subprocess.run result object with the given ps output."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=payload)


class TestGetBotPids(unittest.TestCase):
    """The single-instance process scan must not mistake a shell for the bot."""

    def _scan(self, ps_output: str, my_pid: int = 1000):
        with mock.patch.object(os, "getpid", return_value=my_pid), \
                mock.patch("subprocess.run", return_value=_ps(ps_output)):
            return _get_bot_pids()

    def test_finds_real_bot(self):
        # `python -m claude_tg_bot` is the bot — must be found.
        out = "999  /usr/bin/python3 -m claude_tg_bot\n"
        self.assertEqual(self._scan(out, my_pid=1000), [999])

    def test_ignores_own_pid(self):
        out = "1000  /usr/bin/python3 -m claude_tg_bot\n"
        self.assertEqual(self._scan(out, my_pid=1000), [])

    def test_ignores_zsh_wrapper_inline(self):
        # A zsh -c that inlines the launch string is NOT the bot.
        out = ("999  /bin/zsh -c source ~/.claude/shell-snapshots/snapshot-zsh-1; "
               "python -m claude_tg_bot\n")
        self.assertEqual(self._scan(out), [])

    def test_ignores_bash_wrapper_inline(self):
        out = "999  /bin/bash -c 'python -m claude_tg_bot'\n"
        self.assertEqual(self._scan(out), [])

    def test_ignores_caffeinate_wrapper(self):
        # KEEP_AWAKE runs the bot via `caffeinate -dimsu python -m claude_tg_bot`;
        # caffeinate is a wrapper (first token != python) and must NOT be mistaken
        # for the bot, or the single-instance guard fires on itself.
        out = ("999  caffeinate -dimsu /p/.venv/bin/python -m claude_tg_bot\n")
        self.assertEqual(self._scan(out), [])

    def test_python_dash_c_with_module_matches(self):
        # Documented: a `python -c` whose code mentions the module is still
        # matched (it starts with python). Not ideal but unrelated to shell/
        # caffeinate wrappers; kept here so a change to drop it is intentional.
        out = "999  /usr/bin/python3 -c 'from claude_tg_bot import x'\n"
        self.assertEqual(self._scan(out), [999])


class TestTryCreateLock(unittest.TestCase):
    """The atomic lock must take over an orphaned lock, not block forever."""

    def test_orphaned_lock_taken_over(self, ):
        # An existing lock whose pid is DEAD must be removed and re-acquired.
        with NamedTemporaryFile("w+", delete=False) as f:
            f.write("999999999")  # a pid that surely doesn't exist
            lock_path = f.name
        with mock.patch("claude_tg_bot.process._LOCK_FILE", Path(lock_path)), \
                mock.patch.object(os, "getpid", return_value=1000):
            try:
                self.assertTrue(_try_create_lock())
                with open(lock_path) as f:
                    self.assertEqual(f.read().strip(), "1000")
            finally:
                os.path.exists(lock_path) and os.unlink(lock_path)

    def test_live_lock_blocks(self):
        # A lock owned by a LIVE pid must block (a real concurrent start).
        with NamedTemporaryFile("w+", delete=False) as f:
            f.write(str(os.getpid()))  # our own live pid
            lock_path = f.name
        with mock.patch("claude_tg_bot.process._LOCK_FILE", Path(lock_path)), \
                mock.patch.object(os, "getpid", return_value=1000):
            try:
                self.assertFalse(_try_create_lock())
            finally:
                os.path.exists(lock_path) and os.unlink(lock_path)


if __name__ == "__main__":
    unittest.main()