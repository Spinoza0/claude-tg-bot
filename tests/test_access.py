"""Tests for access control (who may write to the bot and in which chats)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import access  # noqa: E402


def _msg(from_user=None, chat_id=99):
    return SimpleNamespace(
        from_user=from_user,
        chat=SimpleNamespace(id=chat_id),
    )


class TestAllowedUser(unittest.TestCase):
    def test_user_in_list_allowed(self):
        with mock.patch.object(access.config, "ALLOWED_USERS", {100, 200}):
            self.assertTrue(access._is_allowed_user(100))

    def test_user_not_in_list_denied(self):
        with mock.patch.object(access.config, "ALLOWED_USERS", {100}):
            self.assertFalse(access._is_allowed_user(300))


class TestAllowedChat(unittest.TestCase):
    def test_empty_list_allows_only_own_chat(self):
        # Empty ALLOWED_CHAT_IDS: only Saved Messages (chat_id == user_id).
        with mock.patch.object(access.config, "ALLOWED_CHAT_IDS", set()):
            self.assertTrue(access._is_allowed_chat(777, 777))
            self.assertFalse(access._is_allowed_chat(777, 888))

    def test_set_requires_chat_in_list(self):
        with mock.patch.object(access.config, "ALLOWED_CHAT_IDS", {-100123, -100456}):
            self.assertTrue(access._is_allowed_chat(1, -100123))
            self.assertFalse(access._is_allowed_chat(1, 999))


class TestAllowed(unittest.TestCase):
    def test_requires_both_user_and_chat(self):
        with mock.patch.object(access.config, "ALLOWED_USERS", {100}), \
             mock.patch.object(access.config, "ALLOWED_CHAT_IDS", {-1001}):
            self.assertTrue(access._allowed(100, -1001))
            self.assertFalse(access._allowed(100, 999))   # bad chat
            self.assertFalse(access._allowed(500, -1001))  # bad user


class TestAuthor(unittest.TestCase):
    def test_prefers_from_user(self):
        self.assertEqual(access._author(_msg(from_user=SimpleNamespace(id=42), chat_id=99)), 42)

    def test_falls_back_to_chat_when_no_from_user(self):
        # e.g. a channel message: no from_user, so the chat id is the author.
        self.assertEqual(access._author(_msg(from_user=None, chat_id=99)), 99)


if __name__ == "__main__":
    unittest.main()