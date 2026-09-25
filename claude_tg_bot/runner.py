"""Launching Claude and parsing its output.

The bot calls Claude as a subprocess in print mode (-p) with
--output-format stream-json --verbose, parses the JSONL stream and assembles
human-readable text to send back to Telegram.

It uses the command line rather than the SDK: the bot provides no interactive
terminal, so the call is a single pass via -p. The command is configured in
config.env via CLAUDE_COMMAND and COMMAND_ARGS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import config, i18n

# Claude launch events (command, session id, errors) — to the log file.
logger = logging.getLogger("claude_tg_bot")


def _gender_prompt(gender: str) -> str:
    return i18n.t(f"gender.prompt_{gender}")


@dataclass
class ClaudeResult:
    text: str = ""
    session_id: str = ""
    raw_lines: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    exit_code: int = 0


def _build_command(
    prompt: str,
    cwd: Path,
    resume_session_id: Optional[str] = None,
    command_args: Optional[str] = None,
) -> list[str]:
    """Build the argv for Claude.

    - With resume_session_id we continue a SPECIFIC session via --resume <id>.
      An explicit --resume is more reliable than --continue: interactive `claude
      -c` can't find sessions created via -p (--print), while --resume <id> always
      finds them.
    - Without an id — just a fresh -p request: a new session.
    - Since it's -p (print), the code runs non-interactively; for editing code
      that's a deliberate limitation of the first version.
    - command_args — overrides the args set (for a model switch when unavailable).
      None — the base config.COMMAND_ARGS are used.
    """
    cmd = [config.CLAUDE_COMMAND]
    args = config.COMMAND_ARGS if command_args is None else command_args
    cmd += shlex.split(args)
    cmd += ["--print", prompt]
    if resume_session_id:
        # Continue a specific session. --resume picks up both the context and the
        # permissions/history of exactly this id (unlike --continue, which is keyed
        # to the index of interactive sessions and may not find a session created
        # via -p).
        cmd += ["--resume", resume_session_id]
    # Auto mode: the bot runs Claude non-interactively (-p), and nobody can answer
    # a permission prompt from the terminal. So we pass the mode from settings
    # (default bypassPermissions — full auto). Otherwise in -p claude prints a
    # question ("What do you allow?") and hangs until the timeout without an
    # answer. The value comes from config, so it can be changed (e.g. acceptEdits)
    # without editing code.
    if config.CLAUDE_PERMISSION_MODE:
        cmd += ["--permission-mode", config.CLAUDE_PERMISSION_MODE]
    if config.CLAUDE_SYSTEM_PROMPT:
        cmd += ["--append-system-prompt", config.CLAUDE_SYSTEM_PROMPT]
    # The attachment-marker instruction — a separate system prompt added after
    # CLAUDE_SYSTEM_PROMPT so both reach the model (Claude concatenates repeated
    # --append-system-prompt flags). Only present when set.
    if config.ATTACHMENT_SYSTEM_PROMPT:
        cmd += ["--append-system-prompt", config.ATTACHMENT_SYSTEM_PROMPT]
    # A model-facing gender hint so Claude writes "from itself" in the right form.
    if config.AGENT_GENDER:
        cmd += ["--append-system-prompt", _gender_prompt(config.AGENT_GENDER)]
    cmd += [
        "--output-format", "stream-json",
        "--verbose",
    ]
    return cmd


def _human_result(lines: Iterable[str]) -> ClaudeResult:
    res = ClaudeResult()
    text_parts: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        res.raw_lines.append(line)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "system":
            sub = event.get("subtype")
            res.session_id = event.get("session_id") or res.session_id
            if sub == "init":
                continue
        elif event_type == "assistant":
            content = event.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tname = block.get("name", "")
                    res.tools.append(tname)
        elif event_type == "user":
            # There may be a tool_result here; not needed for the readable text
            continue
        elif event_type == "result":
            if not text_parts:
                text = event.get("result", "")
                if text:
                    text_parts.append(text)
            res.session_id = event.get("session_id") or res.session_id
            res.exit_code = int(event.get("is_error") or 0)

    res.text = "\n\n".join(p for p in text_parts if p.strip()).strip()
    return res


async def run_claude(
    prompt: str,
    cwd: Path,
    resume_session_id: Optional[str] = None,
    image_paths: Optional[list[Path]] = None,
    proc_registry: Optional[set[int]] = None,
    command_args: Optional[str] = None,
) -> ClaudeResult:
    """Launch Claude with a prompt, return the result.

    resume_session_id — the session id for --resume (continue a specific session).
    If None — a new session starts (after the answer the id comes back in
    result.session_id).

    image_paths: paths to image files appended to the prompt as @links (Claude
    accepts @path as an attachment).

    proc_registry: a pid set that tracks the COLLECTION of processes launched
    specifically by this bot. A pid is added on start and removed on exit. Using
    it the bot can kill ONLY its own processes (the /kill command) without
    touching foreign/manual Claude sessions.

    command_args: overrides the argument set (model switch when unavailable).
    None — the base config.COMMAND_ARGS.
    """
    cwd = Path(cwd)
    if not cwd.exists():
        raise FileNotFoundError(i18n.t("runner.dir_not_exists", path=cwd))

    final_prompt = prompt.strip()
    if image_paths:
        # Add file mentions as @links — this is exactly how Claude recognizes an
        # attachment in a prompt (also in -p mode). The format "path" in quotes
        # WITHOUT @ does NOT work: the model sees plain text about a file and
        # doesn't get the image (verified: with @ the image is described correctly,
        # without @ Claude tries to read the file via Read/bash). Claude figures
        # out the type by the extension itself.
        refs = [f"@{p}" for p in image_paths]
        if final_prompt:
            final_prompt = final_prompt + i18n.t("runner.files_attached", refs=", ".join(refs))
        else:
            # No prompt (image without a caption): give Claude only the @links,
            # without text — it sees the attachment itself and keeps it in the
            # session context. We don't add the "Files (attached)" note, so it
            # isn't taken as a task.
            final_prompt = " ".join(refs)

    cmd = _build_command(final_prompt, cwd, resume_session_id, command_args)
    run_cwd = str(cwd)
    logger.info("Launching Claude in %s (args set: %s)", run_cwd, command_args or "base")

    # Prompt length limit — protection against giant messages
    if len(final_prompt) > config.MAX_PROMPT_LENGTH:
        raise ValueError(i18n.t("runner.prompt_too_long", len=len(final_prompt)))

    # start_new_session=True — run the subprocess in its OWN session/group.
    # The wrapper may spawn child processes (e.g. its own proxy server and Claude
    # itself). If we kill only the parent (proc.kill()), the children become
    # orphaned (PPID→1) and keep hanging, holding downloaded files and ports.
    # Killing the whole GROUP (-pgid) removes them at once.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=run_cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        # Inherit the environment so Claude picks up its own env
        env=os.environ.copy(),
    )
    # Register the cluster pid as "launched by this bot", so /kill can kill only
    # its own, not every Claude process on the system.
    if proc_registry is not None:
        proc_registry.add(proc.pid)

    out_lines: list[str] = []
    err_lines: list[str] = []
    # Monotonic time of the last output chunk, for the idle timeout.
    last_activity = time.monotonic()

    async def _read(name, stream):
        """Read the stream in CHUNKS, not line by line.

        IMPORTANT: `async for raw in stream` in asyncio reads by line (readuntil)
        and fails with LimitOverrunError on VERY long lines (jsonl with
        --output-format stream-json, especially large hook contexts and responses
        with video attachments >64KB) → reading stops → the PIPE buffer overflows
        → the process blocks → the bot hangs until the timeout. So we read raw
        chunks and split into lines manually.
        """
        nonlocal last_activity
        buf = ""  # the tail of an incomplete line between chunks
        target = out_lines if name == "stdout" else err_lines
        try:
            while True:
                raw = await stream.read(65536)
                if not raw:
                    break
                last_activity = time.monotonic()
                buf += raw.decode(errors="replace")
                # Split by newlines; the last incomplete piece stays in buf.
                *parts, buf = buf.split("\n")
                target.extend(parts)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if buf:
            target.append(buf)

    tasks = [
        asyncio.create_task(_read("stdout", proc.stdout)),
        asyncio.create_task(_read("stderr", proc.stderr)),
    ]

    def _kill_group():
        """Kill the whole process cluster (parent + children), leaving no orphans."""
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            # Fallback — kill at least the parent
            try:
                proc.kill()
            except Exception:
                pass

    try:
        if config.CLAUDE_TIMEOUT_SECONDS > 0:
            # Idle timeout: abort only when Claude has produced NO output for the
            # whole window. A long request that is actively working (still writing)
            # is never cut; only a truly stalled process triggers the timeout.
            idle_limit = config.CLAUDE_TIMEOUT_SECONDS
            while True:
                remaining = idle_limit - (time.monotonic() - last_activity)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    await asyncio.wait_for(proc.wait(), timeout=remaining)
                    break  # process exited on its own
                except asyncio.TimeoutError:
                    # The window elapsed, but if output arrived meanwhile the idle
                    # clock reset — keep waiting instead of a false positive.
                    if time.monotonic() - last_activity >= idle_limit:
                        raise asyncio.TimeoutError
                    continue
        else:
            await proc.wait()
    except asyncio.TimeoutError:
        _kill_group()
        await proc.wait()
        # Drain the streams so the proxy diagnostics end up in stderr
        await asyncio.gather(*tasks, return_exceptions=True)
        tail = _tail_stderr(err_lines, proc_pid=proc.pid, limit=8)
        res = ClaudeResult()
        logger.warning("Claude launch timed out in %s", run_cwd)
        res.text = i18n.t("runner.timeout")
        # Mix in the cause from stderr, if the proxy left diagnostics.
        if tail:
            res.text += i18n.t("runner.timeout_diag", tail=tail)
        res.exit_code = 1
        return res
    except asyncio.CancelledError:
        # The task was cancelled externally (e.g. command /kill) — kill the process
        # cluster so no orphaned children remain, and propagate the cancel further.
        _kill_group()
        await proc.wait()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        # Remove the pid from the "launched by this bot" registry — process done/killed.
        if proc_registry is not None:
            proc_registry.discard(proc.pid)
        # Guaranteed to drain the streams
        await asyncio.gather(*tasks, return_exceptions=True)

    res = _human_result(out_lines)
    res.exit_code = proc.returncode or res.exit_code

    # If the process exited with an error but we got no readable text — show the
    # stderr tail, otherwise the bot silently returns emptiness instead of the cause.
    if res.exit_code != 0 and not res.text:
        tail = _tail_stderr(err_lines, proc_pid=proc.pid)
        if tail:
            res.text = i18n.t("runner.run_error", code=res.exit_code, tail=tail)
    if res.exit_code != 0:
        logger.error("Claude launch finished with code %s: %s", res.exit_code,
                     (res.text or "").splitlines()[0][:200] if res.text else "no text")
    return res


def _tail_stderr(err_lines: list[str], limit: int = 3, proc_pid: Optional[int] = None) -> str:
    """The last meaningful diagnostics lines.

    We take the stderr tail of the process; if it's empty, we additionally read
    the wrapper's log file (/tmp/claude-proxy-<pid>.log) where it writes the real
    cause of a failure (e.g. an authorization error or model unavailability).
    This matters: without it, on an auth/network failure the bot silently hangs
    until the timeout, and the cause is invisible.
    """
    lines = [ln.strip() for ln in err_lines if ln.strip()]
    # the proxy prints a banner + progress; take the tail and drop too-long ones
    tail = "\n".join(lines[-limit * 4:])[:600]
    if not tail and proc_pid:
        tail = _read_proxy_log(proc_pid)
    return tail


def _read_proxy_log(proc_pid: int) -> str:
    """Read the wrapper's fresh diagnostics log (if any).

    The log file is /tmp/claude-proxy-<pid>.log, where the wrapper redirects the
    output of its helper process. We're after the [ERROR] lines and the last few
    lines, not the banner/progress.
    """
    import glob
    # Look for the log by the process pid (bash wrappers) and "neighbor" ones if the pid moved
    candidates = [f"/tmp/claude-proxy-{proc_pid}.log"] + sorted(
        glob.glob("/tmp/claude-proxy-*.log"), key=os.path.getmtime, reverse=True
    )
    for path in candidates[:4]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        # ERROR lines and last INFO, dropping too-long/noisy ones
        errs = [ln for ln in content.splitlines()
                if "[ERROR]" in ln and ln.strip()]
        tail = ("\n".join(errs[-4:]) if errs else "\n".join(
            content.splitlines()[-6:]))
        tail = tail.strip()[:600]
        if tail:
            return tail
    return ""


# ---------------------------------------------------------------------------
# Utilities for Telegram handlers
# ---------------------------------------------------------------------------

# A marker line Claude prints in its answer for each file to send back as an
# attachment (see config.ATTACHMENT_SYSTEM_PROMPT).
_FILE_MARKER_RE = re.compile(r"\[FILE:\s*(.+?)\]")


def extract_file_markers(text: str) -> tuple[list[str], str]:
    """Split attachment markers out of the answer text.

    Returns (paths, clean_text): every "[FILE: <path>]" is captured (trimmed) and
    its marker line removed from the text that is shown to the user. A path may be
    a relative one (resolved against the working dir by the caller).
    """
    if not text:
        return [], text or ""
    paths: list[str] = []
    clean_lines: list[str] = []
    for line in text.splitlines():
        found = list(_FILE_MARKER_RE.findall(line))
        if found:
            paths.extend(p.strip() for p in found)
            # Drop the marker portion but keep any surrounding text of the line.
            rest = _FILE_MARKER_RE.sub("", line).strip()
            if rest:
                clean_lines.append(rest)
        else:
            clean_lines.append(line)
    return paths, "\n".join(clean_lines).strip()


def format_command_hint() -> str:
    mode = (config.DELETE_MODE or "trash").lower()
    where = i18n.t("cmd.trash") if mode == "trash" else i18n.t("cmd.permanent")
    return i18n.t("runner.command_hint", where=where, sandbox_cmd=config.SANDBOX_COMMAND)