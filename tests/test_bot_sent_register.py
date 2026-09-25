"""Unit tests for the bot-sent message register (anti-loop filter).

We check that sent ids are remembered, is_bot_message flags them, and the TTL
prune drops stale ids so the register doesn't grow forever.
"""

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import reply  # noqa: E402


SM = SimpleNamespace


def _msg(mid):
    return SM(id=mid)


class TestSentRegister(unittest.TestCase):
    def setUp(self):
        reply._BOT_SENT.clear()

    def test_register_and_flag(self):
        m = _msg(42)
        reply.register_sent(m)
        self.assertTrue(reply.is_bot_message(m))
        self.assertFalse(reply.is_bot_message(_msg(43)))

    def test_no_id_ignored(self):
        reply.register_sent(SM(id=None))
        self.assertFalse(reply.is_bot_message(_msg(1)))

    def test_ttl_prune(self):
        m = _msg(7)
        reply.register_sent(m)
        # Age the entry beyond the TTL.
        reply._BOT_SENT[7] = time.time() - reply._SENT_TTL - 10
        reply.register_sent(_msg(8))  # this triggers the prune
        self.assertNotIn(7, reply._BOT_SENT)
        self.assertIn(8, reply._BOT_SENT)

    def test_fresh_entry_kept(self):
        m = _msg(9)
        reply.register_sent(m)
        reply.register_sent(_msg(10))
        self.assertIn(9, reply._BOT_SENT)


if __name__ == "__main__":
    unittest.main()