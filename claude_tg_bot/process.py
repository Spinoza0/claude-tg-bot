"""Bot process management: single-instance, registry of launched Claude.

- Protection against two instances (one .session / SQLite) — process scanning
  plus an atomic lock file.
- Registry of Claude pid-groups launched BY THIS bot (_bot_proc_pids) and
  background asyncio tasks (_active_tasks) — for the /kill command.
"""

import asyncio
import os
import re
import signal
import subprocess
from pathlib import Path

from . import i18n

# Marker of a bot launch: the package starts as `python -m claude_tg_bot` (or
# via claude_tg_bot/__main__.py). By it we find ourselves in the process list.
_BOT_MODULE = "claude_tg_bot"

# Lock file to neutralize a RACE when two instances start nearly simultaneously.
# Created atomically (O_CREAT|O_EXCL) and removed after 2 seconds, so it does not
# accumulate junk on disk. After it expires, the process check carries the guard.
_LOCK_FILE = Path(__file__).resolve().parent.parent / "claude-tg-bot.lock"


def _get_bot_pids() -> list[int]:
    """PID of every running bot instance, except the current one.

    We scan `ps` over the whole system (not only children) to catch a bot started
    from any terminal/session. We match the actual package launch by a python
    interpreter (`python -m claude_tg_bot` or a path to __main__.py), not any
    mention in a shell command line (otherwise zsh/bash wrappers with
    `source`/heredoc produce false positives). We exclude ourselves.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []
    my_pid = os.getpid()
    pids: list[int] = []
    # Match either `python ... -m claude_tg_bot` or a path to the claude_tg_bot package.
    pattern = re.compile(
        rf"(?i)\bpython(?:3(?:\.\d+)?)?\b.*(?:{re.escape(_BOT_MODULE)}|\b{re.escape(_BOT_MODULE)}[\\/]__main__\.py)"
    )
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == my_pid:
            continue
        cmd = parts[1]
        # Exclude shell wrappers (source/heredoc/-c) so we don't catch zsh/bash.
        if re.search(r"(^|\s)(zsh|bash|sh)(\s|$)", cmd):
            continue
        if pattern.search(cmd):
            pids.append(pid)
    return pids


def _try_create_lock() -> bool:
    """Atomically create the lock file (O_CREAT|O_EXCL) with our PID.

    Returns True if we got the lock. False — if the file already exists (another
    instance is starting at the same second) and we should exit.
    """
    try:
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        # Someone grabbed the lock right now — a race, so we block.
        return False
    except OSError:
        # Could not create it — not critical, rely on the process check.
        return True
    return True


def _schedule_lock_expiry(delay: float = 2.0) -> None:
    """Remove the lock file in the background after `delay` seconds, so it doesn't linger."""
    def _rm():
        try:
            if _LOCK_FILE.exists() and _LOCK_FILE.read_text().strip() == str(os.getpid()):
                _LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        import threading
        threading.Timer(delay, _rm).start()
    except Exception:
        pass


def acquire_single_instance() -> bool:
    """Check whether the bot is already running (processes + atomic lock file).

    Returns True if we may start; False — if the bot already runs (or is starting
    in parallel), and we should exit.

    Double protection against two instances (both share one .session / SQLite):
      1) An atomic lock file (created O_EXCL, removed after 2 s) — neutralizes
         the INSTANT race when two bots start at the same time.
      2) Process scanning — catches an ALREADY-RUNNING bot.
    """
    if not _try_create_lock():
        print(i18n.t("process.already_starting", name=_LOCK_FILE.name))
        return False
    _schedule_lock_expiry()

    # Then check the processes: if the bot has been running a while, don't start.
    others = _get_bot_pids()
    if others:
        print(i18n.t("process.already_running", pids=", ".join(map(str, others))))
        return False
    return True


# pid-groups of claude clusters LAUNCHED BY THIS BOT. Needed by /kill to kill
# only our own hung sessions, not every claude on the system.
# Maintained in run_claude via the proc_registry param (add on spawn / discard on exit).
_bot_proc_pids: set[int] = set()

# Background asyncio tasks (claude calls). Kept so /kill can cancel them.
_active_tasks: set["asyncio.Task"] = set()


def _count_group(gid: int) -> int:
    """How many processes are in group `gid` (for the /kill report). 0 if empty."""
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=", "-g", str(gid)],
            capture_output=True, text=True, timeout=2,
        )
        return len([ln for ln in out.stdout.splitlines() if ln.strip()])
    except Exception:
        return 1  # couldn't count — assume at least the process itself


def kill_bot_procs() -> tuple[int, list[str]]:
    """Kill all hung claude clusters launched by this bot.

    Works ONLY over the _bot_proc_pids registry (the pid groups the bot itself
    registered in run_claude), so foreign/manual claude sessions are untouched.
    Returns (how many PROCESSES were closed, list of errors).
    """
    killed = 0
    errors: list[str] = []
    for pid in list(_bot_proc_pids):
        try:
            # Count processes in the group BEFORE killing (for an honest report).
            pgid = os.getpgid(pid)
            n = _count_group(pgid)
            # Kill the whole group (parent + children), not only the parent.
            os.killpg(pgid, signal.SIGKILL)
            killed += n if n else 1
        except ProcessLookupError:
            # The process already exited on its own — not an error, count it.
            _bot_proc_pids.discard(pid)
            killed += 1
        except Exception as e:
            errors.append(f"pid {pid}: {e}")
    # Drop the killed pids from the registry so a repeated /kill is idempotent.
    for pid in list(_bot_proc_pids):
        try:
            os.kill(pid, 0)  # probe: is it still alive
        except ProcessLookupError:
            _bot_proc_pids.discard(pid)
    return killed, errors


def _start_bg(coro):
    """Run a coroutine as a background task and remember it for cancel via /kill.

    Keep a reference to the task so /kill can abort a hung claude call.
    On completion the task removes itself from _active_tasks.
    """
    task = asyncio.get_running_loop().create_task(coro)
    _active_tasks.add(task)

    def _done(t):
        _active_tasks.discard(t)
        # Don't let unhandled exceptions surface "silently" into the event loop
        if not t.cancelled():
            try:
                t.exception()
            except asyncio.CancelledError:
                pass

    task.add_done_callback(_done)
    return task