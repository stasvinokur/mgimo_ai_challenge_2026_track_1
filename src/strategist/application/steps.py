"""Идемпотентные шаги по принципу Temporal: завершённый шаг при повторе не выполняется.

Журнал пишет intent и done с хешами входа и выхода. Ключ — (кейс, прогон, шаг).
Шаг с intent без done: если у него нет внешнего побочного эффекта — выполняется заново;
если есть — повтор только после подтверждения человека (NeedsConfirmation).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from strategist.application.context import AppContext
from strategist.application.errors import NeedsConfirmation
from strategist.application.ports import JournalStatus

# Исключения-паузы оркестратора (шлюз человека через interrupt()): это не сбой шага. Application-слой не знает
# о LangGraph, поэтому тип регистрирует инфраструктура (register_suspension в модуле графа).
_SUSPENSIONS: tuple[type[BaseException], ...] = ()


def register_suspension(exc_type: type[BaseException]) -> None:
    global _SUSPENSIONS
    if exc_type not in _SUSPENSIONS:
        _SUSPENSIONS = (*_SUSPENSIONS, exc_type)


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def run_step(
    ctx: AppContext,
    case_id: str,
    step: str,
    inputs: Any,
    fn: Callable[[], dict[str, Any]],
    *,
    side_effect: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    input_hash = stable_hash(inputs)
    existing = ctx.journal.get(case_id, ctx.run_id, step)
    if existing is not None:
        if existing.status == JournalStatus.DONE and existing.input_hash == input_hash:
            ctx.emit("step.skipped", case_id, step=step, reason="уже выполнен")
            return existing.payload
        if existing.status == JournalStatus.INTENT and existing.side_effect and not confirmed:
            raise NeedsConfirmation(step)
    ctx.journal.intent(case_id, ctx.run_id, step, input_hash, side_effect=side_effect, at=ctx.clock.now())
    ctx.emit("step.started", case_id, step=step)
    try:
        payload = fn()
    except Exception as exc:
        if _SUSPENSIONS and isinstance(exc, _SUSPENSIONS):
            # Шаг ждёт человека: остаётся intent и при ответе выполняется заново (побочных эффектов до шлюза нет)
            ctx.emit("step.paused", case_id, step=step)
            raise
        ctx.journal.fail(case_id, ctx.run_id, step, f"{type(exc).__name__}: {exc}", ctx.clock.now())
        ctx.emit("step.failed", case_id, step=step, error=str(exc)[:300])
        raise
    ctx.journal.done(case_id, ctx.run_id, step, stable_hash(payload), payload, ctx.clock.now())
    ctx.emit("step.completed", case_id, step=step)
    return payload
