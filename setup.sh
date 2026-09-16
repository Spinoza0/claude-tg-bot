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

echo "==> Запускаю настройку (claude_tg_bot/setup.py)..."
echo
exec "$VENV/bin/python" claude_tg_bot/setup.py