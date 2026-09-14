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
    user_id: int
    project_root: str = ""          # обычный режим
    project_name: str = ""
    sandbox_project_root: str = ""    # песочница (SANDBOX_ROOT)
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


# Валидное имя проекта — только буквы/цифры/дефис/подчёркивание/точка.
# Это защита от path traversal через имя проекта из сообщения.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def is_safe_project_name(name: str) -> bool:
    return bool(_SAFE_NAME.fullmatch(name)) and name not in (".", "..")


class SessionStore:
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
    # Claude составляет slug из абсолютного пути: '/'-' и '.' (напр. в
    # sintyurin.ivan, .claude) превращаются в '-'. Раньше '.' меняли на '_' —
    # slug не совпадал, и has_session искал несуществующий каталог, поэтому
    # --continue никогда не передавался и каждая сессия начиналась заново.
    slug = str(cwd.resolve()).replace("/", "-").replace(".", "-")[:80]
    return cls_projects / slug


def has_session(cwd: Path) -> bool:
    """Есть ли хоть одна сессия Claude в каталоге cwd.

    Проверяем наличие .jsonl в ~/.claude/projects/<slug-cwd>/. Конкретный
    session_id нам не нужен: для продолжения достаточно флага --continue,
    который Claude сам разрешает в последнюю сессию каталога.
    """
    d = claude_project_dir(cwd)
    if not d.exists():
        return False
    try:
        return any(d.glob("*.jsonl"))
    except OSError:
        return False