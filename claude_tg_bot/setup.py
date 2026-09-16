"""Интерактивный setup: создание/редактирование config.env (issue #4).

Запускается обёрткой setup.sh из корня проекта. Спрашивает настройки по
группам (обязательные и необязательные), создаёт ~/.claude-tg-bot/config.env.
Если файл уже есть — не затирает, а редактирует: делает копию config.env.bak,
показывает текущее значение (Enter — оставить), замену — по вводу нового.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

# Каталог конфига и файл config.env (живут в ~/.claude-tg-bot/, рядом с sandbox).
_CFG_DIR = Path.home() / ".claude-tg-bot"
_CFG_PATH = _CFG_DIR / "config.env"

# Каждая настройка: (ключ, обязательная?, дефолт, подсказка для вопроса).
# required=True — без значения бот не заработает (API_ID/API_HASH/PHONE/
# ALLOWED_USERS), в вопросе помечается «(обязательно)». required=False можно
# оставить пустым (Enter). Настройки сгруппированы по смыслу.
GROUPS = [
    ("Telegram (обязательные)", [
        ("API_ID", True, "", "ID приложения (my.telegram.org/apps)"),
        ("API_HASH", True, "", "Hash приложения (my.telegram.org/apps)"),
        ("PHONE", True, "", "Номер аккаунта Telegram, напр. +79991234567"),
        ("MT_PROXY", False, "", "MTProto-прокси (tg://proxy?server=..&port=..&secret=..). Пусто — прямое подключение"),
        ("CLOUD_TOKEN_PASSWORD", False, "", "Пароль 2FA (облачный), если включена двухфакторка"),
    ]),
    ("Доступ", [
        ("ALLOWED_USERS", True, "", "user_id тех, кому бот отвечает (через запятую). Пусто — бот закрыт"),
        ("ALLOWED_CHAT_IDS", False, "", "chat_id, где бот отвечает. Пусто — только «Избранное»"),
    ]),
    ("Запуск Claude", [
        ("CLAUDE_COMMAND", True, "claude", "Команда запуска Claude (или обёртки), напр. claude-cline"),
        ("COMMAND_ARGS", False, "", "Доп. аргументы к CLAUDE_COMMAND (напр. --provider openai-compatible)"),
        ("COMMAND_ARGS_ALTERNATIVE", False, "", "Альтернативные args для смены модели при её недоступности"),
        ("CLAUDE_SYSTEM_PROMPT", False, "", "Системный промпт (--append-system-prompt). Пусто — стандартный"),
    ]),
    ("Песочница и проекты", [
        ("SANDBOX_ROOT", False, "", "Каталог песочницы (@helpbot). Пусто — ~/.claude-tg-bot/sandbox"),
        ("PROJECTS_ROOT", True, "", "Корень проектов, где боту разрешено работать"),
    ]),
]

# Настройки, которые не спрашиваются (у них осмысленные дефолты) — пишем как есть.
DEFAULTS = {
    "SANDBOX_COMMAND": "@helpbot",
    "CLAUDE_PERMISSION_MODE": "bypassPermissions",
    "CLAUDE_TIMEOUT_SECONDS": "600",
    "MAX_PROMPT_LENGTH": "8000",
    "RETRY_LIMIT": "5",
    "RETRY_BASE_DELAY": "60",
    "RETRY_MAX_DELAY": "300",
    "RETRY_MULTIPLIER": "2.0",
    "MESSAGE_RETRY_LIMIT": "3",
    "MESSAGE_RETRY_DELAY": "2.0",
    "AUTO_DELETE_MEDIA": "false",
    "DELETE_MODE": "trash",
    "KEEP_AWAKE": "false",
}


def _strip_inline_comment(val: str) -> str:
    """Убрать хвост ' # ...' из значения, если он вне кавычек."""
    in_quote = False
    quote_char = ""
    for i, ch in enumerate(val):
        if ch in ('"', "'"):
            if not in_quote:
                in_quote, quote_char = True, ch
            elif ch == quote_char:
                in_quote = False
        elif ch == "#" and not in_quote and (i == 0 or val[i - 1].isspace()):
            return val[:i].rstrip()
    return val


def _load_existing() -> dict[str, str]:
    """Прочитать текущие значения config.env (если файл есть)."""
    result: dict[str, str] = {}
    if not _CFG_PATH.is_file():
        return result
    for line in _CFG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = _strip_inline_comment(val.strip()).strip().strip('"').strip("'")
        result[key] = val
    return result


def _sanitize(value: str) -> str:
    """Подготовить значение для записи: обернуть в кавычки при спецсимволах."""
    value = value.strip()
    if value and any(ch in value for ch in ' "#\''):
        return f'"{value}"'
    return value


def _ask(key: str, hint: str, current: str, default: str, required: bool) -> str:
    """Задать вопрос по параметру, вернуть значение.

    key — имя переменной (напр. API_ID), показывается в вопросе; hint —
    пояснение. current — текущее (редактирование), default — дефолт (создание).
    Enter — оставить current (если есть) или default. Возвращает значение.
    """
    shown = current if current else default
    tag = "(обязательно)" if required else "(необязательно)"
    if shown:
        prompt = f"{key}: {hint} {tag} [текущее: {shown!r}]. Enter = оставить: "
    else:
        prompt = f"{key}: {hint} {tag}. Enter = пропустить: "
    while True:
        try:
            inp = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПрервано. Ничего не сохранено.")
            raise SystemExit(1)
        if inp == "":
            return shown
        return inp


def _render_lines(values: dict[str, str]) -> list[str]:
    """Собрать строки config.env по группам, с подсказками-комментариями."""
    lines = [
        "# config.env — создан скриптом setup (claude_tg_bot/setup.py).",
        "# Полный список настроек и пояснения — см. config.env.example.",
        "",
    ]
    for title, settings in GROUPS:
        lines.append(f"# --- {title} " + "-" * max(1, 48 - len(title)))
        for key, _req, default, hint in settings:
            val = _sanitize(values.get(key, default))
            lines.append(f"# {hint}")
            lines.append(f"{key}={val}")
        lines.append("")
    lines.append("# --- Прочее (умолчания) " + "-" * 30)
    for key, val in DEFAULTS.items():
        lines.append(f"{key}={_sanitize(val)}")
    lines.append("")
    return lines


def _write(path: Path, values: dict[str, str]) -> None:
    """Написать config.env, сделав .bak, если файл уже существует."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
        print(f"   Создана резервная копия: {bak}")
    path.write_text("\n".join(_render_lines(values)) + "\n", encoding="utf-8")
    print(f"   Записан: {path}")


def main() -> None:
    print("Настройка claude-tg-bot.\n")

    existing = _load_existing()
    if existing:
        print(f"Найден существующий {_CFG_PATH}. Отредактирую его (Enter — оставить текущее).\n")
    else:
        print(f"Создам новый {_CFG_PATH}.\n")

    values: dict[str, str] = {}
    for title, settings in GROUPS:
        print(f"\n=== {title} ===")
        for key, required, default, hint in settings:
            current = existing.get(key, "")
            values[key] = _ask(key, hint, current, default, required)

    values.update(DEFAULTS)

    # Проверка обязательных параметров: если какой-то пуст — не сохраняем,
    # а просим заполнить (иначе бот не запустится).
    missing = [k for g in GROUPS for k, req, _d, _h in g[1]
               if req and not values.get(k)]
    if missing:
        print("\n❌ Не заполнены обязательные параметры: " + ", ".join(missing))
        print("   Ничего не сохранено. Перезапусти setup.sh и заполни их.")
        raise SystemExit(1)

    print(f"\n--- Итог: {_CFG_PATH}")
    _write(_CFG_PATH, values)
    print("\nГотово. Дальше:")
    print("  1. Запусти: bash claude-tg-bot.sh")
    print("  2. При первом входе MTProto-клиент запросит код подтверждения из Telegram.")


if __name__ == "__main__":
    main()