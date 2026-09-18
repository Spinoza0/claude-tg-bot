#!/bin/bash
# ================================================================
# setup.sh — interactive setup for claude-tg-bot.
#
# Asks for settings by groups and creates/edits config.env in
# ~/.claude-tg-bot/. If the file already exists — makes a config.env.bak
# copy and changes only the values entered again
# (Enter — keep the current value).
#
# The environment (Python + venv + dependencies) is prepared by the shared
# module lib/env.sh.
#
# Usage:
#   bash setup.sh
# ================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# --- 0. Prepare the environment (Python + venv + deps) ----------------------
# Same as in claude-tg-bot.sh — via the shared module lib/env.sh.
source "$DIR/lib/env.sh"
setup_env

# Language of the shared console messages — from config.env (see lib/lib_msg).
LIB_CONFIG_ENV="${HOME}/.claude-tg-bot/config.env"
[ -f "$LIB_CONFIG_ENV" ] || LIB_CONFIG_ENV="$DIR/config.env"
echo "$(lib_msg setup.sh.launching)"
echo
exec "$VENV/bin/python" claude_tg_bot/setup.py