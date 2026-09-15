"""Юнит-тесты session.py: состояние пользователя и валидация имени проекта."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from session import UserState, is_safe_project_name, has_session, claude_project_dir, find_latest_session  # noqa: E402


class TestUserState(unittest.TestCase):
    """Круговая сериализация и раздельное состояние обычного/агентского режимов."""

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
        # Раздельные контексты
        self.assertEqual(st.get_active_root(sandbox=False), "/normal/one")
        self.assertEqual(st.get_active_root(sandbox=True), "/sandbox/foo")
        self.assertEqual(st.active_name(sandbox=False), "one")
        self.assertEqual(st.active_name(sandbox=True), "foo")


class TestHasSession(unittest.TestCase):
    """has_session: есть ли сессия в каталоге (для --continue по каталогу)."""

    def test_empty_dir_false(self):
        # Каталог, где нет ~/.claude/projects/<slug> (или он пуст) → False
        self.assertFalse(has_session(Path("/nonexistent/xyz-123")))

    def test_some_dir_returns_bool(self):
        # Реальный каталог: должен вернуть bool (есть сессия или нет), не упасть.
        r = has_session(PROJECT_ROOT)
        self.assertIsInstance(r, bool)

    def test_slug_replaces_dot_with_dash(self):
        # Claude составляет slug из абсолютного пути, заменяя '/' и '.' на '-'.
        # Напр. /Users/sintyurin.ivan/... -> -Users-sintyurin-ivan-... (точка в
        # имени пользователя становится дефисом, а ранее была '_' — из-за этого
        # has_session искал не тот каталог и --continue не срабатывал).
        d = claude_project_dir(Path("/Users/sintyurin.ivan/StudioProjects/x"))
        self.assertEqual(
            d.name,
            "-Users-sintyurin-ivan-StudioProjects-x",
        )


class TestFindLatestSession(unittest.TestCase):
    """find_latest_session: отдаёт самый свежий .jsonl (для --resume)."""

    def _mk(self, tmp: Path, names):
        for n in names:
            (tmp / n).touch()

    def test_returns_none_when_empty(self):
        self.assertIsNone(find_latest_session(Path("/nonexistent/xyz-123")))

    def test_returns_newest_by_mtime(self, ):
        import tempfile
        import os
        import time as _t
        d = claude_project_dir(PROJECT_ROOT)
        # Пусть каталог реальный; создаём два файла с разным mtime.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "old.jsonl").touch()
            (tmp / "new.jsonl").touch()
            _t.sleep(0.01)  # чтобы mtime отличался
            os.utime(tmp / "new.jsonl", None)
            # подменяем каталог сессий, чтобы не трогать реальный
            import session as _s
            orig = _s.claude_project_dir
            _s.claude_project_dir = lambda cwd: tmp
            try:
                self.assertEqual(find_latest_session(PROJECT_ROOT), "new")
            finally:
                _s.claude_project_dir = orig


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