"""Юнит-тесты очистки скачанных вложений и удаления в корзину/навсегда.

Проверяем: _env_bool (парсинг булевых настроек), _fmt_bytes (формат объёма),
_dir_size, и /clearmedia (_clear_media) — счёт файлов/объёма и удаление по
DELETE_MODE (в корзину или навсегда, через мок _delete_path).
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import config  # noqa: E402
from claude_tg_bot import commands, handlers, media  # noqa: E402


class TestEnvBool(unittest.TestCase):
    """_env_bool конвертирует строки env в bool."""

    def test_true_values(self):
        for v in ("1", "true", "yes", "on"):
            os.environ["TEST_BOOL"] = v
            self.assertTrue(config._env_bool("TEST_BOOL", False))
        for v in ("0", "false", "no", "off", ""):
            os.environ["TEST_BOOL"] = v
            self.assertFalse(config._env_bool("TEST_BOOL", False))
        os.environ.pop("TEST_BOOL", None)

    def test_default_when_empty(self):
        # Если переменной нет — возвращаем default
        self.assertTrue(config._env_bool("NONEXISTENT_VAR_XYZ", True))
        self.assertFalse(config._env_bool("NONEXISTENT_VAR_XYZ", False))


class TestFmtBytes(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(media._fmt_bytes(512), "512 B")

    def test_kb(self):
        self.assertEqual(media._fmt_bytes(5 * 1024), "5.0 KB")

    def test_mb(self):
        self.assertEqual(media._fmt_bytes(int(1.3 * 1024 * 1024)), "1.3 MB")

    def test_gb(self):
        self.assertEqual(media._fmt_bytes(int(2.1 * 1024**3)), "2.1 GB")


class TestDirSize(unittest.TestCase):
    def test_sum(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.bin").write_bytes(b"x" * 100)
            sub = d / "sub"
            sub.mkdir()
            (sub / "b.bin").write_bytes(b"y" * 50)
            self.assertEqual(media._dir_size(d), 150)


class TestClearMedia(unittest.TestCase):
    """_clear_media: считает файлы/объём и удаляет по DELETE_MODE."""

    def test_delete_to_trash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            media_dir = root / ".claude_tg_bot_media"
            media_dir.mkdir()
            (media_dir / "a.jpg").write_bytes(b"x" * 1024)
            (media_dir / "b.mp4").write_bytes(b"y" * 2048)

            # Мокаем _delete_path — проверяем, что вызван с каталогом и режимом
            deleted = []
            commands._delete_path = lambda p, mode="": deleted.append((p, mode))
            commands.config.DELETE_MODE = "trash"

            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            commands._reply = fake_reply

            asyncio.run(commands._clear_media(None, str(root), sandbox=False, root=str(root)))

            self.assertEqual(len(deleted), 1)
            target, mode = deleted[0]
            self.assertEqual(target, media_dir)
            self.assertEqual(mode, "trash")
            self.assertIn("2", seen["msg"])  # 2 files
            self.assertIn("to trash", seen["msg"])

    def test_silent_no_dir_no_reply(self):
        # /clear при отсутствии каталога вложений: silent=True → никакого ответа.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            commands._reply = fake_reply
            asyncio.run(commands._clear_media(None, str(root), sandbox=False, root=str(root), silent=True))
            self.assertNotIn("msg", seen)  # ответа быть не должно

    def test_silent_empty_dir_deleted(self):
        # /clear при пустом каталоге: silent=True → каталог удаляется + сообщение.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            media_dir = root / ".claude_tg_bot_media"
            media_dir.mkdir()
            deleted = []
            commands._delete_path = lambda p, mode="": deleted.append((p, mode))
            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            commands._reply = fake_reply
            asyncio.run(commands._clear_media(None, str(root), sandbox=False, root=str(root), silent=True))
            self.assertEqual(len(deleted), 1)      # empty folder was deleted
            self.assertEqual(deleted[0][0], media_dir)
            self.assertIn("Empty", seen["msg"])    # message about deleting the empty folder


class TestMediaSize(unittest.TestCase):
    """_media_size: считает файлы/объём скачанных вложений, ничего не удаляя."""

    def test_reports_count_and_size(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            media_dir = root / ".claude_tg_bot_media"
            media_dir.mkdir()
            (media_dir / "a.jpg").write_bytes(b"x" * 1024)
            (media_dir / "b.mp4").write_bytes(b"y" * 2048)

            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            commands._reply = fake_reply

            asyncio.run(commands._media_size(None, str(root)))

            self.assertIn("2", seen["msg"])       # 2 files
            self.assertIn("3.0 KB", seen["msg"])  # 1+2 = 3 KB
            self.assertNotIn("Deleted", seen["msg"])  # /mediasize deletes nothing

    def test_empty_reports_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            seen = {}
            async def fake_reply(message_, text, *a, **k):
                seen["msg"] = text
            commands._reply = fake_reply
            asyncio.run(commands._media_size(None, str(root)))
            self.assertIn("No downloaded attachments", seen["msg"])


class TestClearMediaRouting(unittest.TestCase):
    """/clearmedia не должен попадать в на /clear (префиксный конфликт).

    Раньше проверка была text.startswith('/clear') — она ловила и `/clearmedia`,
    который тоже начинается с `/clear`, и уводила его в on_chat (промпт claude),
    а не в on_command. Отсюда «Unknown command». Проверяем, что теперь
    маршрутизация точная.
    """

    def _msg(self, text):
        return SimpleNamespace(
            text=text, caption=None, chat=SimpleNamespace(id=123),
            photo=None, video=None, audio=None, voice=None, document=None,
            outgoing=False, reply_to_message_id=None,
        )

    def test_routing(self):
        handlers._allowed = lambda uid, cid: True
        handlers._is_allowed_user = lambda uid: True
        handlers._is_sandbox_message = lambda t: False
        handlers._author = lambda m: 777
        routed = []
        async def fake_on_command(client, message, text, sandbox=False): routed.append(("command", text))
        async def fake_on_chat(client, message, text, sandbox=False): routed.append(("chat", text))
        handlers.on_command = fake_on_command
        handlers.on_chat = fake_on_chat
        asyncio.run(handlers.on_all_message(None, self._msg("/clearmedia")))
        asyncio.run(handlers.on_all_message(None, self._msg("/clear")))
        self.assertEqual(routed[0], ("command", "/clearmedia"))
        self.assertEqual(routed[1], ("chat", "/clear"))


if __name__ == "__main__":
    unittest.main()