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

# --- 0. Подготовка окружения (Python + venv + зависимости) ------------------
# Вынесено в общий модуль lib/env.sh, чтобы использовать его и из setup.sh.
# setup_env() выбирает Python >=3.10, создаёт/пересоздаёт venv, ставит
# зависимости. Запускаем бота через $VENV/bin/python (активацию venv не делаем).
source "$DIR/lib/env.sh"
setup_env

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
    UNSET=$(grep -E -e "^API_ID=ЗАМЕНИ_МЕНЯ$" -e "^API_HASH=ЗАМЕНИ_МЕНЯ$" -e "^API_ID=0$" -e "^PHONE=\+7XXXXXXXXXX$" -e "^ALLOWED_USERS=123456789$" -e "^ALLOWED_USERS=$" -e "^PROJECTS_ROOT=$" "$CONFIG_ENV" || true)
    if [ -n "$UNSET" ]; then
        echo "!! config.env содержит незаполненные плейсхолдеры:"
        echo "   $UNSET"
        echo ""
        echo "   Открой $CONFIG_ENV и вставь реальные:"
        echo "     - API_ID и API_HASH (с https://my.telegram.org/apps)"
        echo "     - PHONE (твой номер)"
        echo "     - ALLOWED_USERS (твой telegram user_id)"
        echo "     - PROJECTS_ROOT (корень проектов, где боту разрешено работать)"
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
# KEEP_AWAKE (из config.env): если true — держим макбук бодрствующим
# (caffeinate -dimsu), чтобы при засыпании не отключалась сеть и бот не
# переставал принимать/отвечать на сообщения. По умолчанию false.
# Если caffeinate недоступен (не macOS) — без него.
KEEP_AWAKE="false"
if [ -f "$CONFIG_ENV" ]; then
    KEEP_AWAKE="$(grep -E '^KEEP_AWAKE=' "$CONFIG_ENV" | tail -1 | cut -d= -f2- | tr -d '"' | tr '[:upper:]' '[:lower:]' || true)"
fi
echo "==> Запускаю бота..."
# "$@" внизу — проброс аргументов вызова (напр. --log=info) в python -m.
# Запускаем через $VENV/bin/python (venv не активировали в setup_env).
if { [ "$KEEP_AWAKE" = "true" ] || [ "$KEEP_AWAKE" = "1" ] || [ "$KEEP_AWAKE" = "yes" ]; } && command -v caffeinate >/dev/null 2>&1; then
    exec caffeinate -dimsu "$VENV/bin/python" -m claude_tg_bot "$@"
else
    exec "$VENV/bin/python" -m claude_tg_bot "$@"
fi