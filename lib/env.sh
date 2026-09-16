# ================================================================
# lib/env.sh — общая подготовка окружения для bash-скриптов проекта.
#
# Подключается через `source lib/env.sh` из claude-tg-bot.sh и setup.sh.
# Устанавливает PROJECT_DIR (корень репозитория), VENV, и функцию
# setup_env(), которая выбирает подходящий Python (>=3.10), создаёт
# venv и ставит зависимости из requirements.txt (если их нет).
#
# Не применяет `set -euo pipefail` сам — наследует настройки вызывающего,
# чтобы не менять его поведение.
# ================================================================

# Корень проекта — каталог, где лежат lib/ и claude-tg-bot.sh.
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV="$PROJECT_DIR/.venv"

# --- Выбор подходящего Python (>=3.10) -------------------------------------
# Боту и kurigram нужен Python 3.10+. Системный python3 бывает старее,
# поэтому перебираем известные бинарники и берём первый с версией >=3.10.
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
            printf '%s' "$py"
            return 0
        fi
    done
    return 1
}

# Существующий venv подходит, если его python >=3.10.
venv_ok() {
    local v
    v="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || return 1
    [ "$(printf '%s\n%s\n' "3.10" "$v" | sort -V | head -n1)" = "3.10" ]
}

# Существуют ли установленные зависимости бота.
_deps_ok() {
    # Модуль называется python_socks (пакет python-socks), НЕ socks (PySocks):
    # kurigram зависит от python-socks, а PySocks может не стоять — проверяем
    # именно python_socks, иначе проверка падает каждый раз и ставит зависимости вновь.
    "$VENV/bin/python" -c "import pyrogram, python_socks, dotenv" >/dev/null 2>&1
}

# setup_env — полная подготовка: найти Python, создать/пересоздать venv,
# поставить зависимости. Печатает понятные сообщения о каждом шаге.
setup_env() {
    PYTHON="$(pick_python || true)"
    if [ -z "$PYTHON" ]; then
        echo "!! Не найден Python 3.10+. Требуется >=3.10 (нужен для kurigram)."
        echo "   Установи Python 3.10+ или задай путь: PYTHON=/path/to/python3.13 $0"
        exit 1
    fi
    echo "==> Python: $PYTHON ($("$PYTHON" -c 'import sys; print(".".join(map(str,sys.version_info[:3])))'))"

    # Пересоздаём venv, если он собран старым Python.
    if [ -d "$VENV" ] && ! venv_ok; then
        echo "==> venv собран старым Python ($("$VENV/bin/python" --version 2>&1)), удаляю и пересоздаю через $PYTHON"
        rm -rf "$VENV"
    fi
    if [ ! -d "$VENV" ]; then
        echo "==> venv не найден, создаю: $VENV (через $PYTHON)"
        "$PYTHON" -m venv "$VENV"
    fi

    if ! _deps_ok; then
        echo "==> Устанавливаю зависимости из requirements.txt ..."
        "$VENV/bin/python" -m pip install --quiet --upgrade pip
        "$VENV/bin/python" -m pip install --quiet -r requirements.txt
    fi
}