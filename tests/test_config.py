"""Юнит-тесты config.py: согласованность версии бота."""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import claude_tg_bot  # noqa: E402
from claude_tg_bot import config  # noqa: E402


class TestVersion(unittest.TestCase):
    """Версия бота должна быть заполнена и согласована с __init__."""

    def test_bot_version_present(self):
        self.assertIsInstance(config.BOT_VERSION, str)
        self.assertTrue(config.BOT_VERSION.strip())
        # Формат semver: X.Y.Z
        parts = config.BOT_VERSION.split(".")
        self.assertEqual(len(parts), 3, f"не semver: {config.BOT_VERSION!r}")

    def test_init_version_matches(self):
        # __init__.__version__ читается из config.BOT_VERSION
        self.assertEqual(claude_tg_bot.__version__, config.BOT_VERSION)


class TestFindConfigEnv(unittest.TestCase):
    """Поиск config.env: приоритет мест, None если нет ни в одном."""

    def test_first_existing_wins(self):
        # Если файл есть в ~/.claude-tg-bot — он в приоритете.
        home = Path(tempfile.mkdtemp())
        agent_dir = home / ".claude-tg-bot"
        agent_dir.mkdir(parents=True, exist_ok=True)
        project_dir = Path(tempfile.mkdtemp())
        (agent_dir / "config.env").write_text("X=1\n")
        (project_dir / "config.env").write_text("X=2\n")
        found = config._find_config_env([agent_dir / "config.env", project_dir / "config.env"])
        self.assertEqual(found, agent_dir / "config.env")

    def test_fallback_to_second(self):
        # Нет в sandbox-каталоге, но есть в каталоге проекта — берём оттуда.
        project_dir = Path(tempfile.mkdtemp())
        (project_dir / "config.env").write_text("X=3\n")
        missing = project_dir.parent / "nope"
        found = config._find_config_env([missing / "config.env", project_dir / "config.env"])
        self.assertEqual(found, project_dir / "config.env")

    def test_none_when_missing_everywhere(self):
        # Нет ни в одном месте -> None (бот завершит работу в validate()).
        empty = Path(tempfile.mkdtemp())
        found = config._find_config_env([empty / "a" / "config.env", empty / "b" / "config.env"])
        self.assertIsNone(found)

    def test_validate_raises_when_config_env_missing(self):
        # Если config.env не найден — validate() бросает RuntimeError.
        saved = config.CONFIG_ENV_PATH
        try:
            config.CONFIG_ENV_PATH = None
            with self.assertRaises(RuntimeError):
                config.validate()
        finally:
            config.CONFIG_ENV_PATH = saved


if __name__ == "__main__":
    unittest.main()