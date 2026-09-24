"""Bot configuration.

All data for connecting to Telegram and launching Claude comes from environment
variables (the config.env file). This keeps secrets out of the code. The file is
looked up in two places (by priority):

  1. ~/.claude-tg-bot/config.env        — the folder that holds the sandbox;
  2. <claude-tg-bot.sh folder>/config.env  — next to the launch script.

If the file is found in neither, the bot exits (see validate(): it raises a
RuntimeError stating where config.env should be).
"""

import os
from pathlib import Path
from typing import Iterable, Optional

from . import i18n
from .version import BOT_VERSION

# Project root (parent of the claude_tg_bot package) — where run.sh and state.json live.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _find_config_env(candidates: Optional[Iterable[Path]] = None) -> Optional[Path]:
    """Find config.env by priority.

    First ~/.claude-tg-bot/config.env (the folder holding the sandbox), then
    <claude-tg-bot.sh folder>/config.env. If neither — return None (the bot then
    exits; see validate()).
    """
    if candidates is None:
        candidates = [
            Path.home() / ".claude-tg-bot" / "config.env",  # sandbox folder
            _PROJECT_ROOT / "config.env",                    # claude-tg-bot.sh folder
        ]
    for p in candidates:
        if p.is_file():
            return p
    return None


# All the places the bot looks for config.env (for the error message in validate).
CONFIG_ENV_CANDIDATES = [
    Path.home() / ".claude-tg-bot" / "config.env",
    _PROJECT_ROOT / "config.env",
]


try:
    from dotenv import load_dotenv

    _env_file = _find_config_env()
    if _env_file is not None:
        load_dotenv(_env_file)
        # The module path, so the bot can show where the config was read from.
        CONFIG_ENV_PATH: Optional[Path] = _env_file
    else:
        # The file is missing everywhere — validate() will stop the run.
        CONFIG_ENV_PATH: Optional[Path] = None
except ImportError:
    # python-dotenv is optional: variables can be set manually
    CONFIG_ENV_PATH: Optional[Path] = None


# ---------------------------------------------------------------------------
# Telegram (MTProto)
# ---------------------------------------------------------------------------

# Get these from https://my.telegram.org/apps
API_ID: int = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")

# Phone number of the account the bot runs under (format +7XXXXXXXXXX)
PHONE: str = os.getenv("PHONE", "")

# MTProto client session file name (stored in .session next to the bot)
SESSION_NAME: str = os.getenv("SESSION_NAME", "claude-tg-bot")

# MTProto proxy in the form tg://proxy?server=...&port=...&secret=...
# The MTProto client accepts it natively. Example (fake):
# tg://proxy?server=example.example.com&port=443&secret=0000...
MT_PROXY: str = os.getenv("MT_PROXY", "")

# Cloud password (two-factor) — if any
CLOUD_PASSWORD: Optional[str] = os.getenv("CLOUD_TOKEN_PASSWORD") or None

def _parse_ids(raw: str) -> set:
    """Parse an id list from a string of comma-separated numbers.

    Handles values IN QUOTES (single or double) and without them, positive and
    negative numbers, surrounding spaces. Examples:
        "\"777\",\"-1001234567890\""    -> {777, -1001234567890}
        "777,-100123"                    -> {777, -100123}
        ""                               -> set()
    """
    result = set()
    for part in raw.split(","):
        part = part.strip().strip('"').strip("'").strip()
        # allow a minus + digits (negative group chat_id)
        if part.lstrip("-").isdigit():
            result.add(int(part))
    return result


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# List of telegram user_id (int) allowed to write to the bot.
# If empty — the bot answers nobody (safe).
ALLOWED_USERS = _parse_ids(os.getenv("ALLOWED_USERS", ""))

# The specific chat_id the bot answers IN. Values can be in quotes:
#   ALLOWED_CHAT_IDS="-1001234567890","-1009876543210"
# The bot answers ONLY in these chats and only if the sender is in ALLOWED_USERS.
# Empty = the bot answers nowhere (closed) — deliberately.
ALLOWED_CHAT_IDS = _parse_ids(os.getenv("ALLOWED_CHAT_IDS", ""))


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

# Command to launch Claude. By default the plain "claude".
# Another wrapper (own or third-party) can be set; the value is the executable
# name or a full path, substituted into argv as-is.
CLAUDE_COMMAND: str = os.getenv("CLAUDE_COMMAND", "claude")

# Permission mode (--permission-mode) for the non-interactive launch (-p).
# The bot runs Claude without a terminal, so nobody can answer a permission
# prompt — Claude prints "What do you allow?" and hangs until the timeout.
# That's why we enable the auto mode. Valid values:
#   bypassPermissions — full auto (does everything without confirmations);
#   acceptEdits       — auto for file edits, dangerous tools may ask;
#   plan              — only plans, executes nothing;
#   default           — standard (may ask and hang).
# IMPORTANT: bypassPermissions is a trusted agent; it can do destructive actions
# without confirmation. Switch to acceptEdits if you need caution.
CLAUDE_PERMISSION_MODE: str = os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()

# Extra command-line arguments added to CLAUDE_COMMAND.
# Given as one string and split into separate arguments (via shlex).
# Lets you pass any wrapper-specific parameters. Example for the Cline wrapper:
# --provider openai-compatible (the provider id the model comes from). For the
# plain "claude" it's usually not needed and left empty.
COMMAND_ARGS: str = os.getenv("COMMAND_ARGS", "").strip()

# Alternative argument set to switch the model when it's unavailable.
# If the main model doesn't answer (API Error / Cannot connect / 502 / 503),
# the bot retries the request substituting THIS set INSTEAD OF COMMAND_ARGS
# (usually another provider/model goes here). If set, the bot alternates the base
# and alternative sets until RETRY_LIMIT is exhausted. If empty — it retries with
# the base arguments without a model switch.
COMMAND_ARGS_ALTERNATIVE: str = os.getenv("COMMAND_ARGS_ALTERNATIVE", "").strip()

# System prompt for Claude (--append-system-prompt). Passed to the model as a
# system instruction. If empty — the flag is not added at all and Claude works
# with its standard system prompt.
CLAUDE_SYSTEM_PROMPT: str = os.getenv("CLAUDE_SYSTEM_PROMPT", "").strip()

# ATTACHMENT_SYSTEM_PROMPT — how to mark files Claude creates as attachments to
# send back (save the result into .claude_tg_bot_media and print a "[FILE: path]"
# marker line). Added to the Claude call right after CLAUDE_SYSTEM_PROMPT as a
# second --append-system-prompt (both reach the model). An empty/absent value
# falls back to the built-in instruction below (attachments stay on); set the
# text to override it.
ATTACHMENT_SYSTEM_PROMPT: str = (
    os.getenv("ATTACHMENT_SYSTEM_PROMPT", "").strip()
    or "Attachments rule. When you finish and a result file should be sent back "
       "to the user as an attachment (an image, video, audio or any other file, "
       "not just code), you MUST:\n"
       "1. Save that file into the '.claude_tg_bot_media' subfolder of the current "
       "working directory. Never save it to /tmp, the home directory or anywhere "
       "else outside this folder.\n"
       "2. In your FINAL answer, print the path to it as a marker line, one per "
       "file, exactly on its own line:\n"
       "[FILE: <relative-path-inside-.claude_tg_bot_media>]\n"
       "Use the path relative to the current working directory (e.g. "
       "'.claude_tg_bot_media/frame.png'). Do NOT use an absolute /tmp path. "
       "Print one such line for each file you want to send. Only files saved in "
       "'.claude_tg_bot_media' are sent; anything you save elsewhere is not. "
       "If you created no file to send, print nothing."
)

# The root within which the bot may create/switch projects.
# We don't let the bot work with arbitrary paths — a sandbox.
PROJECTS_ROOT: Path = Path(
    os.getenv("PROJECTS_ROOT", str(Path.home() / "projects"))
).resolve()

# Default directory for a new chat if the user hasn't switched a project
DEFAULT_PROJECT: str = os.getenv("DEFAULT_PROJECT", "")

# Maximum prompt length (chars) sent to Claude
MAX_PROMPT_LENGTH: int = int(os.getenv("MAX_PROMPT_LENGTH", "8000"))

# Timeout of a single Claude call (sec). 0 = no limit.
CLAUDE_TIMEOUT_SECONDS: int = int(os.getenv("CLAUDE_TIMEOUT_SECONDS", "600"))


# ---------------------------------------------------------------------------
# Unified retry scheme for transient failures (connection, sending, Claude)
# ---------------------------------------------------------------------------
# One retry mechanism everywhere: after the N-th failure the pause grows as
# min(base * multiplier^(N-1), max). On success the counter resets to base.
# RETRY_LIMIT — total attempts (including the first) before giving up.
RETRY_LIMIT: int = int(os.getenv("RETRY_LIMIT", "5"))
RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", "60"))      # sec
RETRY_MAX_DELAY: float = float(os.getenv("RETRY_MAX_DELAY", "300"))       # sec (5 min)
RETRY_MULTIPLIER: float = float(os.getenv("RETRY_MULTIPLIER", "2.0"))

# Short retries for sending messages/indicators to Telegram — seconds, so the
# reply doesn't "hang" long (these are transient inter-DC errors, not a network loss).
MESSAGE_RETRY_LIMIT: int = int(os.getenv("MESSAGE_RETRY_LIMIT", "3"))
MESSAGE_RETRY_DELAY: float = float(os.getenv("MESSAGE_RETRY_DELAY", "2.0"))


# ---------------------------------------------------------------------------
# Downloaded attachment management (photo/video/audio/file)
# ---------------------------------------------------------------------------
# AUTO_DELETE_MEDIA — auto-delete an attachment after sending it to claude.
#   true  — delete (per DELETE_MODE) after processing.
#   false — do NOT delete automatically; clean only manually via /clearmedia.
#   Default false (safer — nothing is lost without an explicit command).
AUTO_DELETE_MEDIA: bool = _env_bool("AUTO_DELETE_MEDIA", False)

# DELETE_MODE — where to delete an attachment (for /clearmedia and auto-delete):
#   trash      — to the macOS Trash (restorable). Default value.
#   permanent  — delete forever (no way to restore).
DELETE_MODE: str = (os.getenv("DELETE_MODE", "trash").strip().lower() or "trash")

# KEEP_AWAKE — keep the laptop awake while the bot runs.
#   true  — keep the system awake (otherwise the network drops on sleep and the
#           bot stops receiving/answering messages).
#   false — change nothing. Default off (the user decides).
#   In claude-tg-bot.sh it's read as a config.env environment variable.
KEEP_AWAKE: bool = _env_bool("KEEP_AWAKE", False)

# KEEP_AWAKE_COMMAND — the command run to keep the system awake (command + args,
# space-separated). Used by claude-tg-bot.sh when KEEP_AWAKE is true; default
# "caffeinate -dimsu". Only the flag value is documented here — the actual
# command is read from config.env by the launch script.
KEEP_AWAKE_COMMAND: str = os.getenv("KEEP_AWAKE_COMMAND", "caffeinate -dimsu").strip()


# ---------------------------------------------------------------------------
# Sandbox (@helpbot)
# ---------------------------------------------------------------------------
# SANDBOX_ROOT — the absolute path to the directory Claude runs in for @helpbot
# messages (a sandbox launch "in any chat" by mention). If NOT set — a sandbox
# is created and used in the home directory:
#   ~/.claude-tg-bot/sandbox
SANDBOX_ROOT: Path = Path(
    os.getenv("SANDBOX_ROOT", str(Path.home() / ".claude-tg-bot" / "sandbox"))
).resolve()

# SANDBOX_COMMAND — the trigger string to launch the sandbox "in any chat".
# A message must start with this string (after lstrip), then a space + command/text.
# If unset or empty — the default "@helpbot" (a bot mention) is used.
# The real value is shown in /help and other help texts (see format_command_hint).
SANDBOX_COMMAND: str = (os.getenv("SANDBOX_COMMAND", "") or "@helpbot").strip()


# ---------------------------------------------------------------------------
# Session state storage
# ---------------------------------------------------------------------------

# File with user states (active projects etc.) — in the project root.
STATE_FILE: Path = _PROJECT_ROOT / "state.json"

# Bot message language (see i18n.py). Empty/unknown -> "en".
BOT_LANG: str = (os.getenv("BOT_LANG") or "").strip()


def write_lang_to_config(lang: str) -> bool:
    """Persist ``BOT_LANG`` into the resolved config.env (if found).

    Rewrites only the ``BOT_LANG=...`` line (or appends it if absent) so the
    language survives a restart. Returns True on success. Deliberately does
    not read secrets — only touches the single language key line.
    """
    if CONFIG_ENV_PATH is None:
        return False
    try:
        path = CONFIG_ENV_PATH
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        out = []
        replaced = False
        for ln in lines:
            if ln.lstrip().startswith("BOT_LANG"):
                out.append(f"BOT_LANG={lang}\n")
                replaced = True
            else:
                out.append(ln)
        if not replaced:
            out.append(f"BOT_LANG={lang}\n")
        path.write_text("".join(out), encoding="utf-8")
        return True
    except OSError:
        return False


def validate() -> None:
    """Validate the mandatory settings. Raises RuntimeError with a clear message."""
    problems = []
    warnings = []
    if CONFIG_ENV_PATH is None:
        problems.append(
            i18n.t("config.cfg_missing", candidates="\n  ".join(str(p) for p in CONFIG_ENV_CANDIDATES))
        )
    if not API_ID:
        problems.append(i18n.t("config.api_id_missing"))
    if not API_HASH:
        problems.append(i18n.t("config.api_hash_missing"))
    if not PHONE:
        problems.append(i18n.t("config.phone_missing"))
    if not os.getenv("PROJECTS_ROOT"):
        problems.append(i18n.t("config.projects_root_missing"))
    if not ALLOWED_USERS:
        warnings.append(i18n.t("config.warn_users"))
    if not ALLOWED_CHAT_IDS:
        warnings.append(i18n.t("config.warn_chats"))
    if problems:
        raise RuntimeError(i18n.t("config.validation_error", problems="\n  ".join(problems)))
    if warnings:
        print(i18n.t("config.warning", warnings="\n  ".join(warnings)))