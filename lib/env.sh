# ================================================================
# lib/env.sh — shared environment setup for the project's bash scripts.
#
# Sourced via `source lib/env.sh` from claude-tg-bot and claude-tg-bot-setup.
# Sets PROJECT_DIR (repo root), VENV, and the setup_env() function, which
# picks a suitable Python (>=3.10), creates a venv and installs the
# dependencies from requirements.txt (if missing).
#
# Does not apply `set -euo pipefail` itself — it inherits the caller's settings
# so as not to change the caller's behavior.
# ================================================================

# The project root — the directory holding lib/ and claude-tg-bot.
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV="$PROJECT_DIR/.venv"

# --- Localization (shared locale file, as in claude_tg_bot/i18n.py) ---------
# Console strings come from claude_tg_bot/locale/<lang>.json via python3, so that
# bash and Python read ONE source. The language comes from BOT_LANG in config.env
# (default en).

# Pick the language: BOT_LANG from the passed config or from the standard places.
lib_lang() {
    local lang="" cfg
    for cfg in "${LIB_CONFIG_ENV:-}" "$HOME/.claude-tg-bot/config.env" "$PROJECT_DIR/config.env"; do
        [ -n "$cfg" ] && [ -f "$cfg" ] || continue
        lang="$(grep -E '^BOT_LANG=' "$cfg" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr '[:upper:]' '[:lower:]' || true)"
        [ -n "$lang" ] && break
    done
    printf '%s' "${lang:-en}"
}

# Get a string by key from locale/<lang>.json; fall back to en; empty if absent.
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

# --- Pick a suitable Python (>=3.10) ----------------------------------------
# The bot and kurigram need Python 3.10+. The system python3 can be older, so
# we iterate the known binaries and take the first one with a version >=3.10.
pick_python() {
    local candidates py ver
    # If PYTHON is set — use only it (but we still check the version below).
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

# An existing venv is usable if its python is >=3.10.
venv_ok() {
    local v
    v="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || return 1
    [ "$(printf '%s\n%s\n' "3.10" "$v" | sort -V | head -n1)" = "3.10" ]
}

# Whether the bot's dependencies are installed.
_deps_ok() {
    # The module is python_socks (package python-socks), NOT socks (PySocks):
    # kurigram depends on python-socks, while PySocks may be absent — we check
    # python_socks specifically, otherwise the check fails every time and
    # reinstalls the dependencies.
    "$VENV/bin/python" -c "import pyrogram, python_socks, dotenv" >/dev/null 2>&1
}

# setup_env — full preparation: find Python, create/recreate the venv, install
# the dependencies. Prints a clear message about each step.
setup_env() {
    PYTHON="$(pick_python || true)"
    if [ -z "$PYTHON" ]; then
        echo "$(lib_msg env.sh.no_python)"
        echo "$(lib_msg env.sh.no_python_hint) $0"
        exit 1
    fi

    # Recreate the venv if it was built with an older Python.
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