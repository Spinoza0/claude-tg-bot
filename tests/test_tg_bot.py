"""Юнит-тесты чистой логики бота (без Telegram/сети).

Покрываем только детерминированные функции: парсинг @helpbot, определение
расширения вложения, отправку с ретраями, маршрутизацию вложений. Импортируем
нужные модули из пакета claude_tg_bot.
"""

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import handlers, media, reply, sandbox  # noqa: E402


class TestAgentParsing(unittest.TestCase):
    """Парсинг сообщений @helpbot."""

    @classmethod
    def setUpClass(cls):
        # В тестовой среде SANDBOX_COMMAND может быть пуст (нет config.env).
        # Задаём триггер явно, чтобы тестировать разбор @helpbot (см. config).
        sandbox.SANDBOX_PREFIX = "@helpbot"

    def test_is_sandbox_message_true(self):
        self.assertTrue(sandbox._is_sandbox_message("@helpbot"))
        self.assertTrue(sandbox._is_sandbox_message("@helpbot курс доллара"))
        self.assertTrue(sandbox._is_sandbox_message("  @helpbot /status"))

    def test_is_sandbox_message_false(self):
        # Не путать с похожими строками и старым /agent
        self.assertFalse(sandbox._is_sandbox_message("@helpbotxyz"))
        self.assertFalse(sandbox._is_sandbox_message("@helpbotfoo bar"))
        self.assertFalse(sandbox._is_sandbox_message("просто текст"))
        self.assertFalse(sandbox._is_sandbox_message("/status"))
        self.assertFalse(sandbox._is_sandbox_message("/agent"))
        self.assertFalse(sandbox._is_sandbox_message(""))

    def test_strip_sandbox_prefix(self):
        # Срезаем @helpbot и ВСЕ пробелы после него
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot курс"), "курс")
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot /status"), "/status")
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot   много пробелов"), "много пробелов")
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot\tтаб"), "таб")
        # @helpbot без хвоста → пустая строка
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot"), "")
        self.assertEqual(sandbox._strip_sandbox_prefix("@helpbot   "), "")


class _FakeMedia:
    """Имитация объекта вложения с file_name/mime_type."""

    def __init__(self, file_name=None, mime_type=None):
        self.file_name = file_name
        self.mime_type = mime_type


class TestMediaExt(unittest.TestCase):
    """Определение расширения вложения."""

    def test_extension_from_filename(self):
        self.assertEqual(media._media_ext(_FakeMedia("clip.mp4", "video/mp4")), ".mp4")
        self.assertEqual(media._media_ext(_FakeMedia("pic.JPG", "image/jpeg")), ".jpg")
        self.assertEqual(media._media_ext(_FakeMedia("song.MP3", "audio/mpeg")), ".mp3")

    def test_extension_from_mime(self):
        # Нет file_name (как у Voice) — берём из mime_type
        self.assertEqual(media._media_ext(_FakeMedia(None, "video/webm")), ".webm")
        self.assertEqual(media._media_ext(_FakeMedia(None, "audio/ogg")), ".ogg")
        self.assertEqual(media._media_ext(_FakeMedia(None, "application/pdf")), ".pdf")

    def test_extension_default_bin(self):
        # Нет ни file_name, ни известного mime → .bin
        self.assertEqual(media._media_ext(_FakeMedia(None, None)), ".bin")

    def test_extension_filename_without_dot(self):
        # file_name без точки — откатываемся к mime
        self.assertEqual(media._media_ext(_FakeMedia("noext", "image/png")), ".png")


class TestSniffImageExt(unittest.TestCase):
    """Определение расширения изображения по магическим байтам."""

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
        self.assertEqual(media._sniff_image_ext(p), ".png")

    def test_jpg(self):
        p = self._write(b"\xff\xd8\xff\xe0rest")
        self.assertEqual(media._sniff_image_ext(p), ".jpg")

    def test_webp(self):
        p = self._write(b"RIFFxxxxWEBPdata")
        self.assertEqual(media._sniff_image_ext(p), ".webp")

    def test_gif(self):
        p = self._write(b"GIF89a....")
        self.assertEqual(media._sniff_image_ext(p), ".gif")

    def test_unknown_fallback_jpg(self):
        p = self._write(b"not-an-image-at-all")
        self.assertEqual(media._sniff_image_ext(p), ".jpg")


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

    def test_success_after_transient_errors(self):
        m = _FlakyMessage(fails=2)
        res = asyncio.run(reply._send_with_retry(m, "ответ", attempts=3, delay=0))
        self.assertIsNotNone(res)
        self.assertEqual(m.calls, 3)
        self.assertEqual(m.replied, "🤖 ответ")

    def test_all_fail_returns_none(self):
        m = _FlakyMessage(fails=99)
        res = asyncio.run(reply._send_with_retry(m, "текст", attempts=2, delay=0))
        self.assertIsNone(res)
        self.assertEqual(m.calls, 2)


class TestSplitRouting(unittest.TestCase):
    """Раздельные обработчики (фото/видео/аудио/файл) маршрутизируются верно.

    При вызове on_photo должно идти в _handle_attachment со sniff_ext=True,
    а on_video/on_audio/on_document — со своим расширением из _media_ext.
    Проверяем через делегирование в _handle_attachment (мокаем его).
    """

    def _make_msg(self, photo=None, video=None, audio=None, voice=None, document=None):
        m = SimpleNamespace()
        m.photo, m.video, m.audio, m.voice, m.document = photo, video, audio, voice, document
        m.caption = ""
        return m

    def _mock_handle(self):
        calls = []
        async def mock(*a, **k):
            calls.append((a, k))
        handlers._handle_attachment = mock
        return calls

    def test_photo_uses_sniff(self):
        calls = self._mock_handle()
        m = self._make_msg(photo=_FakeMedia())
        asyncio.run(handlers.on_photo(None, m))
        self.assertEqual(len(calls), 1)
        _, kw = calls[0]
        self.assertEqual(kw["sniff_ext"], True)

    def test_video_ext(self):
        calls = self._mock_handle()
        m = self._make_msg(video=_FakeMedia("c.mp4", "video/mp4"))
        asyncio.run(handlers.on_video(None, m))
        # _handle_attachment(client, msg, kind, ext, ...) → a[0]=(None,m,'видео','.mp4',...)
        self.assertEqual(calls[0][0][3], ".mp4")

    def test_audio_ext(self):
        calls = self._mock_handle()
        m = self._make_msg(audio=_FakeMedia("s.mp3", "audio/mpeg"))
        asyncio.run(handlers.on_audio(None, m))
        self.assertEqual(calls[0][0][3], ".mp3")

    def test_document_ext(self):
        calls = self._mock_handle()
        m = self._make_msg(document=_FakeMedia("doc.pdf", "application/pdf"))
        asyncio.run(handlers.on_document(None, m))
        self.assertEqual(calls[0][0][3], ".pdf")

    def test_video_note_as_video(self):
        # Кружок (video_note) уходит как видео, ext .mp4 по умолчанию
        calls = self._mock_handle()
        m = SimpleNamespace(video_note=_FakeMedia(None, "video/mp4"),
                            photo=None, video=None, audio=None, voice=None,
                            document=None, caption="")
        asyncio.run(handlers.on_video_note(None, m))
        # _handle_attachment(client, msg, kind, ext, ...) → ext = a[0][3]
        self.assertEqual(calls[0][0][3], ".mp4")
        # kind = 'видео-кружок'
        self.assertEqual(calls[0][0][2], "видео-кружок")


class TestTextualMediaPrompt(unittest.TestCase):
    """_textual_media_prompt формирует описание для опроса/гео/контакта."""

    def _msg(self, **kw):
        d = dict(photo=None, video=None, video_note=None, audio=None, voice=None,
                 document=None, animation=None, sticker=None, contact=None,
                 location=None, venue=None, poll=None, dice=None, game=None,
                 web_app_data=None, paid_media=None)
        d.update(kw)
        return SimpleNamespace(**d)

    def _poll(self, question, options):
        return SimpleNamespace(question=SimpleNamespace(text=question),
                               options=[SimpleNamespace(text=o) for o in options])

    def test_poll(self):
        p = self._poll("Как дела?", ["Отлично", "Норм"])
        prompt = media._textual_media_prompt(self._msg(poll=p))
        self.assertIn("Как дела?", prompt)
        self.assertIn("Отлично", prompt)
        self.assertIn("Норм", prompt)

    def test_contact(self):
        c = SimpleNamespace(first_name="Иван", last_name="Иванов", phone_number="+79991234567")
        prompt = media._textual_media_prompt(self._msg(contact=c))
        self.assertIn("Иван", prompt)
        self.assertIn("79991234567", prompt)

    def test_location(self):
        loc = SimpleNamespace(latitude=55.75, longitude=37.61)
        prompt = media._textual_media_prompt(self._msg(location=loc))
        self.assertIn("55.75", prompt)

    def test_none_for_other(self):
        self.assertIsNone(media._textual_media_prompt(self._msg(photo=object())))


class TestStickerAsFile(unittest.TestCase):
    """Стикер уходит как файл-вложение в claude (а не «не могу обработать»)."""

    def test_sticker_routes_to_attachment(self):
        calls = []
        async def mock(*a, **k):
            calls.append((a, k))
        handlers._handle_attachment = mock
        class St:
            file_name = "sticker.webp"
            mime_type = "image/webp"
        m = SimpleNamespace(sticker=St(), photo=None, video=None, video_note=None,
                            audio=None, voice=None, document=None, animation=None,
                            caption="")
        asyncio.run(handlers.on_sticker(None, m))
        self.assertEqual(len(calls), 1)
        # _handle_attachment(client, msg, kind, ext, ...) → ext = a[0][3]
        self.assertEqual(calls[0][0][3], ".webp")


if __name__ == "__main__":
    unittest.main()