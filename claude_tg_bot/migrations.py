"""One-time config.env migrations.

Each migration rewrites config.env in place when the launched version first reads
it. New migrations are appended to MIGRATIONS (in order) — the caller just invokes
run_migrations() and never needs to change main code. A migration runs once when
its marker is absent from the file; the marker is written back so it doesn't rerun.
"""

import os
from pathlib import Path
from typing import Callable

# A migration's marker line we drop from the file once applied, so it turns the
# legacy key into the new one and never runs again.
_MARKER_PREFIX = "# migrated:"


def _rewrite(env_file: Path, rewrite: Callable[[list[str]], list[str]]) -> None:
    """Replace the file lines via rewrite(); leave it untouched on any error."""
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    try:
        new_lines = rewrite(lines)
    except Exception:
        return
    try:
        env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _rename_key(lines, old: str, new: str, marker: str) -> list[str]:
    """Map a legacy KEY=... line to NEW_KEY=... once (checked by marker)."""
    if any(line.startswith(marker) for line in lines):
        return lines
    renamed = []
    done = False
    for line in lines:
        if line.split("#", 1)[0].strip().startswith(f"{old}=") and not done:
            renamed.append(line.replace(old, new, 1))
            renamed.append(marker)
            done = True
        else:
            renamed.append(line)
    return renamed


# Ordered list of one-time migrations. Each is (marker, apply(lines) -> lines).
MIGRATIONS: list[tuple[str, Callable[[list[str]], list[str]]]] = [
    # Legacy key MT_PROXY -> PROXY (kept for any old config from before the rename).
    ("# migrated: mt_proxy_to_proxy",
     lambda lines: _rename_key(lines, "MT_PROXY", "PROXY",
                               "# migrated: mt_proxy_to_proxy")),
]


def run_migrations(env_file: Path | None) -> None:
    """Apply any pending config.env migrations to env_file (no-op if None/absent)."""
    if env_file is None:
        return
    for marker, apply_fn in MIGRATIONS:
        _rewrite(env_file, apply_fn)


def promote_env_legacy() -> None:
    """Mirror a legacy MT_PROXY env var into PROXY when PROXY is not set.

    For configs loaded before this rename the file is rewritten by run_migrations,
    but the in-process os.environ may still hold MT_PROXY only (e.g. variables set
    by hand, not from the file). Ensure the code reads PROXY.
    """
    old = os.getenv("MT_PROXY")
    if old and not os.getenv("PROXY"):
        os.environ["PROXY"] = old
        os.environ.pop("MT_PROXY", None)


if __name__ == "__main__":
    # Called from the launch script before the bot starts: python -m
    # claude_tg_bot.migrations <config.env>. Keeps config.env migration out of the
    # main code path so new migrations are added here, not in config.
    import sys

    if len(sys.argv) > 1:
        run_migrations(Path(sys.argv[1]))