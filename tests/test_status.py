"""Юнит-тесты единого статуса в консоли (перехват логов Pyrogram).

Проверяем, что перехватчик запоминает последнюю ошибку и НЕ штампует её в
stderr, а статус-луп показывает «✗ Ошибка» при свежей ошибке и «● Работаю»
когда она устарела.
"""

import asyncio
import importlib.util
import logging
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


def _record(msg: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("pyrogram.connection", level, "f", 1, msg, None, None)


class TestStatusFilter(unittest.TestCase):
    """Перехватчик: хранит последнюю ошибку, слабые записи игнорирует."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_captures_error(self):
        sf = self.bot._StatusFilter()
        sf.emit(_record("Connection failed: gaierror boom"))
        err, ts = sf.snapshot()
        self.assertIsNotNone(err)
        self.assertIn("gaierror", err)
        self.assertGreater(ts, 0)

    def test_ignores_info(self):
        sf = self.bot._StatusFilter()
        sf.emit(_record("some info", level=logging.INFO))
        err, _ = sf.snapshot()
        self.assertIsNone(err)

    def test_new_error_overwrites(self):
        sf = self.bot._StatusFilter()
        sf.emit(_record("first error"))
        sf.emit(_record("second error"))
        err, _ = sf.snapshot()
        self.assertIn("second error", err)


class TestFriendly(unittest.TestCase):
    """Преобразование сырых сообщений Pyrogram в понятный текст."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_timeout(self):
        self.assertIn("нет ответа", self.bot._friendly('Retrying "updates.GetState" due to: Request timed out'))

    def test_gaierror(self):
        self.assertIn("разрешить хост", self.bot._friendly("Connection failed: gaierror [Errno 8] nodename"))

    def test_connection(self):
        self.assertIn("Нет соединения", self.bot._friendly("Connection failed: Connection reset"))

    def test_interdc(self):
        self.assertIn("Telegram", self.bot._friendly("An error occurred while Telegram was intercommunicating with DC4"))

    def test_unknown_short(self):
        # Незнакомое короче 120 — как есть
        self.assertEqual(self.bot._friendly("что-то неясное"), "что-то неясное")


class TestStatusLoop(unittest.TestCase):
    """Статус-луп печатает «✗ Ошибка» / «● Работаю» при смене состояния."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_shows_error_for_fresh(self):
        bot = self.bot

        async def scenario():
            printed = []
            bot._print_status = lambda state, detail="": (printed.append((state, detail)), state)[1]
            bot._STATUS_FILTER.emit(_record("Connection failed: gaierror boom"))
            stop = asyncio.Event()
            task = asyncio.create_task(bot._status_loop(stop))
            await asyncio.sleep(0.35)
            task.cancel()
            return printed

        printed = asyncio.run(scenario())
        self.assertTrue(printed)
        self.assertEqual(printed[-1][0], "err")
        # Ошибка преобразуется в понятный текст (не сырой gaierror)
        self.assertIn("разрешить хост", printed[-1][1] or "")

    def test_returns_ok_when_stale(self):
        bot = self.bot

        async def scenario():
            printed = []
            bot._print_status = lambda state, detail="": (printed.append((state, detail)), state)[1]
            bot._STATUS_FILTER.emit(_record("old error"))
            bot._STATUS_FILTER._last_error_ts = 0  # «давно» — устарело
            stop = asyncio.Event()
            task = asyncio.create_task(bot._status_loop(stop))
            await asyncio.sleep(0.35)
            task.cancel()
            return printed

        printed = asyncio.run(scenario())
        self.assertTrue(printed)
        self.assertEqual(printed[0][0], "ok")


if __name__ == "__main__":
    unittest.main()