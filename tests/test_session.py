"""Юнит-тесты session.py: состояние пользователя и валидация имени проекта."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from session import UserState, is_safe_project_name  # noqa: E402


class TestUserState(unittest.TestCase):
    """Круговая сериализация и раздельное состояние обычного/агентского режимов."""

    def test_round_trip(self):
        st = UserState(
            user_id=123,
            project_root="/p/one",
            project_name="one",
            agent_project_root="/agent/foo",
            agent_project_name="foo",
        )
        st.set_session("/p/one", "sess-1")
        st.set_session("/agent/foo", "sess-agent")
        st.last_cwd = "/p/one"
        restored = UserState.from_dict(st.to_dict())
        self.assertEqual(restored.user_id, 123)
        self.assertEqual(restored.project_root, "/p/one")
        self.assertEqual(restored.agent_project_root, "/agent/foo")
        self.assertEqual(restored.get_session("/p/one"), "sess-1")
        self.assertEqual(restored.get_session("/agent/foo"), "sess-agent")
        self.assertEqual(restored.last_cwd, "/p/one")

    def test_backward_compat_single_session_id(self):
        # Старый формат: одиночный session_id (без session_ids)
        old = {
            "user_id": 7,
            "project_root": "/p/x",
            "project_name": "x",
            "session_id": "old-sess",
            "last_cwd": "/p/x",
            "updated_at": 0,
        }
        st = UserState.from_dict(old)
        # Должен мигрировать в session_ids по project_root
        self.assertEqual(st.get_session("/p/x"), "old-sess")

    def test_separate_agent_and_normal_state(self):
        st = UserState(user_id=1)
        st.set_active(agent=False, root="/normal/one", name="one")
        st.set_active(agent=True, root="/agent/foo", name="foo")
        # Раздельные контексты
        self.assertEqual(st.get_active_root(agent=False), "/normal/one")
        self.assertEqual(st.get_active_root(agent=True), "/agent/foo")
        self.assertEqual(st.active_name(agent=False), "one")
        self.assertEqual(st.active_name(agent=True), "foo")

    def test_set_session_empty_removes(self):
        st = UserState(user_id=1)
        st.set_session("/p", "s1")
        self.assertEqual(st.get_session("/p"), "s1")
        st.set_session("/p", "")
        self.assertEqual(st.get_session("/p"), "")


class TestIsSafeProjectName(unittest.TestCase):
    """Защита от path traversal через имя проекта."""

    def test_valid(self):
        for name in ("project", "my-project", "my_project", "v1.0", "A1_b2"):
            self.assertTrue(is_safe_project_name(name), name)

    def test_invalid(self):
        for name in ("../evil", "a/b", "..", ".", "", "a b", "a\\b", "a$b", "привет"):
            self.assertFalse(is_safe_project_name(name), name)


if __name__ == "__main__":
    unittest.main()