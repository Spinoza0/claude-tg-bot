#!/bin/bash
# ================================================================
# setup.sh — интерактивная настройка claude-tg-bot.
#
# Спрашивает настройки по группам и создаёт/редактирует config.env
# в ~/.claude-tg-bot/. Если файл уже есть — делает копию config.env.bak
# и меняет только те значения, которые пользователь ввёл заново
# (Enter — оставить текущее).
#
# Использование:
#   bash setup.sh
# ================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Выбираем подходящий Python (>=3.10), как в claude-tg-bot.sh.
pick_python() {
    local candidates py ver
    if [ -n "${PYTHON:-}" ]; then
        candidates="$PYTHON"
    else
        candidates="python3.13 python3.12 python3.11 python3.10 python3"
    fi
    for py in $candidates; do
        command -v "$py" >/dev/null 2>&1 || continue
        ver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || continue
        if [ "$(printf '%s\n%s\n' "3.10" "$ver" | sort -V | head -n1)" = "3.10" ] || [ "$ver" = "3.10" ]; then
            printf '%s' "$py"
            return 0
        fi
    done
    return 1
}

PYTHON="$(pick_python || true)"
if [ -z "$PYTHON" ]; then
    echo "!! Не найден Python 3.10+. Требуется >=3.10."
    echo "   Установи Python 3.10+ или задай путь: PYTHON=/path/to/python3.13 $0"
    exit 1
fi
echo "==> Python: $PYTHON"
echo "==> Запускаю настройку (claude_tg_bot/setup.py)..."
echo
exec "$PYTHON" claude_tg_bot/setup.py