"""Юнит-тесты умного повтора подключения к Telegram (_start_with_retry).

Проверяем: растущую паузу (1 → 15 мин), фиксацию на 15 мин, выход при успехе
и прерывание по Ctrl+C (KeyboardInterrupt не глушится).
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


class _FakeApp:
    """app.start(): сначала fails раз бросает сетевую ошибку, потом успех."""

    def __init__(self, fails):
        self.fails = fails
        self.attempts = 0

    async def start(self):
        self.attempts += 1
        if self.attempts <= self.fails:
            raise ConnectionError("gaierror nodename")
        return True


class TestStartWithRetry(unittest.TestCase):
    """Повтор подключения: рост паузы, потолок 15 мин, прерывание."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def setUp(self):
        # Подмена asyncio.sleep затрагивает весь модуль asyncio — сохраняем
        # оригинал, чтобы не сломать другие тесты (статус-луп тоже спит).
        self._orig_sleep = self.bot.asyncio.sleep

    def tearDown(self):
        self.bot.asyncio.sleep = self._orig_sleep

    def test_delay_grows_and_success_exits(self):
        bot = self.bot
        sleeps = []
        orig = bot.asyncio.sleep  # оригинал до подмены
        # подменяем: записываем задержку, но реально не ждём (оригинал sleep)
        async def fake_sleep(sec):
            sleeps.append(sec)
            if orig:
                await orig(0)
        bot.asyncio.sleep = fake_sleep
        app = _FakeApp(fails=5)
        asyncio.run(bot._start_with_retry(app))
        # После 5 провалов + успех на 6-й. Паузы 1..5 мин.
        self.assertEqual(app.attempts, 6)
        self.assertEqual(sleeps, [60, 120, 180, 240, 300])

    def test_delay_caps_at_15_min(self):
        bot = self.bot
        sleeps = []
        counter = [0]

        async def fake_sleep(sec):
            sleeps.append(sec)
            counter[0] += 1
            if counter[0] >= 40:  # достаточно попыток, чтобы дойти до потолка
                raise KeyboardInterrupt()  # эмулируем Ctrl+C и выходим из цикла

        bot.asyncio.sleep = fake_sleep

        class AlwaysFail:
            async def start(self):
                raise ConnectionError("boom")

        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(bot._start_with_retry(AlwaysFail()))
        # Потолок 15 мин = 900 сек, дальше держим 900
        self.assertEqual(sleeps[-1], 900)
        self.assertEqual(sleeps[-2], 900)

    def test_keyboard_interrupt_propagates(self):
        bot = self.bot

        class AlwaysFail:
            async def start(self):
                raise ConnectionError("boom")

        # первая же пауза прерывается Ctrl+C
        async def fake_sleep(sec):
            raise KeyboardInterrupt()

        bot.asyncio.sleep = fake_sleep
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(bot._start_with_retry(AlwaysFail()))

    def test_non_network_error_propagates(self):
        bot = self.bot

        class AuthFail:
            async def start(self):
                raise ValueError("неверный пароль")  # НЕ сетевая ошибка

        with self.assertRaises(ValueError):
            asyncio.run(bot._start_with_retry(AuthFail()))


if __name__ == "__main__":
    unittest.main()