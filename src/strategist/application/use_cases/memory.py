"""Память между сеансами: прежний ответ агента — не факт.

У каждой записи — происхождение (origin), эпистемический статус и две оси времени: когда сведение
верно (valid_from/valid_until) и когда агент его узнал (recorded_at, журнал изменений в хранилище).
Запись с origin=agent_inference служит доказательством только после явного одобрения пользователя;
одобрение журналируется событием memory.approved. Отозванные и исправленные записи не удаляются, но
доказательством не служат никогда — так атака «внедрить запись через обычный запрос» (MINJA) не
превращает чужую подсказку в факт.
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel

from strategist.application.context import AppContext
from strategist.application.ports import MemoryRecord, MemoryStatus, SearchHit
from strategist.domain.entities import Origin

MEMORY_KIND = "memory"
_MEMORY_ID = re.compile(r"^MEM-(\d+)$")

# Эпистемический статус по умолчанию: вывод агента и внешние данные без перепроверки — «не проверено».
_DEFAULT_EPISTEMIC = {
    Origin.USER_STATED: "verified",
    Origin.AGENT_OUTPUT_ACCEPTED_BY_USER: "verified",
    Origin.SOURCE_DOCUMENT: "verified",
    Origin.EXTERNAL_DATA: "unverified",
    Origin.AGENT_INFERENCE: "unverified",
}


class RecalledItem(BaseModel):
    """Найденный документ базы знаний; для записей памяти — можно ли опираться на неё как на доказательство."""

    hit: SearchHit
    memory: MemoryRecord | None = None
    evidence_blocked: bool = False
    reason: str = ""


def _next_id(ctx: AppContext) -> str:
    numbers = [
        int(m.group(1))
        for rec in ctx.knowledge.memories(include_inactive=True)
        if (m := _MEMORY_ID.match(rec.id))
    ]
    return f"MEM-{max(numbers, default=0) + 1:03d}"


def _find(ctx: AppContext, memory_id: str) -> MemoryRecord:
    for rec in ctx.knowledge.memories(include_inactive=True):
        if rec.id == memory_id:
            return rec
    raise ValueError(f"Нет записи памяти {memory_id}")


def evidence_block_reason(record: MemoryRecord, today: date) -> str:
    """Почему запись нельзя использовать как доказательство ("" — можно)."""
    if record.status == MemoryStatus.RETRACTED:
        return f"запись отозвана: {record.reason}" if record.reason else "запись отозвана"
    if record.status == MemoryStatus.CORRECTED:
        return f"запись исправлена, актуальная версия — {record.superseded_by}"
    if not record.can_be_evidence:
        return "вывод агента без одобрения пользователя — не доказательство"
    if record.valid_from and today < record.valid_from:
        return f"сведение действует только с {record.valid_from.isoformat()}"
    if record.valid_until and today > record.valid_until:
        return f"срок действия сведения истёк {record.valid_until.isoformat()}"
    return ""


def remember(
    ctx: AppContext,
    case_id: str | None,
    text: str,
    origin: Origin | str,
    *,
    valid_from: date | None = None,
    valid_until: date | None = None,
    epistemic_status: str | None = None,
) -> MemoryRecord:
    """Новая запись памяти с происхождением; ID вида MEM-001, время записи — из ctx.clock."""
    text = text.strip()
    if not text:
        raise ValueError("Пустая запись памяти")
    if valid_from and valid_until and valid_until < valid_from:
        raise ValueError("valid_until раньше valid_from")
    origin = Origin(origin)
    record = MemoryRecord(
        id=_next_id(ctx),
        case_id=case_id,
        text=text,
        origin=origin,
        epistemic_status=epistemic_status or _DEFAULT_EPISTEMIC.get(origin, "unverified"),
        valid_from=valid_from,
        valid_until=valid_until,
        recorded_at=ctx.clock.now(),
    )
    ctx.knowledge.remember(record)
    ctx.emit("memory.recorded", case_id or "", memory_id=record.id, origin=origin.value)
    return record


def approve_memory(ctx: AppContext, memory_id: str) -> None:
    """Явное одобрение пользователем: после него вывод агента может служить доказательством."""
    record = _find(ctx, memory_id)
    if record.status != MemoryStatus.ACTIVE:
        raise ValueError(f"Запись {memory_id} {record.status.value} — одобрить можно только активную")
    at = ctx.clock.now()
    ctx.knowledge.approve(memory_id, at)
    ctx.emit(
        "memory.approved",
        record.case_id or "",
        memory_id=memory_id,
        origin=record.origin.value,
        approved_at=at.isoformat(),
    )


def retract_memory(ctx: AppContext, memory_id: str, reason: str) -> None:
    """Отзыв устаревшей или ошибочной записи: она остаётся в истории, но больше не доказательство."""
    if not reason.strip():
        raise ValueError("Укажите причину отзыва записи памяти")
    record = _find(ctx, memory_id)
    ctx.knowledge.retract(memory_id, reason.strip(), ctx.clock.now())
    ctx.emit("memory.retracted", record.case_id or "", memory_id=memory_id, reason=reason.strip())


def correct_memory(
    ctx: AppContext, memory_id: str, new_text: str, *, origin: Origin | str | None = None
) -> MemoryRecord:
    """Исправление: новая активная запись, старая → CORRECTED со ссылкой superseded_by.

    Происхождение по умолчанию наследуется (исправление само по себе не повышает доверие); одобрение
    не переносится — исправленный вывод агента снова требует одобрения пользователя.
    """
    old = _find(ctx, memory_id)
    if old.status != MemoryStatus.ACTIVE:
        hint = f"; исправляйте {old.superseded_by}" if old.superseded_by else ""
        raise ValueError(f"Запись {memory_id} {old.status.value}{hint}")
    text = new_text.strip()
    if not text:
        raise ValueError("Пустой текст исправления")
    new_origin = Origin(origin) if origin is not None else old.origin
    new = MemoryRecord(
        id=_next_id(ctx),
        case_id=old.case_id,
        text=text,
        origin=new_origin,
        epistemic_status=_DEFAULT_EPISTEMIC.get(new_origin, "unverified"),
        valid_from=old.valid_from,
        valid_until=old.valid_until,
        recorded_at=ctx.clock.now(),
    )
    ctx.knowledge.correct(memory_id, new)
    ctx.emit("memory.corrected", old.case_id or "", memory_id=memory_id, superseded_by=new.id)
    return new


def usable_as_evidence(ctx: AppContext, case_id: str | None) -> list[MemoryRecord]:
    """Только записи, на которые можно опираться: активные, в сроке действия; agent_inference — одобренные."""
    today = ctx.clock.today()
    return [m for m in ctx.knowledge.memories(case_id) if not evidence_block_reason(m, today)]


def recall(ctx: AppContext, query: str, case_id: str | None = None, k: int = 8) -> list[RecalledItem]:
    """Поиск в базе знаний; найденные записи памяти помечаются, если на них нельзя опираться."""
    hits = ctx.knowledge.search(query, k=k, case_id=case_id)
    if not hits:
        return []
    records = {m.id: m for m in ctx.knowledge.memories(include_inactive=True)}
    today = ctx.clock.today()
    items: list[RecalledItem] = []
    for hit in hits:
        record = records.get(hit.doc_id)
        if hit.kind != MEMORY_KIND and record is None:
            items.append(RecalledItem(hit=hit))
            continue
        if record is None:
            items.append(
                RecalledItem(hit=hit, evidence_blocked=True, reason="запись памяти не найдена в хранилище")
            )
            continue
        reason = evidence_block_reason(record, today)
        items.append(RecalledItem(hit=hit, memory=record, evidence_blocked=bool(reason), reason=reason))
    return items
