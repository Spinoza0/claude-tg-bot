#!/bin/bash
# ================================================================
# setup.sh — интерактивная настройка claude-tg-bot.
#
# Спрашивает настройки по группам и создаёт/редактирует config.env
# в ~/.claude-tg-bot/. Если файл уже есть — делает копию config.env.bak
# и меняет только те значения, которые пользователь ввёл заново
# (Enter — оставить текущее).
#
# Окружение (Python + venv + зависимости) готовит общий модуль lib/env.sh.
#
# Использование:
#   bash setup.sh
# ================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# --- 0. Подготовка окружения (Python + venv + зависимости) ------------------
# Так же, как в claude-tg-bot.sh — через общий модуль lib/env.sh.
source "$DIR/lib/env.sh"
setup_env

# Язык общих консольных сообщений — из config.env (см. lib/lib_msg).
LIB_CONFIG_ENV="${HOME}/.claude-tg-bot/config.env"
[ -f "$LIB_CONFIG_ENV" ] || LIB_CONFIG_ENV="$DIR/config.env"
echo "$(lib_msg setup.sh.launching)"
echo
exec "$VENV/bin/python" claude_tg_bot/setup.py