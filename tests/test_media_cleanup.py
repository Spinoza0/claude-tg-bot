"""Юнит-тесты очистки скачанных вложений и удаления в корзину/навсегда.

Проверяем: _env_bool (парсинг булевых настроек), _fmt_bytes (формат объёма),
_dim_size, и /clearmedia (_clear_media) — счёт файлов/объёма и удаление по
DELETE_MODE (в корзину или навсегда, через мок _delete_path).
"""

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_bot_module():
    spec = importlib.util.spec_from_file_location(
        "claude_tg_bot", str(PROJECT_ROOT / "claude-tg-bot.py")
    )
    bot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot)
    return bot


def _load_config_module():
    spec = importlib.util.spec_from_file_location("config", str(PROJECT_ROOT / "config.py"))
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    return cfg


class TestEnvBool(unittest.TestCase):
    """_env_bool конвертирует строки env в bool."""

    def test_true_values(self):
        import os
        cfg = _load_config_module()
        for v in ("1", "true", "yes", "on"):
            os.environ["TEST_BOOL"] = v
            self.assertTrue(cfg._env_bool("TEST_BOOL", False))
        for v in ("0", "false", "no", "off", ""):
            os.environ["TEST_BOOL"] = v
            self.assertFalse(cfg._env_bool("TEST_BOOL", False))
        os.environ.pop("TEST_BOOL", None)

    def test_default_when_empty(self):
        cfg = _load_config_module()
        # Если переменной нет — возвращаем default
        self.assertTrue(cfg._env_bool("NONEXISTENT_VAR_XYZ", True))
        self.assertFalse(cfg._env_bool("NONEXISTENT_VAR_XYZ", False))


class TestFmtBytes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_bytes(self):
        self.assertEqual(self.bot._fmt_bytes(512), "512 Б")

    def test_kb(self):
        self.assertEqual(self.bot._fmt_bytes(5 * 1024), "5.0 КБ")

    def test_mb(self):
        self.assertEqual(self.bot._fmt_bytes(int(1.3 * 1024 * 1024)), "1.3 МБ")

    def test_gb(self):
        self.assertEqual(self.bot._fmt_bytes(int(2.1 * 1024**3)), "2.1 ГБ")


class TestDirSize(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_sum(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.bin").write_bytes(b"x" * 100)
            sub = d / "sub"
            sub.mkdir()
            (sub / "b.bin").write_bytes(b"y" * 50)
            self.assertEqual(self.bot._dir_size(d), 150)


class TestClearMedia(unittest.TestCase):
    """_clear_media: считает файлы/объём и удаляет по DELETE_MODE."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_delete_to_trash(self):
        import tempfile, asyncio
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            media = root / ".claude_tg_bot_media"
            media.mkdir()
            (media / "a.jpg").write_bytes(b"x" * 1024)
            (media / "b.mp4").write_bytes(b"y" * 2048)

            # Мокаем _delete_path — проверяем, что вызван с каталогом и режимом
            deleted = []
            self.bot._delete_path = lambda p, mode="": deleted.append((p, mode))
            self.bot.config.DELETE_MODE = "trash"

            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            # мокаем _reply в модуле бота (используется как глобальная)
            self.bot._reply = fake_reply

            async def run():
                await self.bot._clear_media(None, str(root), sandbox=False, root=str(root))
            asyncio.run(run())

            self.assertEqual(len(deleted), 1)
            target, mode = deleted[0]
            self.assertEqual(target, media)
            self.assertEqual(mode, "trash")
            self.assertIn("2", seen["msg"])  # 2 файла
            self.assertIn("в корзину", seen["msg"])

    def test_silent_no_dir_no_reply(self):
        # /clear при отсутствии каталога вложений: silent=True → никакого ответа.
        import tempfile, asyncio
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            self.bot._reply = fake_reply
            async def run():
                await self.bot._clear_media(None, str(root), sandbox=False, root=str(root), silent=True)
            asyncio.run(run())
            self.assertNotIn("msg", seen)  # ответа быть не должно

    def test_silent_empty_dir_deleted(self):
        # /clear при пустом каталоге: silent=True → каталог удаляется + сообщение.
        import tempfile, asyncio
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            media = root / ".claude_tg_bot_media"
            media.mkdir()
            deleted = []
            self.bot._delete_path = lambda p, mode="": deleted.append((p, mode))
            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            self.bot._reply = fake_reply
            async def run():
                await self.bot._clear_media(None, str(root), sandbox=False, root=str(root), silent=True)
            asyncio.run(run())
            self.assertEqual(len(deleted), 1)      # пустой каталог удалён
            self.assertEqual(deleted[0][0], media)
            self.assertIn("пуст", seen["msg"])     # сообщение про удаление пустого каталога


class TestMediaSize(unittest.TestCase):
    """_media_size: считает файлы/объём скачанных вложений, ничего не удаляя."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_reports_count_and_size(self):
        import tempfile, asyncio
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            media = root / ".claude_tg_bot_media"
            media.mkdir()
            (media / "a.jpg").write_bytes(b"x" * 1024)
            (media / "b.mp4").write_bytes(b"y" * 2048)

            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            self.bot._reply = fake_reply

            async def run():
                await self.bot._media_size(None, str(root))
            asyncio.run(run())

            self.assertIn("2", seen["msg"])       # 2 файла
            self.assertIn("3.0 КБ", seen["msg"])  # 1+2 = 3 КБ
            self.assertNotIn("Удалено", seen["msg"])  # /mediasize ничего не удаляет

    def test_empty_reports_none(self):
        import tempfile, asyncio
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            self.bot._reply = fake_reply
            asyncio.run(self.bot._media_size(None, str(root)))
            self.assertIn("Нет скачанных вложений", seen["msg"])


class TestClearMediaRouting(unittest.TestCase):
    """/clearmedia не должен попадать в на /clear (префиксный конфликт).

    Раньше проверка была text.startswith('/clear') — она ловила и `/clearmedia`,
    который тоже начинается с `/clear`, и уводила его в on_chat (промпт claude),
    а не в on_command. Отсюда «Unknown command». Проверяем, что теперь
    маршрутизация точная.
    """

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def _msg(self, text):
        from types import SimpleNamespace
        return SimpleNamespace(
            text=text, caption=None, chat=SimpleNamespace(id=123),
            photo=None, video=None, audio=None, voice=None, document=None,
            outgoing=False, reply_to_message_id=None,
        )

    def test_routing(self):
        bot = self.bot
        bot._allowed = lambda uid, cid: True
        bot._is_allowed_user = lambda uid: True
        bot._is_sandbox_message = lambda t: False
        bot._author = lambda m: 777
        routed = []
        async def fake_on_command(client, message, text, sandbox=False): routed.append(("command", text))
        async def fake_on_chat(client, message, text, sandbox=False): routed.append(("chat", text))
        bot.on_command = fake_on_command
        bot.on_chat = fake_on_chat
        asyncio.run(bot.on_all_message(None, self._msg("/clearmedia")))
        asyncio.run(bot.on_all_message(None, self._msg("/clear")))
        self.assertEqual(routed[0], ("command", "/clearmedia"))
        self.assertEqual(routed[1], ("chat", "/clear"))


if __name__ == "__main__":
    unittest.main()