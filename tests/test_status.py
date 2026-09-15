"""Юнит-тесты единого статуса в консоли (перехват логов Pyrogram).

Проверяем, что перехватчик запоминает последнюю ошибку и НЕ штампует её в
stderr, а статус-луп показывает «✗ Ошибка» при свежей ошибке и «● Работаю»
когда она устарела.
"""

import asyncio
import logging
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import status as st  # noqa: E402


def _record(msg: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("pyrogram.connection", level, "f", 1, msg, None, None)


class TestStatusFilter(unittest.TestCase):
    """Перехватчик: хранит последнюю ошибку, слабые записи игнорирует."""

    def test_captures_error(self):
        sf = st._StatusFilter()
        sf.emit(_record("Connection failed: gaierror boom"))
        err, ts = sf.snapshot()
        self.assertIsNotNone(err)
        self.assertIn("gaierror", err)
        self.assertGreater(ts, 0)

    def test_ignores_info(self):
        sf = st._StatusFilter()
        sf.emit(_record("some info", level=logging.INFO))
        err, _ = sf.snapshot()
        self.assertIsNone(err)

    def test_new_error_overwrites(self):
        sf = st._StatusFilter()
        sf.emit(_record("first error"))
        sf.emit(_record("second error"))
        err, _ = sf.snapshot()
        self.assertIn("second error", err)


class TestFriendly(unittest.TestCase):
    """Преобразование сырых сообщений Pyrogram в понятный текст."""

    def test_timeout(self):
        self.assertIn("нет ответа", st._friendly('Retrying "updates.GetState" due to: Request timed out'))

    def test_gaierror(self):
        self.assertIn("разрешить хост", st._friendly("Connection failed: gaierror [Errno 8] nodename"))

    def test_connection(self):
        self.assertIn("Нет соединения", st._friendly("Connection failed: Connection reset"))

    def test_interdc(self):
        self.assertIn("Telegram", st._friendly("An error occurred while Telegram was intercommunicating with DC4"))

    def test_unknown_short(self):
        # Незнакомое короче 120 — как есть
        self.assertEqual(st._friendly("что-то неясное"), "что-то неясное")


class TestStatusLoop(unittest.TestCase):
    """Статус-луп печатает «✗ Ошибка» / «● Работаю» при смене состояния."""

    def setUp(self):
        # Сбрасываем глобальное состояние статуса, чтобы тесты не зависели от
        # остатков предыдущих (общий _STATUS_FILTER и run-ошибка).
        st._STATUS_FILTER._last_error = None
        st._STATUS_FILTER._last_error_ts = 0.0
        st._report_run_error("")

    def test_shows_error_for_fresh(self):
        async def scenario():
            printed = []
            st._draw_status = lambda lines: printed.append(lines)
            st._STATUS_FILTER.emit(_record("Connection failed: gaierror boom"))
            stop = asyncio.Event()
            task = asyncio.create_task(st._status_loop(stop))
            await asyncio.sleep(0.35)
            task.cancel()
            return printed

        printed = asyncio.run(scenario())
        self.assertTrue(printed)
        last = printed[-1]
        # Блок из двух строк: «Работаю» (без 🟢) + ❌ Ошибка (последняя).
        self.assertEqual(len(last), 2)
        self.assertIn("Работаю", last[0])
        self.assertNotIn("🟢", last[0])          # проблема — нет 🟢 у работы
        self.assertIn("❌", last[1])             # ошибка актуальна — с ❌
        self.assertIn("разрешить хост", last[1])

    def test_returns_ok_when_stale(self):
        async def scenario():
            printed = []
            st._draw_status = lambda lines: printed.append(lines)
            st._STATUS_FILTER.emit(_record("old error"))
            st._STATUS_FILTER._last_error_ts = 0  # «давно» — устарело
            stop = asyncio.Event()
            task = asyncio.create_task(st._status_loop(stop))
            await asyncio.sleep(0.35)
            task.cancel()
            return printed

        printed = asyncio.run(scenario())
        self.assertTrue(printed)
        last = printed[-1]
        # Сейчас всё хорошо: 🟢 у «Работаю», последняя ошибка — без ❌.
        self.assertEqual(len(last), 2)
        self.assertIn("🟢", last[0])             # норма — работа с 🟢
        self.assertIn("Ошибка", last[1])
        self.assertNotIn("❌", last[1])          # ошибка устарела — без ❌


class TestRunError(unittest.TestCase):
    """Ошибка запуска Claude отражается в консольном статусе (отвал модели)."""

    def test_is_run_error_by_exit_code_and_markers(self):
        # exit_code != 0 — ошибка
        self.assertTrue(st._is_run_error(type("R", (), {"exit_code": 1, "text": "..."})()))
        # текст с маркером обёртки — ошибка
        self.assertTrue(st._is_run_error(type("R", (), {"exit_code": 0, "text": "API Error: 502 ..."})()))
        # нормальный ответ — не ошибка
        self.assertFalse(st._is_run_error(type("R", (), {"exit_code": 0, "text": "Курс доллара..."})()))

    def test_report_and_snapshot(self):
        st._report_run_error("API Error: 502")
        text, ts = st._snapshot_run_error()
        self.assertEqual(text, "API Error: 502")
        self.assertGreater(ts, 0)

    def test_status_loop_shows_run_error(self):
        st._STATUS_FILTER._last_error = None  # связь с Telegram в норме

        async def scenario():
            printed = []
            st._draw_status = lambda lines: printed.append(lines)
            st._report_run_error("API Error: 502 Cannot connect")
            stop = asyncio.Event()
            task = asyncio.create_task(st._status_loop(stop))
            await asyncio.sleep(0.35)
            task.cancel()
            return printed

        printed = asyncio.run(scenario())
        self.assertTrue(printed)
        last = printed[-1]
        # Свежая run-ошибка → без 🟢, с ❌.
        self.assertEqual(len(last), 2)
        self.assertNotIn("🟢", last[0])
        self.assertIn("❌", last[1])
        self.assertIn("API Error", last[1])


class TestModelUnavailable(unittest.TestCase):
    """_is_model_unavailable: определяет недоступность модели (issue #5)."""

    def _res(self, text, exit_code=0):
        return type("R", (), {"text": text, "exit_code": exit_code})()

    def test_model_unavailable_markers(self):
        # Классические признаки недоступности модели/провайдера
        self.assertTrue(st._is_model_unavailable(self._res("API Error: 502 Cannot connect ...")))
        self.assertTrue(st._is_model_unavailable(self._res("Cannot connect to host")))
        self.assertTrue(st._is_model_unavailable(self._res("... 503 Service Unavailable")))
        self.assertTrue(st._is_model_unavailable(self._res("model not found")))

    def test_normal_answer_not_unavailable(self):
        # Обычный ответ модели — не ошибка доступности
        self.assertFalse(st._is_model_unavailable(self._res("Курс доллара на завтра...")))
        self.assertFalse(st._is_model_unavailable(self._res("")))

    def test_other_error_not_unavailable(self):
        # Сбой запуска / длинный промпт — это НЕ недоступность модели
        self.assertFalse(st._is_model_unavailable(self._res("⚠️ Ошибка: Промпт слишком длинный")))


class TestDrawStatus(unittest.TestCase):
    """_draw_status перерисовывает блок на месте, не накапливая каскад строк."""

    def _draw_sequence(self, states):
        """Прогнать серию _draw_status, вернуть склеенную ANSI-последовательность."""
        buf: list[str] = []
        st._use_color = lambda: True
        orig_write = sys.stdout.write
        try:
            sys.stdout.write = buf.append
            for s in states:
                st._draw_status(s)
        finally:
            sys.stdout.write = orig_write
        return "".join(buf)

    def test_redraw_clears_previous_block(self):
        out = self._draw_sequence([
            ["\x1b[32mРаботаю [t1]"],
            ["\x1b[32mРаботаю [t2]", "\x1b[31m❌ Ошибка: сбой [t2]\x1b[0m"],
            ["\x1b[32mРаботаю [t3]", "\x1b[31m❌ Ошибка: сбой [t3]\x1b[0m"],
        ])
        # В последующих вызовах обязан быть подъём \033[<n>F и затирание \033[J.
        self.assertIn("\x1b[J", out)
        # Подъём происходит на высоту прошлого блока (2 строки) — \033[2F.
        self.assertIn("\x1b[2F", out)
        # Каждый повторный перерисовывает, а не дописывает — значит каскада нет.
        self.assertGreaterEqual(out.count("\x1b[J"), 2)

    def test_prev_lines_tracks_height(self):
        buf: list[str] = []
        st._use_color = lambda: True
        orig = sys.stdout.write
        try:
            sys.stdout.write = buf.append
            st._draw_status(["\x1b[32mРаботаю [t1]"])
            self.assertEqual(st._STATUS_PREV_LINES, 1)
            st._draw_status(["\x1b[32mРаботаю [t2]", "❌ Ошибка [t2]"])
            self.assertEqual(st._STATUS_PREV_LINES, 2)
            st._draw_status(["\x1b[32mРаботаю [t3]", "❌ Ошибка [t3]"])
            self.assertEqual(st._STATUS_PREV_LINES, 2)
        finally:
            sys.stdout.write = orig


if __name__ == "__main__":
    unittest.main()