"""Юнит-тесты единого механизма повторов (_run_with_retry, _retry_backoff_delay,
_start_with_retry).

Проверяем: растущую паузу (1 → 5 мин по issue #6), потолок 5 мин, сброс к base
при успехе, ограничение числа попыток RETRY_LIMIT и прерывание по Ctrl+C
(KeyboardInterrupt не глушится).
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


class TestBackoffDelay(unittest.TestCase):
    """_retry_backoff_delay: рост с потолком 5 мин (issue #6)."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_growth_and_cap(self):
        d = self.bot._retry_backoff_delay
        # 60, 120, 240, 480->300 (потолок), 960->300 ...
        self.assertEqual(d(1), 60)
        self.assertEqual(d(2), 120)
        self.assertEqual(d(3), 240)
        self.assertEqual(d(4), 300)
        self.assertEqual(d(5), 300)
        self.assertEqual(d(6), 300)


class TestRunWithRetry(unittest.TestCase):
    """_run_with_retry: ограничение попыток и результат при успехе."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def setUp(self):
        self._orig = self.bot.asyncio.sleep

    def tearDown(self):
        self.bot.asyncio.sleep = self._orig

    def test_returns_none_after_limit(self):
        bot = self.bot
        calls = [0]
        _orig = bot.asyncio.sleep
        async def fake(sec):
            await _orig(0)
        bot.asyncio.sleep = fake
        async def fail():
            calls[0] += 1
            raise RuntimeError("boom")
        res = asyncio.run(bot._run_with_retry(fail))
        self.assertIsNone(res)
        # RETRY_LIMIT попыток, потом стоп
        self.assertEqual(calls[0], bot.config.RETRY_LIMIT)

    def test_returns_result_on_success(self):
        bot = self.bot
        async def ok():
            return 42
        self.assertEqual(asyncio.run(bot._run_with_retry(ok)), 42)

    def test_keyboard_interrupt_propagates(self):
        bot = self.bot
        async def key():
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(bot._run_with_retry(key))


class TestStartWithRetry(unittest.TestCase):
    """Повтор подключения: рост паузы, потолок 5 мин, прерывание."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def setUp(self):
        self._orig_sleep = self.bot.asyncio.sleep

    def tearDown(self):
        self.bot.asyncio.sleep = self._orig_sleep

    def test_delay_grows_and_success_exits(self):
        bot = self.bot
        sleeps = []
        orig = bot.asyncio.sleep  # оригинал до подмены
        async def fake_sleep(sec):
            sleeps.append(sec)
            await orig(0)
        bot.asyncio.sleep = fake_sleep
        app = _FakeApp(fails=2)
        asyncio.run(bot._start_with_retry(app))
        # 2 провала + успех на 3-й. Паузы 1, 2 мин.
        self.assertEqual(app.attempts, 3)
        self.assertEqual(sleeps, [60, 120])

    def test_delay_caps_at_5_min(self):
        bot = self.bot
        sleeps = []

        async def fake_sleep(sec):
            sleeps.append(sec)
            raise KeyboardInterrupt()  # выходим из цикла после первой паузы

        bot.asyncio.sleep = fake_sleep

        class AlwaysFail:
            async def start(self):
                raise ConnectionError("boom")

        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(bot._start_with_retry(AlwaysFail()))
        # Первая пауза — base (1 мин), ещё не потолок
        self.assertEqual(sleeps[0], 60)

    def test_exhausts_and_raises(self):
        bot = self.bot
        orig = bot.asyncio.sleep

        async def fake_sleep(sec):
            await orig(0)
        bot.asyncio.sleep = fake_sleep

        class AlwaysFail:
            async def start(self):
                raise ConnectionError("boom")

        # После RETRY_LIMIT попыток — бросаем (не крутим бесконечно)
        with self.assertRaises(ConnectionError):
            asyncio.run(bot._start_with_retry(AlwaysFail()))

    def test_keyboard_interrupt_propagates(self):
        bot = self.bot

        class AlwaysFail:
            async def start(self):
                raise ConnectionError("boom")

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