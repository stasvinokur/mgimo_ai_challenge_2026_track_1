"""Выбор варианта и фиксация решения (ADR + MADR Confirmation + DACI + RAND ABP + kill-критерии)."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from strategist.application.context import AppContext
from strategist.application.digests import claims_digest, frame_digest
from strategist.application.dto.llm_outputs import DecisionRecordDTO
from strategist.application.llm_calls import ask_structured
from strategist.application.ports import GateKind, GateRequest, GateResponse
from strategist.domain.entities import (
    ClaimType,
    Commitment,
    Daci,
    Decision,
    DecisionCase,
    DecisionStatus,
    OptionStatus,
    Trigger,
    TriggerKind,
)
from strategist.domain.services.ranking import aggregate_panel


def option_choice_request(case: DecisionCase, default: str | None) -> GateRequest:
    panel = aggregate_panel(case.role_scores)
    scores = {r.option_id: r for r in panel.rankings}
    lines = ["| Вариант | Тип | Панель (среднее / голоса) | Сильнейшее возражение |", "|---|---|---|---|"]
    for o in case.options:
        if o.status == OptionStatus.CLONE:
            continue
        r = scores.get(o.id)
        top = max(case.objections_for(o.id), key=lambda x: x.strength or 0, default=None)
        lines.append(
            f"| [{o.id}] {o.title} | {o.kind.label_ru} | "
            + (f"{r.mean:.1f} / {r.votes}" if r else "—")
            + f" | {(top.text[:120] + ' (' + str(top.strength) + '/5)') if top else '—'} |"
        )
    if case.stress:
        lines.append(f"\nСтресс-тест: {case.stress.summary}")
        lines += [f"- {b.statement}" for b in case.stress.boundaries]
    if panel.order_sensitive:
        lines.append(f"\n⚠ {panel.note}")
    return GateRequest(
        gate=GateKind.OPTION_CHOICE,
        case_id=case.id,
        title="Выбор варианта",
        summary="\n".join(lines),
        choices=[o.id for o in case.options if o.status != OptionStatus.CLONE],
        default_choice=default,
    )


def apply_option_choice(
    ctx: AppContext, case: DecisionCase, response: GateResponse, default: str | None
) -> str:
    valid = {o.id for o in case.options if o.status != OptionStatus.CLONE}
    chosen = response.choice if response.choice in valid else default
    if chosen is None:
        raise ValueError("Не выбран вариант")
    boundaries = case.stress.boundaries if case.stress else []
    for o in case.options:
        if o.status == OptionStatus.CLONE:
            continue
        if o.id == chosen:
            o.status = OptionStatus.SELECTED
            continue
        o.status = OptionStatus.REJECTED
        o.rejected_reason = response.comment or "Не выбран руководителем на шлюзе"
        flips = [b.statement for b in boundaries if o.id in b.winners_by_level.values()]
        o.revisit_when = "; ".join(flips) or (o.kill_criterion.signal if o.kill_criterion else None)
    case.selected_option_id = chosen
    ctx.emit("option.chosen", case.id, option=chosen, by_default=response.choice not in valid)
    return chosen


def commit_request(case: DecisionCase) -> GateRequest:
    option = case.option(case.selected_option_id or "")
    exps = "\n".join(
        f"- [{e.id}] {e.hypothesis} (kill: {e.kill_criterion.signal if e.kill_criterion else '—'})"
        for e in case.experiments
    )
    return GateRequest(
        gate=GateKind.DECISION_COMMIT,
        case_id=case.id,
        title="Фиксация решения",
        summary=f"Вариант: {option.title if option else '—'}\n\nПлан проверки:\n{exps or '—'}",
        choices=["approve", "reject"],
        default_choice="approve",
    )


def _snapshot(case: DecisionCase, claim_ids: list[str]) -> dict[str, str]:
    snap = {}
    for cid in claim_ids:
        claim = case.claim(cid)
        if claim is not None:
            snap[cid] = hashlib.sha256(claim.text.encode()).hexdigest()[:12]
    return snap


def commit_decision(ctx: AppContext, case: DecisionCase, response: GateResponse) -> Decision:
    option = case.option(case.selected_option_id or "")
    if option is None:
        raise ValueError("Вариант не выбран")
    dto, _ = ask_structured(
        ctx,
        task="experiments",
        prompt="decision_record",
        values={
            "frame": frame_digest(case),
            "option": f"[{option.id}] {option.title}: {option.summary}\n{option.rationale}",
            "claims": claims_digest(case, [*option.supporting_claim_ids, *option.assumption_claim_ids]),
            "experiments": "\n".join(
                f"[{e.id}] {e.hypothesis}; правило: {e.decision_rule}" for e in case.experiments
            ),
            "stress": case.stress.summary if case.stress else "—",
        },
        schema=DecisionRecordDTO,
        case_id=case.id,
        step="commit",
    )
    today = ctx.clock.today()
    load_bearing = [c.id for c in case.claims if c.load_bearing and c.is_active]
    vulnerable = [c.id for c in case.claims if c.vulnerable and c.is_active and c.id in load_bearing] or [
        cid for cid in option.assumption_claim_ids
    ]
    kill_deadlines = [
        k.deadline
        for k in [option.kill_criterion, *(e.kill_criterion for e in case.experiments)]
        if k and k.deadline
    ]
    revisit = min(kill_deadlines) if kill_deadlines else today + timedelta(days=30)

    reserved: list[str] = []

    def tid() -> str:
        new = case.new_id("trigger", reserved=reserved)
        reserved.append(new)
        return new

    triggers: list[Trigger] = []
    for cid in option.assumption_claim_ids:
        claim = case.claim(cid)
        if claim is None:
            continue
        triggers.append(
            Trigger(
                id=tid(),
                kind=TriggerKind.SIGNAL,
                # Что отслеживаем, а не что случилось: «Опровергнуто допущение» модель проверки сигналов
                # читала как наступившее событие (TASK-38)
                description=f"Допущение решения [{cid}]: {claim.text[:140]}",
                condition=claim.what_would_change or "Новые данные противоречат допущению",
                assumption_claim_ids=[cid],
            )
        )
    for cid in load_bearing:
        claim = case.claim(cid)
        if (
            claim is not None
            and claim.type in (ClaimType.FACT, ClaimType.DERIVED_METRIC)
            and cid not in option.assumption_claim_ids
        ):
            triggers.append(
                Trigger(
                    id=tid(),
                    kind=TriggerKind.EVIDENCE,
                    description=f"Изменилось несущее основание [{cid}]: {claim.text[:140]}",
                    condition="Новый источник опровергает или заменяет основание",
                    assumption_claim_ids=[cid],
                )
            )
    if option.kill_criterion:
        triggers.append(
            Trigger(
                id=tid(),
                kind=TriggerKind.KILL,
                description=f"Kill-критерий: {option.kill_criterion.signal}",
                condition=option.kill_criterion.threshold or "",
                due=option.kill_criterion.deadline,
            )
        )
    triggers.append(
        Trigger(id=tid(), kind=TriggerKind.DATE, description="Плановый пересмотр решения", due=revisit)
    )

    commitments = []
    creserved: list[str] = []
    for text in dto.commitments:
        cm = Commitment(id=case.new_id("commitment", reserved=creserved), description=text, due=revisit)
        creserved.append(cm.id)
        commitments.append(cm)
    for exp in case.experiments:
        cm = Commitment(
            id=case.new_id("commitment", reserved=creserved),
            description=f"Провести эксперимент {exp.id}: {exp.metric}",
            owner=exp.owner,
            due=exp.kill_criterion.deadline if exp.kill_criterion else revisit,
        )
        creserved.append(cm.id)
        commitments.append(cm)

    previous = case.active_decision()
    decision = Decision(
        id=case.new_id("decision"),
        selected_option_id=option.id,
        rejected_option_ids=[o.id for o in case.options if o.status == OptionStatus.REJECTED],
        rationale=dto.rationale,
        evidence_snapshot=_snapshot(case, load_bearing),
        load_bearing_claim_ids=load_bearing,
        vulnerable_claim_ids=vulnerable,
        daci=Daci(
            driver="ИИ-стратег (агент)",
            approver=response.approver
            or (case.frame.decision_owner if case.frame else "")
            or "руководитель продукта",
            contributors=["финансы", "маркетинг", "адвокат клиента (панель ролей)"],
            informed=[case.frame.recipient] if case.frame and case.frame.recipient else [],
        ),
        decision_deadline=case.frame.deadline if case.frame else None,
        revisit_date=revisit,
        metrics=case.product.metrics if case.product else [],
        confirmation=dto.confirmation,
        triggers=triggers,
        kill_criteria=[
            k for k in [option.kill_criterion, *(e.kill_criterion for e in case.experiments)] if k
        ],
        commitments=commitments,
        shaping_actions=dto.shaping_actions,
        hedging_actions=dto.hedging_actions,
        supersedes=previous.id if previous else None,
        created_at=ctx.clock.now(),
    )
    if previous is not None:
        previous.status = DecisionStatus.SUPERSEDED
        previous.superseded_by = decision.id
    case.decisions.append(decision)
    ctx.emit("decision.committed", case.id, decision=decision.id, option=option.id, triggers=len(triggers))
    return decision
