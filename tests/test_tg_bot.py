"""Юнит-тесты чистой логики claude-tg-bot.py (без Telegram/сети).

Покрываем только детерминированные функции: парсинг @helpbot, определение
расширения вложения, отправку с ретраями. Для импорта модуля добавляем корень
проекта в sys.path (как делает сам бот при запуске скриптом).
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
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


class TestAgentParsing(unittest.TestCase):
    """Парсинг сообщений @helpbot."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()
        # В тестовой среде SANDBOX_COMMAND может быть пуст (нет config.env).
        # Задаём триггер явно, чтобы тестировать разбор @helpbot (см. config).
        cls.bot.AGENT_PREFIX = "@helpbot"

    def test_is_agent_message_true(self):
        self.assertTrue(self.bot._is_agent_message("@helpbot"))
        self.assertTrue(self.bot._is_agent_message("@helpbot курс доллара"))
        self.assertTrue(self.bot._is_agent_message("  @helpbot /status"))

    def test_is_agent_message_false(self):
        # Не путать с похожими строками и старым /agent
        self.assertFalse(self.bot._is_agent_message("@helpbotxyz"))
        self.assertFalse(self.bot._is_agent_message("@helpbotfoo bar"))
        self.assertFalse(self.bot._is_agent_message("просто текст"))
        self.assertFalse(self.bot._is_agent_message("/status"))
        self.assertFalse(self.bot._is_agent_message("/agent"))
        self.assertFalse(self.bot._is_agent_message(""))

    def test_strip_agent_prefix(self):
        # Срезаем @helpbot и ВСЕ пробелы после него
        self.assertEqual(self.bot._strip_agent_prefix("@helpbot курс"), "курс")
        self.assertEqual(self.bot._strip_agent_prefix("@helpbot /status"), "/status")
        self.assertEqual(self.bot._strip_agent_prefix("@helpbot   много пробелов"), "много пробелов")
        self.assertEqual(self.bot._strip_agent_prefix("@helpbot\tтаб"), "таб")
        # @helpbot без хвоста → пустая строка
        self.assertEqual(self.bot._strip_agent_prefix("@helpbot"), "")
        self.assertEqual(self.bot._strip_agent_prefix("@helpbot   "), "")


class _FakeMedia:
    """Имитация объекта вложения с file_name/mime_type."""

    def __init__(self, file_name=None, mime_type=None):
        self.file_name = file_name
        self.mime_type = mime_type


class TestMediaExt(unittest.TestCase):
    """Определение расширения вложения."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_extension_from_filename(self):
        self.assertEqual(self.bot._media_ext(_FakeMedia("clip.mp4", "video/mp4")), ".mp4")
        self.assertEqual(self.bot._media_ext(_FakeMedia("pic.JPG", "image/jpeg")), ".jpg")
        self.assertEqual(self.bot._media_ext(_FakeMedia("song.MP3", "audio/mpeg")), ".mp3")

    def test_extension_from_mime(self):
        # Нет file_name (как у Voice) — берём из mime_type
        self.assertEqual(self.bot._media_ext(_FakeMedia(None, "video/webm")), ".webm")
        self.assertEqual(self.bot._media_ext(_FakeMedia(None, "audio/ogg")), ".ogg")
        self.assertEqual(self.bot._media_ext(_FakeMedia(None, "application/pdf")), ".pdf")

    def test_extension_default_bin(self):
        # Нет ни file_name, ни известного mime → .bin
        self.assertEqual(self.bot._media_ext(_FakeMedia(None, None)), ".bin")

    def test_extension_filename_without_dot(self):
        # file_name без точки — откатываемся к mime
        self.assertEqual(self.bot._media_ext(_FakeMedia("noext", "image/png")), ".png")


class TestSniffImageExt(unittest.TestCase):
    """Определение расширения изображения по магическим байтам."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def _write(self, data: bytes) -> Path:
        # Временный файл, который удаляется сам после теста (иначе мусор
        # sniff_*.tmp копится в /tmp).
        fd, name = tempfile.mkstemp(prefix="sniff_", suffix=".tmp")
        os.close(fd)
        p = Path(name)
        p.write_bytes(data)
        self.addCleanup(p.unlink, missing_ok=True)
        return p

    def test_png(self):
        p = self._write(b"\x89PNG\r\n\x1a\nrest")
        self.assertEqual(self.bot._sniff_image_ext(p), ".png")

    def test_jpg(self):
        p = self._write(b"\xff\xd8\xff\xe0rest")
        self.assertEqual(self.bot._sniff_image_ext(p), ".jpg")

    def test_webp(self):
        p = self._write(b"RIFFxxxxWEBPdata")
        self.assertEqual(self.bot._sniff_image_ext(p), ".webp")

    def test_gif(self):
        p = self._write(b"GIF89a....")
        self.assertEqual(self.bot._sniff_image_ext(p), ".gif")

    def test_unknown_fallback_jpg(self):
        p = self._write(b"not-an-image-at-all")
        self.assertEqual(self.bot._sniff_image_ext(p), ".jpg")


class _FlakyMessage:
    """Сообщение, у которого reply_text падает первые fails раз."""

    def __init__(self, fails):
        self.fails = fails
        self.calls = 0
        self.replied = None

    async def reply_text(self, text):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("Telegram says: [500 INTERDC_X_CALL_ERROR] boom")
        self.replied = text
        return type("R", (), {"chat": type("C", (), {"id": 1})(), "id": 1})()


class TestSendWithRetry(unittest.TestCase):
    """Отправка ответа с повторными попытками."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_success_after_transient_errors(self):
        m = _FlakyMessage(fails=2)
        res = asyncio.run(self.bot._send_with_retry(m, "ответ", attempts=3, delay=0))
        self.assertIsNotNone(res)
        self.assertEqual(m.calls, 3)
        self.assertEqual(m.replied, "🤖 ответ")

    def test_all_fail_returns_none(self):
        m = _FlakyMessage(fails=99)
        res = asyncio.run(self.bot._send_with_retry(m, "текст", attempts=2, delay=0))
        self.assertIsNone(res)
        self.assertEqual(m.calls, 2)


class _FakeMedia:
    def __init__(self, file_name=None, mime_type=None):
        self.file_name = file_name
        self.mime_type = mime_type


class TestSplitRouting(unittest.TestCase):
    """Раздельные обработчики (фото/видео/аудио/файл) маршрутизируются верно.

    При вызове on_photo должно идти в _handle_attachment со sniff_ext=True,
    а on_video/on_audio/on_document — со своим расширением из _media_ext.
    Проверяем через делегирование в _handle_attachment (мокаем его).
    """

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def _make_msg(self, photo=None, video=None, audio=None, voice=None, document=None):
        class M:
            pass
        m = M()
        m.photo, m.video, m.audio, m.voice, m.document = photo, video, audio, voice, document
        m.caption = ""
        return m

    def _mock_handle(self):
        bot = self.bot
        calls = []
        async def mock(*a, **k):
            calls.append((a, k))
        bot._handle_attachment = mock
        return calls

    def test_photo_uses_sniff(self):
        bot = self.bot
        calls = self._mock_handle()
        m = self._make_msg(photo=_FakeMedia())
        asyncio.run(bot.on_photo(None, m))
        self.assertEqual(len(calls), 1)
        _, kw = calls[0]
        self.assertEqual(kw["sniff_ext"], True)

    def test_video_ext(self):
        calls = self._mock_handle()
        m = self._make_msg(video=_FakeMedia("c.mp4", "video/mp4"))
        asyncio.run(self.bot.on_video(None, m))
        # _handle_attachment(client, msg, kind, ext, ...) → a[0]=(None,m,'видео','.mp4',...)
        self.assertEqual(calls[0][0][3], ".mp4")

    def test_audio_ext(self):
        calls = self._mock_handle()
        m = self._make_msg(audio=_FakeMedia("s.mp3", "audio/mpeg"))
        asyncio.run(self.bot.on_audio(None, m))
        self.assertEqual(calls[0][0][3], ".mp3")

    def test_document_ext(self):
        calls = self._mock_handle()
        m = self._make_msg(document=_FakeMedia("doc.pdf", "application/pdf"))
        asyncio.run(self.bot.on_document(None, m))
        self.assertEqual(calls[0][0][3], ".pdf")

    def test_video_note_as_video(self):
        # Кружок (video_note) уходит как видео, ext .mp4 по умолчанию
        calls = self._mock_handle()
        class M:
            pass
        m = M()
        m.video_note = _FakeMedia(None, "video/mp4")
        m.photo = m.video = m.audio = m.voice = m.document = None
        m.caption = ""
        asyncio.run(self.bot.on_video_note(None, m))
        # _handle_attachment(client, msg, kind, ext, ...) → ext = a[0][3]
        self.assertEqual(calls[0][0][3], ".mp4")
        # kind = 'видео-кружок'
        self.assertEqual(calls[0][0][2], "видео-кружок")


class TestTextualMediaPrompt(unittest.TestCase):
    """_textual_media_prompt формирует описание для опроса/гео/контакта."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def _msg(self, **kw):
        from types import SimpleNamespace
        d = dict(photo=None, video=None, video_note=None, audio=None, voice=None,
                 document=None, animation=None, sticker=None, contact=None,
                 location=None, venue=None, poll=None, dice=None, game=None,
                 web_app_data=None, paid_media=None)
        d.update(kw)
        return SimpleNamespace(**d)

    def _poll(self, question, options):
        from types import SimpleNamespace
        return SimpleNamespace(question=SimpleNamespace(text=question),
                               options=[SimpleNamespace(text=o) for o in options])

    def test_poll(self):
        p = self._poll("Как дела?", ["Отлично", "Норм"])
        prompt = self.bot._textual_media_prompt(self._msg(poll=p))
        self.assertIn("Как дела?", prompt)
        self.assertIn("Отлично", prompt)
        self.assertIn("Норм", prompt)

    def test_contact(self):
        from types import SimpleNamespace
        c = SimpleNamespace(first_name="Иван", last_name="Иванов", phone_number="+79991234567")
        prompt = self.bot._textual_media_prompt(self._msg(contact=c))
        self.assertIn("Иван", prompt)
        self.assertIn("79991234567", prompt)

    def test_location(self):
        from types import SimpleNamespace
        loc = SimpleNamespace(latitude=55.75, longitude=37.61)
        prompt = self.bot._textual_media_prompt(self._msg(location=loc))
        self.assertIn("55.75", prompt)

    def test_none_for_other(self):
        self.assertIsNone(self.bot._textual_media_prompt(self._msg(photo=object())))


class TestStickerAsFile(unittest.TestCase):
    """Стикер уходит как файл-вложение в claude (а не «не могу обработать»)."""

    @classmethod
    def setUpClass(cls):
        cls.bot = _load_bot_module()

    def test_sticker_routes_to_attachment(self):
        calls = []
        async def mock(*a, **k):
            calls.append((a, k))
        bot = self.bot
        bot._handle_attachment = mock
        from types import SimpleNamespace
        class St:
            file_name = "sticker.webp"
            mime_type = "image/webp"
        m = SimpleNamespace(sticker=St(), photo=None, video=None, video_note=None,
                            audio=None, voice=None, document=None, animation=None,
                            caption="")
        asyncio.run(bot.on_sticker(None, m))
        self.assertEqual(len(calls), 1)
        # _handle_attachment(client, msg, kind, ext, ...) → ext = a[0][3]
        self.assertEqual(calls[0][0][3], ".webp")


if __name__ == "__main__":
    unittest.main()