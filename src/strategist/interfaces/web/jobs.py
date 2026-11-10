"""Фоновые прогоны и живые события для веб-панели.

Исполнитель (LangGraph) блокирует вызов до следующего шлюза или конца прогона, а контекст приложения общий
(`ctx.run_id`), поэтому прогоны идут по одному на процесс: второй запуск, пока идёт первый, получает «занято».
События агента приходят из рабочих потоков; один подписчик шины (отписки у неё нет) раздаёт их всем открытым
потокам SSE через их очереди — потокобезопасно, через цикл событий сервера.
"""

from __future__ import annotations

import asyncio
import json
import threading
import traceback
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class Busy(RuntimeError):
    """Уже идёт другой прогон — кейсы и прогоны в одном процессе выполняются по одному."""


@dataclass
class Job:
    case_id: str
    kind: str  # run | gate | revise | contest
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    error: str = ""

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "running": self.running,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
        }


class JobRunner:
    """Один фоновый поток на процесс. `wait()` — для тестов и сценариев без цикла событий."""

    def __init__(self, on_done: Callable[[Job], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.current: Job | None = None
        self._on_done = on_done

    def submit(self, case_id: str, kind: str, fn: Callable[[], Any]) -> Job:
        with self._lock:
            if self.current is not None and self.current.running:
                raise Busy(f"идёт прогон кейса {self.current.case_id} ({self.current.kind})")
            job = Job(case_id=case_id, kind=kind)
            self.current = job

        def work() -> None:
            try:
                fn()
            except Exception as exc:  # ошибка прогона — в статус задания, а не в лог сервера
                job.error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            finally:
                job.finished_at = datetime.now(UTC)
                if self._on_done is not None:
                    self._on_done(job)

        self._thread = threading.Thread(target=work, name=f"strategist-{kind}-{case_id}", daemon=True)
        self._thread.start()
        return job

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


class EventHub:
    """Раздача событий открытым потокам SSE. publish() можно звать из любого потока."""

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, default=str)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            clients = list(self._clients)
        for queue in clients:
            loop.call_soon_threadsafe(queue.put_nowait, line)

    async def stream(self) -> AsyncIterator[str]:
        self.bind(asyncio.get_running_loop())
        queue: asyncio.Queue[str] = asyncio.Queue()
        with self._lock:
            self._clients.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            with self._lock:
                self._clients.discard(queue)
