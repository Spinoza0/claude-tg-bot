"""Конфигурация бота.

Все данные для подключения к Telegram и запуска Claude берутся из
переменных окружения (файл config.env). Это позволяет не зашивать секреты в
код. Файл ищется в двух местах (по приоритету):

  1. ~/.claude-tg-bot/config.env        — каталог, где лежит папка sandbox;
  2. <каталог claude-tg-bot.sh>/config.env  — рядом со скриптом запуска.

Если файла нет ни там, ни там — бот завершает работу (см. validate(): бросает
RuntimeError с указанием, где должен лежать config.env).
"""

import os
from pathlib import Path
from typing import Iterable, Optional

from .version import BOT_VERSION

# Корень проекта (родитель пакета claude_tg_bot) — там лежат run.sh и state.json.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _find_config_env(candidates: Optional[Iterable[Path]] = None) -> Optional[Path]:
    """Найти config.env в порядке приоритета.

    Сначала ~/.claude-tg-bot/config.env (каталог, где находится папка sandbox),
    затем <каталог claude-tg-bot.sh>/config.env. Если нет ни там, ни там —
    возвращаем None (бот тогда завершает работу, см. validate()).
    """
    if candidates is None:
        candidates = [
            Path.home() / ".claude-tg-bot" / "config.env",  # каталог папки sandbox
            _PROJECT_ROOT / "config.env",                    # каталог claude-tg-bot.sh
        ]
    for p in candidates:
        if p.is_file():
            return p
    return None


# Все места, где бот ищет config.env (для сообщения об ошибке в validate).
CONFIG_ENV_CANDIDATES = [
    Path.home() / ".claude-tg-bot" / "config.env",
    _PROJECT_ROOT / "config.env",
]


try:
    from dotenv import load_dotenv

    _env_file = _find_config_env()
    if _env_file is not None:
        load_dotenv(_env_file)
        # Путь модуля, чтобы бот мог показать, откуда прочитан конфиг.
        CONFIG_ENV_PATH: Optional[Path] = _env_file
    else:
        # Файла нет ни в одном месте — validate() завершит работу.
        CONFIG_ENV_PATH: Optional[Path] = None
except ImportError:
    # python-dotenv опционален: можно задать переменные вручную
    CONFIG_ENV_PATH: Optional[Path] = None


# ---------------------------------------------------------------------------
# Telegram (MTProto)
# ---------------------------------------------------------------------------

# Получаем из https://my.telegram.org/apps
API_ID: int = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")

# Номер телефона аккаунта (в формате +7XXXXXXXXXX), под которым работает бот
PHONE: str = os.getenv("PHONE", "")

# Имя файла сессии MTProto-клиента (хранится в .session рядом с ботом)
SESSION_NAME: str = os.getenv("SESSION_NAME", "claude-tg-bot")

# MTProto-прокси в формате tg://proxy?server=...&port=...&secret=...
# MTProto-клиент принимает его нативно. Пример (фейковый):
# tg://proxy?server=example.example.com&port=443&secret=0000...
MT_PROXY: str = os.getenv("MT_PROXY", "")

# Пароль облака (двухфакторка) — если есть
CLOUD_PASSWORD: Optional[str] = os.getenv("CLOUD_TOKEN_PASSWORD") or None

def _parse_ids(raw: str) -> set:
    """Парсит список id из строки вида числа, разделённые запятыми.

    Поддерживает значения В КАВЫЧКАХ (одинарных или двойных) и без них,
    положительные и отрицательные числа, пробелы вокруг. Примеры:
        "\"777\",\"-1001234567890\""    -> {777, -1001234567890}
        "777,-100123"                    -> {777, -100123}
        ""                               -> set()
    """
    result = set()
    for part in raw.split(","):
        part = part.strip().strip('"').strip("'").strip()
        # допускаем минус + цифры (отрицательные chat_id группы)
        if part.lstrip("-").isdigit():
            result.add(int(part))
    return result


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# Список telegram user_id (int), которым разрешено писать боту.
# Если пусто — бот не отвечает никому (безопасно).
ALLOWED_USERS = _parse_ids(os.getenv("ALLOWED_USERS", ""))

# Конкретные chat_id, в КОТОРЫХ бот отвечает. Значения можно в кавычках:
#   ALLOWED_CHAT_IDS="-1001234567890","-1009876543210"
# Бот отвечает ТОЛЬКО в этих чатах и только если отправитель в ALLOWED_USERS.
# Пусто = бот не отвечает нигде (закрыт) — намеренно.
ALLOWED_CHAT_IDS = _parse_ids(os.getenv("ALLOWED_CHAT_IDS", ""))


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

# Команда для запуска Claude. По умолчанию — обычный "claude".
# Можно указать другую обёртку (собственную или стороннюю); значение — имя
# исполняемого файла или полный путь, подставляется в argv как есть.
CLAUDE_COMMAND: str = os.getenv("CLAUDE_COMMAND", "claude")

# Режим разрешений (--permission-mode) для неинтерактивного запуска (-p).
# Бот запускает Claude без терминала, поэтому никто не может ответить на
# запрос разрешения — Claude печатает «Что разрешаешь?» и зависает до таймаута.
# Поэтому включаем авто-режим. Валидные значения:
#   bypassPermissions — полный авто (выполняет всё без подтверждений);
#   acceptEdits       — авто для правок файлов, опасные тулзы могут спросить;
#   plan              — только планирует, ничего не выполняет;
#   default           — стандартный (может спросить и зависнуть).
# ВАЖНО: bypassPermissions — доверенный агент, он может делать разрушительные
# действия без подтверждения. Смени на acceptEdits, если нужна осторожность.
CLAUDE_PERMISSION_MODE: str = os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()

# Дополнительные аргументы командной строки, добавляемые к CLAUDE_COMMAND.
# Задаются одной строкой и разбиваются на отдельные аргументы (через shlex).
# Позволяет передать обёртке любые её специфичные параметры. Пример для
# Cline-обёртки: --provider openai-compatible (идентификатор провайдера,
# откуда берётся модель). Для обычного "claude" обычно не требуется и
# оставляется пустым.
COMMAND_ARGS: str = os.getenv("COMMAND_ARGS", "").strip()

# Альтернативный набор аргументов для смены модели при её недоступности.
# Если основная модель не отвечает (API Error / Cannot connect / 502 / 503),
# бот повторяет запрос, подставляя ЭТОТ набор ВМЕСТО COMMAND_ARGS (обычно здесь
# указывают другого провайдера/модель). Если задан — бот чередует базовый и
# альтернативный наборы до исчерпания RETRY_LIMIT. Если пуст — повторяет запрос
# с базовыми аргументами без смены модели.
COMMAND_ARGS_ALTERNATIVE: str = os.getenv("COMMAND_ARGS_ALTERNATIVE", "").strip()

# Системный промпт для Claude (--append-system-prompt). Передаётся модели как
# системная инструкция. Если пусто — флаг не добавляется вовсе, Claude работает
# со стандартным системным промптом.
CLAUDE_SYSTEM_PROMPT: str = os.getenv("CLAUDE_SYSTEM_PROMPT", "").strip()

# Корень, внутри которого боту разрешено создавать/переключать проекты.
# Не даём боту работать с произвольными путями — песочница.
PROJECTS_ROOT: Path = Path(
    os.getenv("PROJECTS_ROOT", str(Path.home() / "projects"))
).resolve()

# Каталог по умолчанию для нового чата, если юзер не переключил проект
DEFAULT_PROJECT: str = os.getenv("DEFAULT_PROJECT", "")

# Максимальная длина промпта (символов), отправляемого в Claude
MAX_PROMPT_LENGTH: int = int(os.getenv("MAX_PROMPT_LENGTH", "8000"))

# Таймаут одного вызова Claude (сек). 0 = без лимита.
CLAUDE_TIMEOUT_SECONDS: int = int(os.getenv("CLAUDE_TIMEOUT_SECONDS", "600"))


# ---------------------------------------------------------------------------
# Единая схема повторов при транзиентных сбоях (подключение, отправка, Claude)
# ---------------------------------------------------------------------------
# Один механизм ретраев на все места: после N-й неудачи пауза растёт как
# min(base * multiplier^(N-1), max). При успехе счётчик сбрасывается к base.
# RETRY_LIMIT — сколько попыток суммарно (включая первую) до отказа.
RETRY_LIMIT: int = int(os.getenv("RETRY_LIMIT", "5"))
RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", "60"))      # сек
RETRY_MAX_DELAY: float = float(os.getenv("RETRY_MAX_DELAY", "300"))       # сек (5 мин)
RETRY_MULTIPLIER: float = float(os.getenv("RETRY_MULTIPLIER", "2.0"))

# Короткие повторы для отправки сообщений/индикаторов в Telegram — секунды,
# чтобы ответ не «висел» долго (это транзиентные меж-DC ошибки, не потеря сети).
MESSAGE_RETRY_LIMIT: int = int(os.getenv("MESSAGE_RETRY_LIMIT", "3"))
MESSAGE_RETRY_DELAY: float = float(os.getenv("MESSAGE_RETRY_DELAY", "2.0"))


# ---------------------------------------------------------------------------
# Управление скачанными вложениями (фото/видео/звук/файл)
# ---------------------------------------------------------------------------
# AUTO_DELETE_MEDIA — автоудалять ли вложение после отправки в claude.
#   true  — удалить (по DELETE_MODE) после обработки.
#   false — НЕ удалять автоматически; очищать только вручную через /clearmedia.
#   По умолчанию false (безопаснее — ничего не теряется без явной команды).
AUTO_DELETE_MEDIA: bool = _env_bool("AUTO_DELETE_MEDIA", False)

# DELETE_MODE — куда удалять вложение (и по /clearmedia, и при автоудалении):
#   trash      — в корзину macOS (восстанавливаемо). Значение по умолчанию.
#   permanent  — удалить навсегда (без возможности восстановить).
DELETE_MODE: str = (os.getenv("DELETE_MODE", "trash").strip().lower() or "trash")

# KEEP_AWAKE — не давать макбуку засыпать, пока бот работает (caffeinate -dimsu).
#   true  — держать систему бодрствующей (иначе при засыпании отключается сеть
#           и бот перестаёт принимать/отвечать на сообщения).
#   false — ничего не менять. По умолчанию выключено (решает пользователь).
#   В claude-tg-bot.sh читается как переменная окружения config.env.
KEEP_AWAKE: bool = _env_bool("KEEP_AWAKE", False)


# ---------------------------------------------------------------------------
# Песочница (@helpbot)
# ---------------------------------------------------------------------------
# SANDBOX_ROOT — абсолютный путь каталога, в котором Claude запускается
# для сообщений @helpbot (запуск песочницы «в любом чате» по упоминанию). Если
# НЕ задан — создаётся и используется песочница в домашней директории:
#   ~/.claude-tg-bot/sandbox
SANDBOX_ROOT: Path = Path(
    os.getenv("SANDBOX_ROOT", str(Path.home() / ".claude-tg-bot" / "sandbox"))
).resolve()

# SANDBOX_COMMAND — строка-триггер запуска песочницы «в любом чате». Сообщение
# обязано начинаться с этой строки (после lstrip), дальше пробел + команда/текст.
# Если не задана или пустая — используется дефолт "@helpbot" (упоминание бота).
# Реальное значение показывается в /help и других справках (см. format_command_hint).
SANDBOX_COMMAND: str = (os.getenv("SANDBOX_COMMAND", "") or "@helpbot").strip()


# ---------------------------------------------------------------------------
# Хранение состояния сессий
# ---------------------------------------------------------------------------

# Файл с состояниями пользователей (активные проекты и т.п.) — в корне проекта.
STATE_FILE: Path = _PROJECT_ROOT / "state.json"


def validate() -> None:
    """Проверить обязательные настройки. Бросает RuntimeError с понятным сообщением."""
    problems = []
    warnings = []
    if CONFIG_ENV_PATH is None:
        problems.append(
            "config.env не найден ни в одном месте. Положи его в один из каталогов:\n  "
            + "\n  ".join(str(p) for p in CONFIG_ENV_CANDIDATES)
            + "\n  (образец: cp config.env.example config.env)"
        )
    if not API_ID:
        problems.append("API_ID не задан (получите на https://my.telegram.org/apps)")
    if not API_HASH:
        problems.append("API_HASH не задан (получите на https://my.telegram.org/apps)")
    if not PHONE:
        problems.append("PHONE не задан (номер аккаунта Telegram)")
    if not os.getenv("PROJECTS_ROOT"):
        problems.append(
            "PROJECTS_ROOT не задан — корень проектов, где боту разрешено работать. "
            "Укажи его в config.env (обязательный параметр)."
        )
    if not ALLOWED_USERS:
        warnings.append("ALLOWED_USERS пуст — бот будет игнорировать всех (закрыт)")
    if not ALLOWED_CHAT_IDS:
        warnings.append(
            "ALLOWED_CHAT_IDS пуст — бот отвечает только в «Избранном» (Saved Messages), "
            "а не в группах. Если нужен другой/несколько чатов — впиши id в ALLOWED_CHAT_IDS (см. README)."
        )
    if problems:
        raise RuntimeError("Ошибка конфигурации:\n  " + "\n  ".join(problems))
    if warnings:
        print("⚠️  Предупреждение:\n  " + "\n  ".join(warnings))