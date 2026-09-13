"""Управление сессиями и проектами.

Бот хранит для каждого telegram user_id состояние:
    - активный рабочий каталог (project_root)
    - связанный session_id Claude (для resume контекста)

Состояние сериализуется в state.json, чтобы переживать перезапуск бота.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

# Совместимость с запуском и как пакета, и напрямую (python3 session.py / import session)
if __package__:
    from . import config
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config  # noqa: E402


@dataclass
class UserState:
    """Состояние одного пользователя бота."""

    user_id: int
    project_root: str = ""          # абсолютный путь рабочего каталога (активный, обычный)
    project_name: str = ""          # отображаемое имя активного проекта (обычный)
    agent_project_root: str = ""    # активный каталог для агента (внутри SANDBOX_ROOT)
    agent_project_name: str = ""    # отображаемое имя активного агентского проекта
    # Отдельная Claude session_id для КАЖДОГО каталога (project_root -> session_id).
    # Позволяет при /switch назад восстанавливать контекст проекта, а не терять его.
    # Ключ — полный путь, поэтому работает и для обычных проектов (PROJECTS_ROOT),
    # и для агентских (SANDBOX_ROOT).
    session_ids: Dict[str, str] = field(default_factory=dict)
    last_cwd: str = ""              # последний известный cwd сессии
    updated_at: float = field(default_factory=time.time)

    def get_session(self, project_root: str) -> str:
        """session_id для конкретного проекта (или "")."""
        return self.session_ids.get(project_root, "")

    def set_session(self, project_root: str, session_id: str) -> None:
        """Запомнить session_id для проекта ("" — удаляет/обнуляет)."""
        if session_id:
            self.session_ids[project_root] = session_id
        else:
            self.session_ids.pop(project_root, None)

    def get_active_root(self, agent: bool) -> str:
        """Активный рабочий каталог в зависимости от режима (обычный или агентский)."""
        return self.agent_project_root if agent else self.project_root

    def set_active(self, agent: bool, root: str, name: str) -> None:
        """Запомнить активный каталог и его имя для режима (обычный или агентский)."""
        if agent:
            self.agent_project_root = root
            self.agent_project_name = name
        else:
            self.project_root = root
            self.project_name = name

    def active_name(self, agent: bool) -> str:
        """Имя активного проекта для режима (или "")."""
        return self.agent_project_name if agent else self.project_name

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "project_root": self.project_root,
            "project_name": self.project_name,
            "agent_project_root": self.agent_project_root,
            "agent_project_name": self.agent_project_name,
            "session_ids": self.session_ids,
            "last_cwd": self.last_cwd,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserState":
        sids = d.get("session_ids", {})
        # Обратная совместимость: раньше был одиночный session_id
        if not sids and d.get("session_id"):
            sids = {d.get("project_root", ""): d["session_id"]}
        return cls(
            user_id=int(d.get("user_id", 0)),
            project_root=d.get("project_root", ""),
            project_name=d.get("project_name", ""),
            agent_project_root=d.get("agent_project_root", ""),
            agent_project_name=d.get("agent_project_name", ""),
            session_ids=sids if isinstance(sids, dict) else {},
            last_cwd=d.get("last_cwd", ""),
            updated_at=float(d.get("updated_at", 0)),
        )


# Валидное имя проекта — только буквы/цифры/дефис/подчёркивание/точка.
# Это защита от path traversal через имя проекта из сообщения.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def is_safe_project_name(name: str) -> bool:
    """Разрешён ли <name> как подпапка внутри PROJECTS_ROOT."""
    return bool(_SAFE_NAME.fullmatch(name)) and name not in (".", "..")


class SessionStore:
    """In-memory + state.json хранилище состояний пользователей."""

    def __init__(self, path: Path = config.STATE_FILE):
        self._path = path
        self._users: Dict[int, UserState] = {}
        self._load()

    # -- персистентность -----------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for uid, d in data.items():
                self._users[int(uid)] = UserState.from_dict(d)
        except (json.JSONDecodeError, OSError):
            # Повреждённый файл — начинаем с чистого состояния, не падаем
            self._users = {}

    def _save(self) -> None:
        try:
            payload = {str(uid): st.to_dict() for uid, st in self._users.items()}
            self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError:
            # Если не удалось сохранить — не роняем бота ради этого
            pass

    # -- доступ --------------------------------------------------------------
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
        """Список доступных проектов (подпапок корня).

        root — каталог, в котором ищем проекты. По умолчанию PROJECTS_ROOT
        (обычный режим). Для @helpbot передаётся SANDBOX_ROOT.
        """
        root = root if root is not None else config.PROJECTS_ROOT
        if not root.exists():
            return []
        return sorted(
            p
            for p in root.iterdir()
            if p.is_dir() and is_safe_project_name(p.name)
        )


def claude_project_dir(cwd: Path) -> Path:
    """Каталог, где Claude хранит JSONL сессий для заданного cwd.

    Claude кладёт сессии в ~/.claude/projects/<slug-cwd>/<session_id>.jsonl.
    Slug — это путь, нормализованный: '/' -> '-', затем усечённый.
    Здесь мы просто воспроизводим этот путь, чтобы найти существует ли сессия.
    """
    cls_projects = Path.home() / ".claude" / "projects"
    slug = str(cwd.resolve()).replace("/", "-").replace(".", "_")[:80]
    return cls_projects / slug


def find_latest_session(cwd: Path) -> Optional[str]:
    """Найти самый свежий session_id для данного cwd (для авто-продолжения)."""
    d = claude_project_dir(cwd)
    if not d.exists():
        return None
    try:
        sessions = sorted(
            (p.stem for p in d.glob("*.jsonl")),
            key=lambda s: d.joinpath(s + ".jsonl").stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    return sessions[0] if sessions else None