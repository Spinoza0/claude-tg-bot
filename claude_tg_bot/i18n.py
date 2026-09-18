"""Internationalization: load user-facing strings from a JSON locale file.

One language = one JSON file in ``locale/``. The language code comes from the
``BOT_LANG`` environment variable (set in ``config.env``), defaulting to ``en``.
Both the Python package (through this module) and the bash scripts (through
``lib/env.sh``, via ``python3 -c``) read the same files, so user messages stay
consistent across the whole project.

Only strings shown to a human (Telegram replies, console output) live here.
Logger diagnostics are English-only and stay in the code.

The active dictionary is loaded lazily on first use, so ``config`` has already
run ``load_dotenv`` (which populates ``BOT_LANG``) before messages are built.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

# Location of the locale files, next to this module.
_LOCALE_DIR = Path(__file__).resolve().parent / "locale"

# Name of the env var / config.env key that selects the language.
LANG_ENV = "BOT_LANG"

# Default code used when the env value is empty or unknown.
_DEFAULT_CODE = "en"

# Guard for lazy init and the mutable active dictionary (handler threads).
_LOCK = threading.Lock()
_loaded = False
_LANG = ""
_TRANSLATIONS: dict = {}
_EN: dict = {}


def _load(code: str) -> dict:
    """Load ``locale/<code>.json``, returning {} if the file is absent/broken."""
    path = _LOCALE_DIR / f"{code}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def available_langs() -> list[str]:
    """Language codes with a shipped locale file (e.g. ['en', 'ru'])."""
    try:
        return sorted(p.stem for p in _LOCALE_DIR.glob("*.json"))
    except OSError:
        return []


def _code_for(env_value: str) -> str:
    """Resolve an env value to a shipped language code, defaulting to 'en'."""
    code = (env_value or "").strip().lower()
    if code in available_langs():
        return code
    return _DEFAULT_CODE


def _ensure_loaded() -> None:
    """Initialize dictionaries on first use (after config.env is loaded)."""
    global _loaded, _LANG, _TRANSLATIONS, _EN
    if _loaded:
        return
    with _LOCK:
        if _loaded:
            return
        _LANG = _code_for(os.getenv(LANG_ENV))
        _TRANSLATIONS = _load(_LANG) or _load(_DEFAULT_CODE)
        _EN = _load(_DEFAULT_CODE)
        _loaded = True


def current_lang() -> str:
    """The active language code (e.g. 'en', 'ru')."""
    _ensure_loaded()
    return _LANG


def ensure_available() -> None:
    """Fail fast if no usable language source exists (checked at startup).

    We can start if EITHER the configured language file OR English is present;
    `t()` falls back per-key to English, so a single shipped file is enough.
    But if BOTH are missing we can't produce readable strings at all, and the
    whole project would silently emit bare keys. In that case we stop with a
    hardcoded English error, since we can't rely on localization that is absent.
    """
    _ensure_loaded()
    if not _TRANSLATIONS and not _EN:
        raise RuntimeError(
            f"No language files found in {_LOCALE_DIR}. Cannot start: neither the "
            f"configured language ({_LANG or 'unset'}) nor English (en.json) is "
            "available. Ship at least one locale file (e.g. claude_tg_bot/"
            "locale/en.json)."
        )


def set_lang(code: str) -> str:
    """Switch the active language at runtime and return the resolved code.

    Re-reads the locale file for ``code``. Falls back to the current language
    (the file keeps English as a per-key fallback) if ``code`` is not shipped,
    so an invalid code never breaks messages.
    """
    global _LANG, _TRANSLATIONS
    _ensure_loaded()
    code = (code or "").strip().lower()
    if code not in available_langs():
        return _LANG
    table = _load(code) or _EN
    with _LOCK:
        _LANG = code
        _TRANSLATIONS = table
    return code


def t(key: str, **kwargs) -> str:
    """Translate ``key``, formatting ``{name}`` placeholders from kwargs.

    Missing key -> fall back to English, then to the key itself (never crashes).
    """
    _ensure_loaded()
    with _LOCK:
        value = _TRANSLATIONS.get(key) or _EN.get(key) or key
    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return value
    return value