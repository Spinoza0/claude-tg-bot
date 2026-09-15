"""Управление процессами бота: single-instance, реестр запущенных Claude.

- Защита от двух экземпляров (один .session / SQLite) — сканирование процессов
  плюс атомарный lock-файл.
- Реестр pid-групп Claude, запущенных ЭТИМ ботом (_bot_proc_pids) и фоновых
  asyncio-задач (_active_tasks) — для команды /kill.
"""

import asyncio
import os
import re
import signal
import subprocess
from pathlib import Path

# Маркер запуска бота: пакет стартует как `python -m claude_tg_bot` (или через
# claude_tg_bot/__main__.py). По нему ищем себя в списке процессов.
_BOT_MODULE = "claude_tg_bot"

# lock-файл для гашения ГОНКИ при почти-одновременном старте двух экземпляров.
# Создаётся атомарно (O_CREAT|O_EXCL) и удаляется через 2 секунды — поэтому
# не копит мусор на диске. После истечения защиту несёт проверка по процессам.
_LOCK_FILE = Path(__file__).resolve().parent.parent / "claude-tg-bot.lock"


def _get_bot_pids() -> list[int]:
    """PID всех запущенных экземпляров бота, кроме текущего.

    Сканируем `ps` по всей системе (не только потомков), чтобы поймать бота,
    запущенного из любого терминала/сессии. Match'им именно запуск пакета
    python-интерпретатором (`python -m claude_tg_bot` или путь к __main__.py),
    а не любое упоминание в командной строке шелла (иначе zsh/bash-обёртки с
    `source`/heredoc дают ложные срабатывания). Себя исключаем.
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
    # Ищем либо `python ... -m claude_tg_bot`, либо путь к пакету claude_tg_bot.
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
        # Исключаем шелл-обёртки (source/heredoc/-c), чтобы не ловить zsh/bash.
        if re.search(r"(^|\s)(zsh|bash|sh)(\s|$)", cmd):
            continue
        if pattern.search(cmd):
            pids.append(pid)
    return pids


def _try_create_lock() -> bool:
    """Атомарно создать lock-файл (O_CREAT|O_EXCL) с нашим PID.

    Возвращает True, если lock получен нами. False — если файл уже существует
    (другой экземпляр стартует в эту же секунду), и мы должны выйти.
    """
    try:
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        # Кто-то уже занял lock прямо сейчас — это гонка, блокируем.
        return False
    except OSError:
        # Не смогли создать — не критично, полагаемся на проверку процессов.
        return True
    return True


def _schedule_lock_expiry(delay: float = 2.0) -> None:
    """Фоном удалить lock-файл через delay секунд, чтобы он не хранился зря."""
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
    """Проверить, не запущен ли бот уже (процессы + атомарный lock-файл).

    Возвращает True, если можно стартовать; False — если бот уже работает
    (или стартует параллельно), и мы должны выйти.

    Двойная защита от двух экземпляров (оба работают с одним .session / SQLite):
      1) Атомарный lock-файл (создаётся O_EXCL, удаляется через 2 с) — гасит
         МГНОВЕННУЮ гонку, когда два бота стартуют одновременно.
      2) Сканирование процессов — ловит УЖЕ работающего бота.
    """
    if not _try_create_lock():
        print(f"⚠️  Бот уже запускается (lock-файл {_LOCK_FILE.name}). "
              "Второй экземпляр не запускается.")
        return False
    _schedule_lock_expiry()

    # Затем проверяем процессы: если бот уже давно работает, не стартуем.
    others = _get_bot_pids()
    if others:
        print(f"⚠️  Бот уже запущен (pid {', '.join(map(str, others))}). "
              "Второй экземпляр не запускается.")
        return False
    return True


# pid-группы claude-кластеров, ЗАПУЩЕННЫХ ЭТИМ БОТОМ. Нужны команде /kill,
# чтобы прибить только свои подвисшие сессии, а не все claude в системе.
# Ведётся в run_claude через param proc_registry (add на спавне / discard на выходе).
_bot_proc_pids: set[int] = set()

# Фоновые asyncio-задачи (вызовы claude). Храним, чтобы /kill мог их отменить.
_active_tasks: set["asyncio.Task"] = set()


def _count_group(gid: int) -> int:
    """Сколько процессов в группе gid (для отчёта /kill). 0, если группа пуста."""
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=", "-g", str(gid)],
            capture_output=True, text=True, timeout=2,
        )
        return len([ln for ln in out.stdout.splitlines() if ln.strip()])
    except Exception:
        return 1  # не удалось посчитать — считаем минимум сам процесс


def kill_bot_procs() -> tuple[int, list[str]]:
    """Убить все зависшие claude-кластеры, запущенные этим ботом.

    Работает ТОЛЬКО по реестру _bot_proc_pids (pid групп, которые бот сам
    зарегистрировал в run_claude), поэтому чужие/ручные сессии claude не
    затрагиваются. Возвращает (сколько ПРОЦЕССОВ закрыто, список ошибок).
    """
    killed = 0
    errors: list[str] = []
    for pid in list(_bot_proc_pids):
        try:
            # Считаем процессы в группе ДО убийства (для честного отчёта).
            pgid = os.getpgid(pid)
            n = _count_group(pgid)
            # Убиваем всю группу (родитель + дочерние процессы), не только родителя.
            os.killpg(pgid, signal.SIGKILL)
            killed += n if n else 1
        except ProcessLookupError:
            # Процесс уже завершился сам — не ошибка, засчитываем.
            _bot_proc_pids.discard(pid)
            killed += 1
        except Exception as e:
            errors.append(f"pid {pid}: {e}")
    # Снимаем убитые pids из реестра, чтобы повторный /kill был идемпотентным.
    for pid in list(_bot_proc_pids):
        try:
            os.kill(pid, 0)  # probe: жив ли ещё
        except ProcessLookupError:
            _bot_proc_pids.discard(pid)
    return killed, errors


def _start_bg(coro):
    """Запустить корутину фоновой задачей и запомнить её для отмены из /kill.

    Храним ссылку на задачу, чтобы /kill мог прервать зависший вызов claude.
    При завершении задача сама снимается из _active_tasks.
    """
    task = asyncio.get_running_loop().create_task(coro)
    _active_tasks.add(task)

    def _done(t):
        _active_tasks.discard(t)
        # Не даём необработанным исключениям «молча» всплыть в event loop
        if not t.cancelled():
            try:
                t.exception()
            except asyncio.CancelledError:
                pass

    task.add_done_callback(_done)
    return task