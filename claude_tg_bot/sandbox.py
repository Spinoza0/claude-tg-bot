"""Sandbox detection: the SANDBOX_COMMAND trigger and message recognition.

The sandbox handling itself (on_sandbox) lives in handlers.py — it launches the
other handlers. Here we only keep pure predicates over the message string.
"""

from . import config

# Trigger string for the sandbox. Taken from config.SANDBOX_COMMAND (default
# "@helpbot" — a mention of the bot). A message must start with it, followed by a
# space and a command/text (with or without an image).
SANDBOX_PREFIX = config.SANDBOX_COMMAND


def _is_sandbox_message(text: str) -> bool:
    """Whether a message starts with the SANDBOX_COMMAND trigger (word boundary).

    We require a space or end-of-line after the trigger, so that similar strings
    (@helpbotxyz etc.) don't match. An empty trigger is treated as "not for the
    sandbox" (in practice SANDBOX_COMMAND is never empty — default @helpbot,
    see config).
    """
    if not SANDBOX_PREFIX:
        return False
    t = text.lstrip()
    if not t.startswith(SANDBOX_PREFIX):
        return False
    tail = t[len(SANDBOX_PREFIX):]
    return tail == "" or tail[0] in " \t\r\n"


def _strip_sandbox_prefix(text: str) -> str:
    """Strip the leading '@helpbot' and any whitespace after it.

    Returns the "tail" — the command/text handed to the sandbox. If the result is
    an empty string (and there's no image) the message is ignored.
    """
    t = text.lstrip()
    rest = t[len(SANDBOX_PREFIX):]
    return rest.lstrip(" \t\r\n")