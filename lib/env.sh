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

# --- Локализация (общий языковой файл, как в claude_tg_bot/i18n.py) --------
# Строки консоли берём из claude_tg_bot/locale/<lang>.json через python3, чтобы
# bash и Python читали ОДИН источник. Язык — из BOT_LANG в config.env (дефолт en).

# Выбрать язык: BOT_LANG из переданного конфига или из стандартных мест.
lib_lang() {
    local lang="" cfg
    for cfg in "${LIB_CONFIG_ENV:-}" "$HOME/.claude-tg-bot/config.env" "$PROJECT_DIR/config.env"; do
        [ -n "$cfg" ] && [ -f "$cfg" ] || continue
        lang="$(grep -E '^BOT_LANG=' "$cfg" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr '[:upper:]' '[:lower:]' || true)"
        [ -n "$lang" ] && break
    done
    printf '%s' "${lang:-en}"
}

# Достать строку по ключу из locale/<lang>.json; фолбэк на en; пусто, если нет.
lib_msg() {
    local key="${1:-}" lang py base
    [ -n "$key" ] || return 0
    lang="$(lib_lang)"
    base="$PROJECT_DIR"
    py="$(command -v python3 || true)"
    [ -n "$py" ] || py="$VENV/bin/python"
    [ -x "$py" ] || return 0
    "$py" - "$base" "$lang" "$key" <<'PY'
import json, os, sys
base, lang, key = sys.argv[1], sys.argv[2], sys.argv[3]
d = os.path.join(base, 'claude_tg_bot', 'locale')
def load(c):
    try:
        return json.load(open(os.path.join(d, c + '.json'), encoding='utf-8'))
    except Exception:
        return {}
table = load(lang)
if key not in table:
    table = load('en')
print(table.get(key, ''))
PY
}

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
        echo "$(lib_msg env.sh.no_python)"
        echo "$(lib_msg env.sh.no_python_hint) $0"
        exit 1
    fi
    echo "$(lib_msg env.sh.python)" | sed "s|{py}|$PYTHON|; s|{ver}|$("$PYTHON" -c 'import sys; print(".".join(map(str,sys.version_info[:3])))')|"

    # Пересоздаём venv, если он собран старым Python.
    if [ -d "$VENV" ] && ! venv_ok; then
        echo "$(lib_msg env.sh.rebuild_venv)" | sed "s|{old}|$("$VENV/bin/python" --version 2>&1)|; s|{py}|$PYTHON|"
        rm -rf "$VENV"
    fi
    if [ ! -d "$VENV" ]; then
        echo "$(lib_msg env.sh.create_venv)" | sed "s|{venv}|$VENV|; s|{py}|$PYTHON|"
        "$PYTHON" -m venv "$VENV"
    fi

    if ! _deps_ok; then
        echo "$(lib_msg env.sh.install_deps)"
        "$VENV/bin/python" -m pip install --quiet --upgrade pip
        "$VENV/bin/python" -m pip install --quiet -r requirements.txt
    fi
}