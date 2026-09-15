#!/bin/bash
# ================================================================
# claude-tg-bot.sh — запуск Telegram-бота одной командой.
#
# Что делает:
#   1. Создаёт и активирует venv (.venv), если его нет.
#   2. Устанавливает зависимости из requirements.txt (если не установлены).
#   3. Ищет config.env (в ~/.claude-tg-bot, затем рядом со скриптом) и
#      проверяет, что он заполнен реальными значениями. Если файла нет —
#      работает с настройками по умолчанию.
#   4. Запускает бота (python -m claude_tg_bot).
#      Бот при старте проверяет запущенные процессы: если уже работает —
#      сообщит об этом и второй раз не стартует.
#
# Использование:
#   bash claude-tg-bot.sh
# ================================================================
set -euo pipefail

# Директория скрипта (работает при запуске откуда угодно)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# --- 0. Выбираем подходящий Python (>=3.10): PYTHON из env, иначе автоподбор.
# ---------------------------------------------------------------------------
# Боту и kurigram нужен Python 3.10+. Системный python3 бывает старее (напр.
# 3.9), поэтому перебираем известные бинарники и берём первый с версией >=3.10.
pick_python() {
    local candidates py ver
    # Если PYTHON задан — только он (но проверим версию ниже).
    if [ -n "${PYTHON:-}" ]; then
        candidates="$PYTHON"
    else
        candidates="python3.13 python3.12 python3.11 python3.10 python3"
    fi
    for py in $candidates; do
        command -v "$py" >/dev/null 2>&1 || continue
        ver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || continue
        if [ "$(printf '%s\n%s\n' "3.10" "$ver" | sort -V | head -n1)" = "3.10" ] || [ "$ver" = "3.10" ]; then
            # ver >= 3.10
            printf '%s' "$py"
            return 0
        fi
    done
    return 1
}

PYTHON="$(pick_python || true)"
if [ -z "$PYTHON" ]; then
    echo "!! Не найден Python 3.10+. Требуется >=3.10 (нужен для kurigram)."
    echo "   Установи Python 3.10+ или задай путь: PYTHON=/path/to/python3.13 $0"
    exit 1
fi
echo "==> Python: $PYTHON ($("$PYTHON" -c 'import sys; print(".".join(map(str,sys.version_info[:3])))'))"

# config.env ищется в двух местах (по приоритету, как в config.py):
#   1. ~/.claude-tg-bot/config.env  — каталог, где лежит папка sandbox;
#   2. <каталог скрипта>/config.env  — рядом с claude-tg-bot.sh.
# Корень проектов (PROJECTS_ROOT) и каталог песочницы (SANDBOX_ROOT) печатает
# сам бот в main — сразу после config.env, чтобы они шли подряд.
CONFIG_ENV="${HOME}/.claude-tg-bot/config.env"
if [ ! -f "$CONFIG_ENV" ]; then
    CONFIG_ENV="$DIR/config.env"
fi

# Путь к конфигу показываем первым — он определяет и корень проектов, и песочницу.
# Корень проектов и каталог песочницы печатает сам бот в main (сразу после config.env).
echo "==> config.env: $CONFIG_ENV"

# --- 1. Создаём venv выбранным Python, либо пересоздаём, если он старой версии.
# ---------------------------------------------------------------------------
VENV="$DIR/.venv"

venv_ok() {
    # Существующий venv подходит, если его python >=3.10.
    local v
    v="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || return 1
    [ "$(printf '%s\n%s\n' "3.10" "$v" | sort -V | head -n1)" = "3.10" ]
}

if [ -d "$VENV" ] && ! venv_ok; then
    echo "==> venv собран старым Python ($("$VENV/bin/python" --version 2>&1)), удаляю и пересоздаю через $PYTHON"
    rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
    echo "==> venv не найден, создаю: $VENV (через $PYTHON)"
    "$PYTHON" -m venv "$VENV"
fi

# --- 2. Активируем venv и проверяем наличие зависимостей --------------------
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# Внимание: модуль называется python_socks (пакет python-socks), НЕ socks (PySocks).
# kurigram зависит от python-socks, а PySocks может не стоять — поэтому проверяем
# именно python_socks, иначе проверка падает каждый раз и ставит зависимости вновь.
if ! python -c "import pyrogram, python_socks, dotenv" >/dev/null 2>&1; then
    echo "==> Устанавливаю зависимости из requirements.txt ..."
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet -r requirements.txt
fi

# --- 3. Проверка config.env — файл обязателен (в одном из двух мест) -------
if [ ! -f "$CONFIG_ENV" ]; then
    echo "!! config.env не найден ни в одном из двух мест:"
    echo "   - ${HOME}/.claude-tg-bot/config.env"
    echo "   - $DIR/config.env"
    echo ""
    echo "   Скопируй пример и заполни секреты:"
    echo "   cp config.env.example config.env"
    exit 1
else

    # Плейсхолдеры, которые надо обязательно заменить.
    # Проверяем ЦЕЛИКОМ строки вида КЛЮЧ=ПЛЕЙСХОЛДЕР (по якорям ^ и $),
    # чтобы не ловить подстроки — например, число 123456789 внутри реального API_HASH.
    UNSET=$(grep -E -e "^API_ID=ЗАМЕНИ_МЕНЯ$" -e "^API_HASH=ЗАМЕНИ_МЕНЯ$" -e "^API_ID=0$" -e "^PHONE=\+7XXXXXXXXXX$" -e "^ALLOWED_USERS=123456789$" -e "^ALLOWED_USERS=$" "$CONFIG_ENV" || true)
    if [ -n "$UNSET" ]; then
        echo "!! config.env содержит незаполненные плейсхолдеры:"
        echo "   $UNSET"
        echo ""
        echo "   Открой $CONFIG_ENV и вставь реальные:"
        echo "     - API_ID и API_HASH (с https://my.telegram.org/apps)"
        echo "     - PHONE (твой номер)"
        echo "     - ALLOWED_USERS (твой telegram user_id)"
        echo "     - ALLOWED_CHAT_IDS (id чата, где бот отвечает — см. README)"
        exit 1
    fi

    # Предупреждение: если ALLOWED_CHAT_IDS пуст, бот ответит в «Избранном»
    if grep -qE '^ALLOWED_CHAT_IDS=""$|^ALLOWED_CHAT_IDS=$' "$CONFIG_ENV"; then
        echo "ℹ️  ALLOWED_CHAT_IDS пуст — бот будет отвечать только в «Избранном»."
        echo "   (в группах отвечать не будет). Другой чат — впиши id в config.env."
    fi
fi

# --- 4. Запуск бота --------------------------------------------------------
# Бот сам проверяет single-instance (сканирует запущенные процессы): если
# уже работает — напечатает об этом и второй раз не стартует.
echo "==> Запускаю бота..."
exec python -m claude_tg_bot