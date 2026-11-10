"""Системные адаптеры среды: часы и шина событий в процессе (хуки подписываются на события)."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime

from strategist.application.ports import Event

log = logging.getLogger("strategist.events")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def today(self) -> date:
        return datetime.now(UTC).date()


class InProcessEventBus:
    """Синхронная шина: обработчик по типу события или «*» для всех. Ошибка хука не роняет работу."""

    def __init__(self) -> None:
        self.handlers: dict[str, list[Callable[[Event], None]]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable[[Event], None]) -> None:
        self.handlers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        for handler in [*self.handlers.get(event.type, []), *self.handlers.get("*", [])]:
            try:
                handler(event)
            except Exception:  # хук не должен ломать основной процесс
                log.exception("Ошибка обработчика события %s", event.type)


class OffsetClock:
    """Часы со сдвигом к моменту записи кассет: в воспроизведении «сегодня» совпадает с днём записи,
    поэтому промпты (где есть дата) дают те же ключи кассет."""

    def __init__(self, start_at: datetime) -> None:
        self.offset = start_at - datetime.now(UTC)

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def today(self) -> date:
        return self.now().date()
