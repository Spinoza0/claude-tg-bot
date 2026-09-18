"""Project management.

For each telegram user_id the bot stores a state: the active working directory
(project_root) and, separately, the sandbox directory (sandbox_project_root).
The state is serialized to state.json so it survives a bot restart. The Claude
session itself lives on disk (in ~/.claude/projects) and is not stored here.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from . import config


@dataclass
class UserState:
    user_id: int
    project_root: str = ""          # regular mode
    project_name: str = ""
    sandbox_project_root: str = ""    # sandbox (SANDBOX_ROOT)
    sandbox_project_name: str = ""
    last_cwd: str = ""
    updated_at: float = field(default_factory=time.time)

    def get_active_root(self, sandbox: bool) -> str:
        return self.sandbox_project_root if sandbox else self.project_root

    def set_active(self, sandbox: bool, root: str, name: str) -> None:
        if sandbox:
            self.sandbox_project_root = root
            self.sandbox_project_name = name
        else:
            self.project_root = root
            self.project_name = name

    def active_name(self, sandbox: bool) -> str:
        return self.sandbox_project_name if sandbox else self.project_name

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "project_root": self.project_root,
            "project_name": self.project_name,
            "sandbox_project_root": self.sandbox_project_root,
            "sandbox_project_name": self.sandbox_project_name,
            "last_cwd": self.last_cwd,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserState":
        return cls(
            user_id=int(d.get("user_id", 0)),
            project_root=d.get("project_root", ""),
            project_name=d.get("project_name", ""),
            sandbox_project_root=d.get("sandbox_project_root", ""),
            sandbox_project_name=d.get("sandbox_project_name", ""),
            last_cwd=d.get("last_cwd", ""),
            updated_at=float(d.get("updated_at", 0)),
        )


# A valid project name consists of letters/digits/hyphen/underscore/dot only.
# This guards against path traversal via a project name coming from a message.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def is_safe_project_name(name: str) -> bool:
    return bool(_SAFE_NAME.fullmatch(name)) and name not in (".", "..")


class SessionStore:
    def __init__(self, path: Path = config.STATE_FILE):
        self._path = path
        self._users: Dict[int, UserState] = {}
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for uid, d in data.items():
                self._users[int(uid)] = UserState.from_dict(d)
        except (json.JSONDecodeError, OSError):
            # Corrupted file — start from a clean state rather than crash
            self._users = {}

    def _save(self) -> None:
        try:
            payload = {str(uid): st.to_dict() for uid, st in self._users.items()}
            self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError:
            # If saving fails, don't crash the bot over it
            pass

    # -- access --------------------------------------------------------------
    def get(self, user_id: int) -> Optional[UserState]:
        return self._users.get(user_id)

    def get_or_init(self, user_id: int) -> UserState:
        st = self._users.get(user_id)
        if st is None:
            st = UserState(user_id=user_id)
            self._users[user_id] = st
        return st

    def update(self, st: UserState) -> None:
        st.updated_at = time.time()
        self._users[st.user_id] = st
        self._save()

    def list_projects(self, user_id: int, root: Optional[Path] = None) -> list[Path]:
        """List available projects (subfolders of the root).

        root — the directory to scan for projects. By default PROJECTS_ROOT
        (regular mode). For @helpbot SANDBOX_ROOT is passed.
        """
        root = root if root is not None else config.PROJECTS_ROOT
        if not root.exists():
            return []
        return sorted(
            p
            for p in root.iterdir()
            if p.is_dir() and is_safe_project_name(p.name)
        )


# Single shared state store instance (common to all handlers).
store = SessionStore()


def claude_project_dir(cwd: Path) -> Path:
    """Directory where Claude stores JSONL sessions for a given cwd.

    Claude keeps sessions in ~/.claude/projects/<slug-cwd>/<session_id>.jsonl.
    The slug is a normalized path: '/' -> '-', then truncated. Here we simply
    reproduce that path so we can check whether a session exists.
    """
    cls_projects = Path.home() / ".claude" / "projects"
    # Claude builds the slug from the absolute path: '/' and '.' (e.g. in
    # john.doe, .claude) both become '-'. Previously '.' was mapped to '_',
    # so the slug didn't match, has_session looked for a nonexistent directory,
    # and --continue was never passed — each session started over.
    slug = str(cwd.resolve()).replace("/", "-").replace(".", "-")[:80]
    return cls_projects / slug


def has_session(cwd: Path) -> bool:
    """Whether there is any Claude session in the cwd directory.

    Checks for a .jsonl in ~/.claude/projects/<slug-cwd>/.
    """
    d = claude_project_dir(cwd)
    if not d.exists():
        return False
    try:
        return any(d.glob("*.jsonl"))
    except OSError:
        return False


def find_latest_session(cwd: Path) -> Optional[str]:
    """The session_id of the newest Claude session in the cwd directory, or None.

    Returns the name (without extension) of the newest .jsonl by mtime inside
    ~/.claude/projects/<slug-cwd>/. That's the session_id which can be passed to
    --resume. If there are no sessions — None (a new one will start).
    """
    d = claude_project_dir(cwd)
    if not d.exists():
        return None
    try:
        files = [p for p in d.glob("*.jsonl") if p.is_file()]
    except OSError:
        return None
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return newest.stem