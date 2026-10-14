"""Unit tests for config.env migrations (claude_tg_bot/migrations.py)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import claude_tg_bot.migrations as mig  # noqa: E402


class TestMigrations(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env_file = self.dir / "config.env"

    def _write(self, text):
        self.env_file.write_text(text, encoding="utf-8")

    def _read(self):
        return self.env_file.read_text(encoding="utf-8")

    def test_renames_mt_proxy_to_proxy(self):
        self._write("API_ID=1\nMT_PROXY=tg://proxy?server=x&secret=00\n")
        mig.run_migrations(self.env_file)
        out = self._read()
        self.assertIn("PROXY=tg://proxy?server=x&secret=00", out)
        self.assertNotIn("MT_PROXY=", out)

    def test_noop_when_no_legacy_key(self):
        self._write("PROXY=tg://webproxy?server=x&secret=00\n")
        mig.run_migrations(self.env_file)
        self.assertEqual(self._read(), "PROXY=tg://webproxy?server=x&secret=00\n")

    def test_runs_once(self):
        self._write("MT_PROXY=tg://proxy?server=x&secret=00\n")
        mig.run_migrations(self.env_file)
        first = self._read()
        mig.run_migrations(self.env_file)
        second = self._read()
        self.assertEqual(first, second)

    def test_missing_file_is_noop(self):
        mig.run_migrations(self.dir / "nope.env")
        # nothing raised, nothing written

    def test_promote_env_legacy(self):
        with mock.patch.dict(os.environ, {"MT_PROXY": "tg://proxy?server=x&secret=00"},
                             clear=False):
            os.environ.pop("PROXY", None)
            mig.promote_env_legacy()
            self.assertEqual(os.getenv("PROXY"), "tg://proxy?server=x&secret=00")
            self.assertNotIn("MT_PROXY", os.environ)


if __name__ == "__main__":
    unittest.main()