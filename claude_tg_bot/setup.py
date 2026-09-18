"""Interactive setup: create/edit config.env (issue #4).

Run by the setup.sh wrapper from the project root. Asks for settings by group
(mandatory and optional) and creates ~/.claude-tg-bot/config.env.
If the file already exists it does not overwrite but edits: makes a copy
config.env.bak, shows the current value (Enter — keep), replaces on new input.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from . import i18n

# Config directory and config.env file (live in ~/.claude-tg-bot/, next to sandbox).
_CFG_DIR = Path.home() / ".claude-tg-bot"
_CFG_PATH = _CFG_DIR / "config.env"

# Each setting: (key, mandatory?, default, hint for the prompt).
# required=True — the bot won't work without it (API_ID/API_HASH/PHONE/
# ALLOWED_USERS); the prompt marks it "(mandatory)". required=False may be left
# empty (Enter). Settings are grouped by meaning.
GROUPS = [
    ("Telegram (mandatory)", [
        ("API_ID", True, "", "App ID (my.telegram.org/apps)"),
        ("API_HASH", True, "", "App hash (my.telegram.org/apps)"),
        ("PHONE", True, "", "Telegram account number, e.g. +79991234567"),
        ("MT_PROXY", False, "", "MTProto proxy (tg://proxy?server=..&port=..&secret=..). Empty — direct connection"),
        ("CLOUD_TOKEN_PASSWORD", False, "", "2FA (cloud) password, if two-factor is enabled"),
    ]),
    ("Access", [
        ("ALLOWED_USERS", True, "", "user_id of those the bot answers (comma-separated). Empty — bot is closed"),
        ("ALLOWED_CHAT_IDS", False, "", "chat_id where the bot answers. Empty — only Saved Messages"),
    ]),
    ("Claude launch", [
        ("CLAUDE_COMMAND", True, "claude", "Claude (or wrapper) launch command, e.g. claude-cline"),
        ("COMMAND_ARGS", False, "", "Extra args for CLAUDE_COMMAND (e.g. --provider openai-compatible)"),
        ("COMMAND_ARGS_ALTERNATIVE", False, "", "Alternative args to switch the model when it is unavailable"),
        ("CLAUDE_SYSTEM_PROMPT", False, "", "System prompt (--append-system-prompt). Empty — default"),
    ]),
    ("Sandbox and projects", [
        ("SANDBOX_ROOT", False, "", "Sandbox folder (@helpbot). Empty — ~/.claude-tg-bot/sandbox"),
        ("PROJECTS_ROOT", True, "", "Root of projects the bot is allowed to work in"),
    ]),
]

# Settings not asked about (they have sensible defaults) — written as-is.
DEFAULTS = {
    "SANDBOX_COMMAND": "@helpbot",
    "BOT_LANG": "en",
    "CLAUDE_PERMISSION_MODE": "bypassPermissions",
    "CLAUDE_TIMEOUT_SECONDS": "600",
    "MAX_PROMPT_LENGTH": "8000",
    "RETRY_LIMIT": "5",
    "RETRY_BASE_DELAY": "60",
    "RETRY_MAX_DELAY": "300",
    "RETRY_MULTIPLIER": "2.0",
    "MESSAGE_RETRY_LIMIT": "3",
    "MESSAGE_RETRY_DELAY": "2.0",
    "AUTO_DELETE_MEDIA": "false",
    "DELETE_MODE": "trash",
    "KEEP_AWAKE": "false",
}


def _strip_inline_comment(val: str) -> str:
    """Strip a trailing ' # ...' comment from a value if it's outside quotes."""
    in_quote = False
    quote_char = ""
    for i, ch in enumerate(val):
        if ch in ('"', "'"):
            if not in_quote:
                in_quote, quote_char = True, ch
            elif ch == quote_char:
                in_quote = False
        elif ch == "#" and not in_quote and (i == 0 or val[i - 1].isspace()):
            return val[:i].rstrip()
    return val


def _load_existing() -> dict[str, str]:
    """Read current config.env values (if the file exists)."""
    result: dict[str, str] = {}
    if not _CFG_PATH.is_file():
        return result
    for line in _CFG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = _strip_inline_comment(val.strip()).strip().strip('"').strip("'")
        result[key] = val
    return result


def _sanitize(value: str) -> str:
    """Prepare a value for writing: wrap in quotes when it contains special chars."""
    value = value.strip()
    if value and any(ch in value for ch in ' "#\''):
        return f'"{value}"'
    return value


def _ask(key: str, hint: str, current: str, default: str, required: bool) -> str:
    """Ask for a setting and return the value.

    key — the variable name (e.g. API_ID) shown in the prompt; hint — a
    description. current — the current value (editing), default — on creation.
    Enter — keep current (if any) or default. Returns the value.
    """
    shown = current if current else default
    tag = i18n.t("setup.required_tag") if required else i18n.t("setup.optional_tag")
    if shown:
        prompt = i18n.t("setup.prompt_current", name=key, hint=hint, tag=tag, shown=shown)
    else:
        prompt = i18n.t("setup.prompt_empty", name=key, hint=hint, tag=tag)
    while True:
        try:
            inp = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print(i18n.t("setup.aborted"))
            raise SystemExit(1)
        if inp == "":
            return shown
        return inp


def _render_lines(values: dict[str, str]) -> list[str]:
    """Build config.env lines by group, with hint comments."""
    lines = [
        "# config.env — generated by the setup script (claude_tg_bot/setup.py).",
        "# Full list of options and explanations — see config.env.example.",
        "",
    ]
    for title, settings in GROUPS:
        lines.append(f"# --- {title} " + "-" * max(1, 48 - len(title)))
        for key, _req, default, hint in settings:
            val = _sanitize(values.get(key, default))
            lines.append(f"# {hint}")
            lines.append(f"{key}={val}")
        lines.append("")
    lines.append("# --- Other (defaults) " + "-" * 30)
    for key, val in DEFAULTS.items():
        lines.append(f"{key}={_sanitize(val)}")
    lines.append("")
    return lines


def _write(path: Path, values: dict[str, str]) -> None:
    """Write config.env, making a .bak if the file already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
        print(i18n.t("setup.bak_created", path=bak))
    path.write_text("\n".join(_render_lines(values)) + "\n", encoding="utf-8")
    print(i18n.t("setup.written", path=path))


def main() -> None:
    print(i18n.t("setup.title") + "\n")

    existing = _load_existing()
    if existing:
        print(i18n.t("setup.existing", path=_CFG_PATH) + "\n")
    else:
        print(i18n.t("setup.new", path=_CFG_PATH) + "\n")

    values: dict[str, str] = {}
    for title, settings in GROUPS:
        print(i18n.t("setup.group_title", title=title))
        for key, required, default, hint in settings:
            current = existing.get(key, "")
            values[key] = _ask(key, hint, current, default, required)

    values.update(DEFAULTS)

    # Check mandatory params: if any is empty we don't save, but ask to fill it
    # in (otherwise the bot won't start).
    missing = [k for g in GROUPS for k, req, _d, _h in g[1]
               if req and not values.get(k)]
    if missing:
        print(i18n.t("setup.missing", missing=", ".join(missing)))
        print(i18n.t("setup.not_saved"))
        raise SystemExit(1)

    print(i18n.t("setup.result", path=_CFG_PATH))
    _write(_CFG_PATH, values)
    print(i18n.t("setup.done1"))
    print(i18n.t("setup.done2"))
    print(i18n.t("setup.done3"))


if __name__ == "__main__":
    main()