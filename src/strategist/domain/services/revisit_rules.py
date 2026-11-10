"""«Что решать сейчас» и «какие основания устарели» — детерминированные запросы к журналу решения."""

from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum

from pydantic import Field

from strategist.domain.common import Entity
from strategist.domain.entities.case import DecisionCase
from strategist.domain.entities.claims import ClaimStatus, UnknownStatus
from strategist.domain.entities.decision import CommitmentStatus, DecisionStatus, TriggerKind


class AgendaKind(StrEnum):
    DEADLINE = "deadline"
    TRIGGER = "trigger"
    KILL = "kill_criterion"
    COMMITMENT = "commitment"
    REVISIT = "revisit_date"
    UNKNOWN = "unknown_deadline"
    REOPENED = "reopened_decision"


class AgendaItem(Entity):
    kind: AgendaKind
    title: str
    detail: str = ""
    due: date | None = None
    entity_id: str = ""
    urgency: int = 0


def what_to_decide_now(case: DecisionCase, today: date, horizon_days: int = 7) -> list[AgendaItem]:
    items: list[AgendaItem] = []
    soon = today + timedelta(days=horizon_days)
    decision = case.active_decision()

    if decision is None and case.frame and case.frame.deadline and case.frame.deadline <= soon:
        overdue = case.frame.deadline < today
        items.append(
            AgendaItem(
                kind=AgendaKind.DEADLINE,
                title="Срок решения " + ("прошёл" if overdue else "близко"),
                detail=case.frame.question,
                due=case.frame.deadline,
                urgency=3 if overdue else 2,
            )
        )
    for d in case.decisions:
        if d.status == DecisionStatus.REOPENED:
            items.append(
                AgendaItem(
                    kind=AgendaKind.REOPENED,
                    title=f"Решение {d.id} открыто заново",
                    detail="Сработали сигналы пересмотра — нужно подтвердить или сменить вариант",
                    entity_id=d.id,
                    urgency=3,
                )
            )
    if decision is not None:
        for trig in decision.triggers:
            if trig.fired:
                items.append(
                    AgendaItem(
                        kind=AgendaKind.TRIGGER,
                        title=f"Сработал сигнал: {trig.description}",
                        detail=trig.fired_reason,
                        entity_id=trig.id,
                        urgency=3,
                    )
                )
            elif trig.kind == TriggerKind.DATE and trig.due and trig.due <= today:
                items.append(
                    AgendaItem(
                        kind=AgendaKind.REVISIT,
                        title=f"Наступила дата пересмотра: {trig.description}",
                        due=trig.due,
                        entity_id=trig.id,
                        urgency=2,
                    )
                )
        for kc in decision.kill_criteria:
            if kc.deadline and kc.deadline <= today:
                items.append(
                    AgendaItem(
                        kind=AgendaKind.KILL,
                        title=f"Проверить kill-критерий: {kc.signal}",
                        detail=kc.threshold or "",
                        due=kc.deadline,
                        urgency=3,
                    )
                )
        for cm in decision.commitments:
            if cm.status == CommitmentStatus.OPEN and cm.due and cm.due < today:
                items.append(
                    AgendaItem(
                        kind=AgendaKind.COMMITMENT,
                        title=f"Просрочено обязательство: {cm.description}",
                        detail=f"ответственный: {cm.owner or '—'}",
                        due=cm.due,
                        entity_id=cm.id,
                        urgency=2,
                    )
                )
        if decision.revisit_date and decision.revisit_date <= today:
            items.append(
                AgendaItem(
                    kind=AgendaKind.REVISIT,
                    title="Наступила плановая дата пересмотра решения",
                    due=decision.revisit_date,
                    entity_id=decision.id,
                    urgency=2,
                )
            )
    for exp in case.experiments:
        if exp.kill_criterion and exp.kill_criterion.deadline and exp.kill_criterion.deadline <= today:
            items.append(
                AgendaItem(
                    kind=AgendaKind.KILL,
                    title=f"Kill-критерий эксперимента {exp.id}: {exp.kill_criterion.signal}",
                    due=exp.kill_criterion.deadline,
                    entity_id=exp.id,
                    urgency=2,
                )
            )
    for unk in case.unknowns:
        if unk.status == UnknownStatus.OPEN and unk.deadline and unk.deadline < today:
            items.append(
                AgendaItem(
                    kind=AgendaKind.UNKNOWN,
                    title=f"Не закрыт пробел: {unk.question}",
                    due=unk.deadline,
                    entity_id=unk.id,
                    urgency=2 if unk.flips_decision else 1,
                )
            )
    return sorted(items, key=lambda i: (-i.urgency, i.due or date.max))


class StaleGround(Entity):
    claim_id: str
    text: str
    reasons: list[str] = Field(default_factory=list)
    score: float = 0.0
    load_bearing: bool = False
    vulnerable: bool = False


def stale_grounds(case: DecisionCase, today: date) -> list[StaleGround]:
    """Основания, которые устарели: истёк срок, источник заменён или отозван, записан опровергатель."""
    result: list[StaleGround] = []
    for claim in case.claims:
        if claim.status in (ClaimStatus.RETRACTED, ClaimStatus.SUPERSEDED):
            continue
        reasons: list[str] = []
        if claim.valid_until and claim.valid_until < today:
            reasons.append(f"истёк срок актуальности ({claim.valid_until.isoformat()})")
        for ev_id in claim.evidence_ids:
            ev = case.evidence_item(ev_id)
            src = case.source(ev.source_id) if ev else None
            if src is not None and src.retracted:
                reasons.append(f"источник {src.id} отозван")
            elif src is not None and src.superseded_by:
                reasons.append(f"источник {src.id} заменён на {src.superseded_by}")
        for defeater in claim.defeaters:
            reasons.append(f"опровергатель ({defeater.kind.value}): {defeater.description}")
        if claim.status == ClaimStatus.SUSPECT:
            reasons.append("помечено «под подозрением» после новых данных")
        if not reasons:
            continue
        lb = 1.0 if claim.load_bearing else 0.3
        vu = 1.0 if claim.vulnerable else 0.3
        conf = 1.0 - (claim.confidence if claim.confidence is not None else 0.5)
        result.append(
            StaleGround(
                claim_id=claim.id,
                text=claim.text,
                reasons=reasons,
                score=round(lb * vu * conf, 4),
                load_bearing=claim.load_bearing,
                vulnerable=claim.vulnerable,
            )
        )
    return sorted(result, key=lambda s: -s.score)
