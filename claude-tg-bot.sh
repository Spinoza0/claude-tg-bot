#!/bin/bash
# ================================================================
# claude-tg-bot.sh — run the Telegram bot with a single command.
#
# What it does:
#   1. Creates and activates the venv (.venv), if absent.
#   2. Installs the dependencies from requirements.txt (if not installed).
#   3. Looks for config.env (in ~/.claude-tg-bot, then next to the script) and
#      checks that it's filled with real values. If the file is absent — works
#      with the default settings.
#   4. Runs the bot (python -m claude_tg_bot).
#      On startup the bot checks running processes: if one is already running —
#      it reports it and won't start a second time.
#
# Usage:
#   bash claude-tg-bot.sh
# ================================================================
set -euo pipefail

# The script directory (works when run from anywhere)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# --- 0. Prepare the environment (Python + venv + deps) ----------------------
# Moved into the shared module lib/env.sh so setup.sh can use it too.
# setup_env() picks Python >=3.10, creates/recreates the venv, installs the
# dependencies. We run the bot via $VENV/bin/python (we don't activate the venv).
source "$DIR/lib/env.sh"
setup_env

# config.env is looked up in two places (by priority, as in config.py):
#   1. ~/.claude-tg-bot/config.env  — the directory that holds the sandbox;
#   2. <script directory>/config.env — next to claude-tg-bot.sh.
# The bot prints PROJECTS_ROOT and SANDBOX_ROOT itself in main — right after
# config.env, so they come consecutively.
CONFIG_ENV="${HOME}/.claude-tg-bot/config.env"
if [ ! -f "$CONFIG_ENV" ]; then
    CONFIG_ENV="$DIR/config.env"
fi

# Show the config path first — it determines both the projects root and the
# sandbox. The bot prints the projects root and the sandbox directory in main
# (right after config.env). We pass the language from this config for the shared
# messages (see lib/lib_msg).
LIB_CONFIG_ENV="$CONFIG_ENV"
echo "$(lib_msg run.sh.config_env)" | sed "s|{path}|$CONFIG_ENV|"

# --- 3. Check config.env — the file is mandatory (in one of the two places) --
if [ ! -f "$CONFIG_ENV" ]; then
    echo "$(lib_msg run.sh.cfg_missing)"
    echo "   - ${HOME}/.claude-tg-bot/config.env"
    echo "   - $DIR/config.env"
    echo ""
    echo "$(lib_msg run.sh.cfg_missing_copy)"
    echo "$(lib_msg run.sh.cfg_copy_cmd)"
    exit 1
else

    # Placeholders that must be replaced.
    # We check the WHOLE lines of the form KEY=PLACEHOLDER (via ^ and $ anchors),
    # so we don't catch substrings — e.g. the number 123456789 inside a real API_HASH.
    UNSET=$(grep -E -e "^API_ID=ЗАМЕНИ_МЕНЯ$" -e "^API_HASH=ЗАМЕНИ_МЕНЯ$" -e "^API_ID=0$" -e "^PHONE=\+7XXXXXXXXXX$" -e "^ALLOWED_USERS=123456789$" -e "^ALLOWED_USERS=$" -e "^PROJECTS_ROOT=$" "$CONFIG_ENV" || true)
    if [ -n "$UNSET" ]; then
        echo "$(lib_msg run.sh.cfg_placeholders)"
        echo "   $UNSET"
        echo ""
        echo "$(lib_msg run.sh.cfg_placeholders_fix)" | sed "s|{path}|$CONFIG_ENV|"
        echo "$(lib_msg run.sh.fix_api)"
        echo "$(lib_msg run.sh.fix_phone)"
        echo "$(lib_msg run.sh.fix_users)"
        echo "$(lib_msg run.sh.fix_projects_root)"
        echo "$(lib_msg run.sh.fix_chat_ids)"
        exit 1
    fi

    # Warning: if ALLOWED_CHAT_IDS is empty, the bot answers only in Saved Messages
    if grep -qE '^ALLOWED_CHAT_IDS=""$|^ALLOWED_CHAT_IDS=$' "$CONFIG_ENV"; then
        echo "$(lib_msg run.sh.chat_ids_empty)"
        echo "$(lib_msg run.sh.chat_ids_empty_hint)"
    fi
fi

# --- 4. Run the bot ---------------------------------------------------------
# The bot checks single-instance on its own (scans running processes): if one is
# already running — it prints it and won't start a second time.
# KEEP_AWAKE (from config.env): if true — keep the laptop awake
# (caffeinate -dimsu) so sleep doesn't drop the network and the bot keeps
# receiving/answering messages. Default false.
# If caffeinate is unavailable (not macOS) — without it.
KEEP_AWAKE="false"
if [ -f "$CONFIG_ENV" ]; then
    KEEP_AWAKE="$(grep -E '^KEEP_AWAKE=' "$CONFIG_ENV" | tail -1 | cut -d= -f2- | tr -d '"' | tr '[:upper:]' '[:lower:]' || true)"
fi
echo "$(lib_msg run.sh.launching)"
# "$@" below — pass through the call arguments (e.g. --log=info) into python -m.
# We run via $VENV/bin/python (we didn't activate the venv in setup_env).
if { [ "$KEEP_AWAKE" = "true" ] || [ "$KEEP_AWAKE" = "1" ] || [ "$KEEP_AWAKE" = "yes" ]; } && command -v caffeinate >/dev/null 2>&1; then
    exec caffeinate -dimsu "$VENV/bin/python" -m claude_tg_bot "$@"
else
    exec "$VENV/bin/python" -m claude_tg_bot "$@"
fi