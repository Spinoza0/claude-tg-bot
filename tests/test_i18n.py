"""Юнит-тесты i18n: загрузка языкового файла, фолбэк, переключение на лету.

Осторожно с глобальным состоянием: i18n инициализируется лениво и кэширует
словарь. Каждый тест пересоздаёт модуль и восстанавливает окружение (BOT_LANG
без побочных эффектов для последующих тестовых файлов).
"""

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from claude_tg_bot import i18n


def _hard_reset(value=None):
    """Сбросить живую инициализацию i18n и задать BOT_LANG (None = убрать)."""
    if value is None:
        os.environ.pop("BOT_LANG", None)
    else:
        os.environ["BOT_LANG"] = value
    import claude_tg_bot.i18n as m
    m._loaded = False
    m._LANG = ""
    m._TRANSLATIONS = {}
    m._EN = {}
    importlib.reload(m)
    return m


class I18nTestCase(unittest.TestCase):
    def setUp(self):
        _hard_reset(None)

    def tearDown(self):
        # Очищаем, чтобы не влиять на следующие тестовые файлы в том же процессе.
        _hard_reset(None)


class TestLangResolution(I18nTestCase):
    """Разрешение языка из BOT_LANG и дефолт при пустом/неизвестном."""

    def test_default_when_unset(self):
        m = _hard_reset(None)
        self.assertEqual(m.current_lang(), "en")

    def test_resolves_shipped_lang(self):
        m = _hard_reset("ru")
        self.assertEqual(m.current_lang(), "ru")

    def test_falls_back_to_en_when_unknown(self):
        m = _hard_reset("de")
        self.assertEqual(m.current_lang(), "en")

    def test_translation_present(self):
        m = _hard_reset("en")
        self.assertEqual(m.t("cmd.no_project"), "Choose a project with /list or create a new one with /new.")


class TestRuntimeSwitch(I18nTestCase):
    """set_lang меняет язык на лету, невалидный код сохраняет текущий."""

    def test_switch_to_ru(self):
        before = i18n.t("cmd.no_project")
        self.assertIn("Choose a project", before)
        code = i18n.set_lang("ru")
        self.assertEqual(code, "ru")
        after = i18n.t("cmd.no_project")
        self.assertIn("Выбери проект", after)

    def test_invalid_keeps_current(self):
        self.assertEqual(i18n.current_lang(), "en")
        code = i18n.set_lang("xx")
        self.assertEqual(code, "en")       # не сменился
        self.assertEqual(i18n.current_lang(), "en")

    def test_missing_key_falls_back_to_key(self):
        # Несуществующий ключ возвращает сам ключ (не падает).
        self.assertEqual(i18n.t("no.such.key"), "no.such.key")


class TestAvailableLangs(I18nTestCase):
    def test_ships_en_and_ru(self):
        langs = i18n.available_langs()
        self.assertIn("en", langs)
        self.assertIn("ru", langs)


class TestEnsureAvailable(I18nTestCase):
    """ensure_available(): fail fast only when neither language nor en exists."""

    def _fresh_with_locale(self, lang, files: dict[str, dict]):
        """Reset i18n, then point _LOCALE_DIR at a temp dir with the given files.

        Load order matters: _hard_reset reloads the module (resetting _LOCALE_DIR
        to the real one), so the temp override must be applied AFTER the reset.
        """
        m = _hard_reset(lang)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        for code, table in files.items():
            (d / f"{code}.json").write_text(json.dumps(table), encoding="utf-8")
        m._LOCALE_DIR = d
        return m

    def test_ok_when_only_en(self):
        self._fresh_with_locale(None, {"en": {"cmd.no_project": "No project"}})
        i18n.ensure_available()  # not expected to raise

    def test_ok_when_configured_lang_only_no_en(self):
        # No en.json, but the configured language file is present — allowed.
        m = self._fresh_with_locale("ru", {"ru": {"cmd.no_project": "Нет проекта"}})
        self.assertEqual(m.current_lang(), "ru")
        i18n.ensure_available()  # not expected to raise

    def test_ok_when_en_missing_but_configured_present(self):
        # en.json missing, the configured (ru) present — must NOT raise.
        self._fresh_with_locale("ru", {"ru": {"cmd.no_project": "Нет проекта"}})
        i18n.ensure_available()

    def test_fails_when_configured_missing_and_en_missing(self):
        # Configured "de" not shipped -> resolves to en (also missing) -> no source.
        self._fresh_with_locale("de", {"ru": {"cmd.no_project": "Нет проекта"}})
        with self.assertRaises(RuntimeError):
            i18n.ensure_available()

    def test_fails_when_none_exist_and_lang_unset(self):
        self._fresh_with_locale(None, {})
        with self.assertRaises(RuntimeError):
            i18n.ensure_available()

    def test_error_message_is_english_hardcoded(self):
        self._fresh_with_locale(None, {})
        with self.assertRaises(RuntimeError) as ctx:
            i18n.ensure_available()
        # The message must be plain English (not a bare i18n key), since no locale exists.
        self.assertNotIn("cfg_missing", str(ctx.exception))
        self.assertIn("No language files found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()