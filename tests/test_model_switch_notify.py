"""Юнит-тесты уведомления в Telegram о смене модели (issue #5).

Проверяем поведение _run_and_reply: когда основная модель не отвечает, бот
переключается на запасной набор аргументов (COMMAND_ARGS_ALTERNATIVE) и
сообщает об этом пользователю в Telegram, а не только пишет в лог. Также
проверяем, что уведомление о восстановлении уходит после успешного ответа
запасной модели.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import handlers  # noqa: E402


def _make_result(text: str, exit_code: int = 0):
    """Фейковый ClaudeResult: только нужные поля для _is_model_unavailable/_is_run_error."""
    return SimpleNamespace(text=text, exit_code=exit_code, session_id="")


class _FakeMsg:
    """Фейковый message: минимальные поля, которые использует _run_and_reply."""

    def __init__(self):
        self.chat = SimpleNamespace(id=1)
        self.id = 10
        self.reply_text = AsyncMock()


class TestModelSwitchNotify(unittest.TestCase):
    """Уведомление в Telegram о смене модели при её недоступности."""

    def setUp(self):
        # Гарантируем, что альтернативный набор включён и лимит известен.
        self._alt = patch.object(handlers.config, "COMMAND_ARGS_ALTERNATIVE", "--provider cline-pass")
        self._limit = patch.object(handlers.config, "RETRY_LIMIT", 5)
        self._alt.start()
        self._limit.start()
        self.addCleanup(self._alt.stop)
        self.addCleanup(self._limit.stop)

    async def _run(self, run_claude_side_effect, sentry: list):
        """Запустить _run_and_reply с замоканными зависимостями, вернуть команды вызова run_claude."""
        message = _FakeMsg()

        async def _fake_send_with_retry(msg, text, *a, **k):
            sentry.append(text)
            return msg

        calls = []

        async def _fake_run_claude(prompt, cwd, resume_session_id=None, image_paths=None,
                                   proc_registry=None, command_args=None):
            calls.append({"prompt": prompt, "cwd": cwd, "command_args": command_args})
            return run_claude_side_effect(calls)

        client = SimpleNamespace(delete_messages=AsyncMock())
        st = SimpleNamespace(project_root="/tmp/proj")

        with patch.object(handlers, "run_claude", _fake_run_claude), \
             patch.object(handlers, "_send_with_retry", _fake_send_with_retry), \
             patch.object(handlers, "_send_split", AsyncMock(return_value=None)), \
             patch.object(handlers, "find_latest_session", lambda p: None), \
             patch.object(handlers, "_reply", lambda m, t: SimpleNamespace(chat=SimpleNamespace(id=1), id=10)), \
             patch.object(handlers.asyncio, "sleep", AsyncMock(return_value=None)):
            await handlers._run_and_reply(client, message, st, "тест", [], cwd="/tmp/proj")

        return calls, sentry

    def test_switches_to_alt_and_notifies(self):
        # Попытка 1 (база) — модель недоступна; попытка 2 (alt) — ответила.
        async def scenario():
            sentry = []
            def effect(calls):
                # первая попытка (база) — недоступна; вторая (alt) — ок
                if len(calls) == 1:
                    return _make_result("API Error: 502 Cannot connect", exit_code=1)
                return _make_result("Курс доллара вырос")
            calls, sentry = await self._run(effect, sentry)
            return calls, sentry

        calls, sentry = asyncio.run(scenario())
        # Альтернативный набор реально дошёл до run_claude на 2-й попытке.
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0]["command_args"])                      # база
        self.assertEqual(calls[1]["command_args"], "--provider cline-pass")  # alt
        # В Telegram ушло и уведомление о переключении, и о восстановлении.
        self.assertTrue(any("switching to the fallback" in s for s in sentry))
        self.assertTrue(any("fallback model responded" in s for s in sentry))

    def test_no_switch_notification_when_alt_unset(self):
        # Без COMMAND_ARGS_ALTERNATIVE бот НЕ переключается и не шлёт уведомление.
        with patch.object(handlers.config, "COMMAND_ARGS_ALTERNATIVE", ""):
            calls = []
            async def scenario():
                sentry = []
                def effect(c):
                    return _make_result("ок") if len(c) > 1 else _make_result("API Error: 502", exit_code=1)
                # без alt variants=[None], значит попытка 2 снова база (вариаций нет)
                r = await self._run(effect, sentry)
                return r
            calls, sentry = asyncio.run(scenario())
        # Смена модели не происходила — уведомлений о запасной модели нет.
        self.assertFalse(any("switching to the fallback" in s for s in sentry))
        self.assertFalse(any("fallback model responded" in s for s in sentry))


if __name__ == "__main__":
    unittest.main()