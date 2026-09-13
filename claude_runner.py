"""Запуск Claude и разбор его вывода.

Бот вызывает Claude как субпроцесс в режиме печати (-p) с
--output-format stream-json --verbose, парсит поток JSONL и собирает
человекочитаемый текст для отправки обратно в Telegram.

Используется именно командная строка, а не SDK: бот не предоставляет
интерактивный терминал, поэтому вызов идёт одиночным проходом через -p.
Команда настраивается в config.env через CLAUDE_COMMAND и COMMAND_ARGS.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

if __package__:
    from . import config
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config  # noqa: E402


@dataclass
class ClaudeResult:
    """Итог одного вызова Claude."""

    text: str = ""                 # финальный ответ для показа юзеру
    session_id: str = ""           # session_id сессии (для resume)
    raw_lines: list[str] = field(default_factory=list)  # сырые JSON-строки
    tools: list[str] = field(default_factory=list)      # упоминания инструментов
    exit_code: int = 0


def _build_command(prompt: str, cwd: Path, session_id: Optional[str]) -> list[str]:
    """Собрать argv для Claude.

    - Без --resume просто новый запрос из -p.
    - С --resume <id> продолжаем существующую сессию (сохраняем контекст).
    - Т.к. это -p (печать), код выполняется неинтерактивно; для правки кода
      это осознанное ограничение первой версии.
    """
    cmd = [config.CLAUDE_COMMAND]
    # Дополнительные аргументы обёртки (COMMAND_ARGS) — добавляются в начало,
    # разбиваются на отдельные токены (поддерживают кавычки, напр. значение
    # с пробелами). Пустая строка ничего не даёт.
    cmd += shlex.split(config.COMMAND_ARGS)
    # Промпт сразу после -p (как в справке CLI: claude -p "query" ...)
    cmd += ["--print", prompt]
    if session_id:
        # Продолжаем конкретную сессию по ID — так сохраняем контекст диалога.
        # --resume=<id> через '=' — чтобы значение не потерялось при парсинге.
        cmd += [f"--resume={session_id}"]
    # Авто-режим: бот запускает Claude неинтерактивно (-p), и никто не может
    # ответить на запрос разрешения из терминала. Поэтому передаём режим
    # --permission-mode из настроек (по умолчанию bypassPermissions — полный
    # авто). Иначе в -p клод печатает вопрос («Что разрешаешь?») и зависает
    # до таймаута, не получая ответа. Значение берём из config, чтобы можно
    # было сменить (напр. acceptEdits) без правки кода.
    if config.CLAUDE_PERMISSION_MODE:
        cmd += ["--permission-mode", config.CLAUDE_PERMISSION_MODE]
    cmd += [
        "--output-format", "stream-json",
        "--verbose",
    ]
    return cmd


def _human_result(lines: Iterable[str]) -> ClaudeResult:
    """Перевести поток JSONL в человекочитаемый ответ."""
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
            # Не-JSON строка — это мусор от Claude, пропускаем
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
            # Тут может быть tool_result; для читаемого текста они не нужны
            continue
        elif event_type == "result":
            # Финальный результат содержит итоговый текст
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
    session_id: Optional[str] = None,
    image_paths: Optional[list[Path]] = None,
    proc_registry: Optional[set[int]] = None,
) -> ClaudeResult:
    """Запустить Claude с промптом, вернуть результат.

    image_paths: пути к файлам-картинкам, которые добавятся в промпт как
    @ссылки (Claude принимает @path как attachment).

    proc_registry: множество pid, которое ведёт КОЛЛЕКЦИЮ процессов, запущенных
    именно этим ботом. Сюда добавляется pid при старте и удаляется при выходе.
    По нему бот может прибить ТОЛЬКО СВОИ процессы (команда /kill), не трогая
    чужие/ручные сессии Claude.
    """
    cwd = Path(cwd)
    if not cwd.exists():
        raise FileNotFoundError(f"Каталог не существует: {cwd}")

    final_prompt = prompt.strip()
    if image_paths:
        # Добавляем упоминания файлов как @ссылки — именно так Claude
        # распознаёт вложение в промпте (и в -p режиме). Формат "путь" в
        # кавычках БЕЗ @ НЕ работает: модель видит просто текст про файл и не
        # получает изображение (проверено: с @ картинка описывается верно,
        # без @ — Claude пытается читать файл через Read/bash). Claude
        # сам определит тип по расширению.
        refs = [f"@{p}" for p in image_paths]
        if final_prompt:
            final_prompt = f"{final_prompt}\n\nФайлы (приложены): {', '.join(refs)}"
        else:
            # Промпта нет (картинка без подписи): отдаём в Claude только
            # @ссылки, без текста — он сам увидит вложение и сохранит его
            # в контексте сессии. Приписку «Файлы (приложены)» не добавляем,
            # чтобы не воспринималась как задание.
            final_prompt = " ".join(refs)

    cmd = _build_command(final_prompt, cwd, session_id)
    run_cwd = str(cwd)

    # Ограничение длины промпта — защита от гигантских сообщений
    if len(final_prompt) > config.MAX_PROMPT_LENGTH:
        raise ValueError(f"Промпт слишком длинный ({len(final_prompt)} символов)")

    # start_new_session=True — запускаем субпроцесс в СОБСТВЕННОЙ сессии/группе.
    # Обёртка может породить дочерние процессы (например, свой прокси-сервер и
    # сам Claude). Если убить только родителя (proc.kill()), дети осиротеют
    # (PPID→1) и продолжат висеть, держа скачанные файлы и порты. Убийство всей
    # ГРУППЫ (-pgid) убирает их разом.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=run_cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        # Наследуем окружение, чтобы Claude подхватил свои env
        env=os.environ.copy(),
    )
    # Регистрируем pid кластера как «запущенный этим ботом», чтобы /kill мог
    # прибить только свои, а не все процессы Claude в системе.
    if proc_registry is not None:
        proc_registry.add(proc.pid)

    out_lines: list[str] = []
    err_lines: list[str] = []

    async def _read(name, stream):
        """Читать поток ЧАНКАМИ, а не построчно.

        ВАЖНО: `async for raw in stream` в asyncio читает по строкам
        (readuntil) и падает с LimitOverrunError на ОЧЕНЬ длинных строках
        (jsonl при --output-format stream-json, особенно большие hook-контексты
        и ответы с вложениями-видео>64КБ) → чтение останавливается → буфер
        PIPE переполняется → процесс блокируется → бот висит до таймаута.
        Поэтому читаем сырые чанки и разбиваем на строки вручную.
        """
        buf = ""  # хвост неполной строки между чанками
        target = out_lines if name == "stdout" else err_lines
        try:
            while True:
                raw = await stream.read(65536)
                if not raw:
                    break
                buf += raw.decode(errors="replace")
                # Разбиваем по переносам; последний неполный кусок остаётся в buf.
                *parts, buf = buf.split("\n")
                target.extend(parts)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        # Хвост последней строки без переноса
        if buf:
            target.append(buf)

    tasks = [
        asyncio.create_task(_read("stdout", proc.stdout)),
        asyncio.create_task(_read("stderr", proc.stderr)),
    ]

    def _kill_group():
        """Убить весь процесс-кластер (родитель + дети), не оставляя осиротевших."""
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            # Фолбэк — убиваем хотя бы родителя
            try:
                proc.kill()
            except Exception:
                pass

    try:
        if config.CLAUDE_TIMEOUT_SECONDS > 0:
            await asyncio.wait_for(proc.wait(), timeout=config.CLAUDE_TIMEOUT_SECONDS)
        else:
            await proc.wait()
    except asyncio.TimeoutError:
        _kill_group()
        await proc.wait()
        # Дочитываем потоки, чтобы в stderr попала диагностика proxy
        await asyncio.gather(*tasks, return_exceptions=True)
        tail = _tail_stderr(err_lines, proc_pid=proc.pid, limit=8)
        res = ClaudeResult()
        res.text = "⏱️ Превышен таймаут. Попробуй сократить запрос."
        # Подмешиваем причину из stderr, если proxy оставил диагностику.
        if tail:
            res.text += f"\n\n(диагностика: {tail})"
        res.exit_code = 1
        return res
    except asyncio.CancelledError:
        # Задачу отменили извне (напр. команда /kill) — убиваем процесс-кластер,
        # чтобы не осталось осиротевших дочерних процессов, и пробрасываем отмену дальше.
        _kill_group()
        await proc.wait()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        # Снимаем pid из реестра «запущенных ботом» — процесс завершён/убит.
        if proc_registry is not None:
            proc_registry.discard(proc.pid)
        # Гарантированно дочитываем потоки
        await asyncio.gather(*tasks, return_exceptions=True)

    res = _human_result(out_lines)
    res.exit_code = proc.returncode or res.exit_code

    # Если процесс завершился с ошибкой, но мы не получили читаемый текст, —
    # показываем хвост stderr, иначе бот молча вернёт пустоту вместо причины.
    if res.exit_code != 0 and not res.text:
        tail = _tail_stderr(err_lines, proc_pid=proc.pid)
        if tail:
            res.text = f"⚠️ Ошибка запуска Claude (код {res.exit_code}):\n{tail}"
    return res


def _tail_stderr(err_lines: list[str], limit: int = 3, proc_pid: Optional[int] = None) -> str:
    """Последние содержательные строки диагностики.

    Берём хвост stderr процесса, а если он пуст — дополнительно читаем
    журнальный файл обёртки (/tmp/claude-proxy-<pid>.log), куда та пишет
    реальную причину сбоя (например, ошибку авторизации или недоступность
    модели). Это важно: без него бот при сбое авторизации/сети молча висит до
    таймаута, а причина не видна.
    """
    lines = [ln.strip() for ln in err_lines if ln.strip()]
    # proxy печатает баннер + прогресс; берём хвост и убираем слишком длинные
    tail = "\n".join(lines[-limit * 4:])[:600]
    if not tail and proc_pid:
        tail = _read_proxy_log(proc_pid)
    return tail


def _read_proxy_log(proc_pid: int) -> str:
    """Прочитать свежий диагностический журнал обёртки (если есть).

    Лог-файл называется /tmp/claude-proxy-<pid>.log, куда обёртка
    перенаправляет вывод своего вспомогательного процесса. Нас интересуют
    строки [ERROR] и последние строки, а не баннер/прогресс.
    """
    import glob
    # Ищем лог по pid процесса (bash-обёртки) и «соседние», если pid уехал
    candidates = [f"/tmp/claude-proxy-{proc_pid}.log"] + sorted(
        glob.glob("/tmp/claude-proxy-*.log"), key=os.path.getmtime, reverse=True
    )
    for path in candidates[:4]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        # Строки с ERROR и последние INFO, убираем слишком длинные/шумные
        errs = [ln for ln in content.splitlines()
                if "[ERROR]" in ln and ln.strip()]
        tail = ("\n".join(errs[-4:]) if errs else "\n".join(
            content.splitlines()[-6:]))
        tail = tail.strip()[:600]
        if tail:
            return tail
    return ""


# ---------------------------------------------------------------------------
# Утилиты для Telegram-хендлеров
# ---------------------------------------------------------------------------

def format_command_hint() -> str:
    """Краткая справка по командам бота."""
    return (
        "📖 Доступные команды:\n"
        "/start — начать и выбрать проект\n"
        "/list — список доступных проектов\n"
        "/switch <имя> — переключиться на проект\n"
        "/new [имя] — создать новый проект\n"
        "/status — текущий проект и сессия\n"
        "/clear — отправить Claude (он сам сбросит/обработает контекст)\n"
        "/clearmedia — удалить скачанные вложения текущего проекта (в корзину/навсегда)\n"
        "/kill — убить зависшие сессии Claude, запущенные ботом\n"
        f"{config.SANDBOX_COMMAND} <команда/текст> — работа в любом чате (в каталоге SANDBOX_ROOT)\n"
        "/help — эта справка"
    )