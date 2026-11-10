"""ReviseOnNewEvidence: точечный пересмотр вместо переписывания стратегии.

Новый материал → цитаты и утверждения → связи с прежними строками (противоречит / заменяет) →
обход в ширину по графу зависимостей помечает затронутые узлы «под подозрением» → модель пересматривает
только их (unchanged / revised / retracted) → раздел «Что изменилось». Нетронутое не трогаем —
так агент не наносит «сопутствующего ущерба» и не держится за отозванное (AGM-Bench).
"""

from __future__ import annotations

import re

from strategist.application.context import AppContext
from strategist.application.digests import claims_digest, evidence_digest, frame_digest
from strategist.application.dto.llm_outputs import RelationDTO, RevisionClaimsDTO, RevisionVerdictsDTO
from strategist.application.llm_calls import ask_structured
from strategist.application.ports import DecisionKind, DecisionQuestion, LedgerEntry
from strategist.application.use_cases.claims import claim_from_dto, classify_types, verify_claims
from strategist.application.use_cases.collect import ingest_source, merge_ingest
from strategist.application.use_cases.options import run_stress_test
from strategist.application.use_cases.start_case import register_materials
from strategist.domain.common import clip_words
from strategist.domain.entities import (
    Claim,
    ClaimStatus,
    ClaimType,
    Decision,
    DecisionCase,
    DecisionStatus,
    Defeater,
    DefeaterKind,
    NodeRevision,
    NodeVerdict,
    OptionStatus,
    RevisionRecord,
    Trigger,
    TriggerKind,
)
from strategist.domain.services.dependency_graph import PRODUCT_NODE, build_graph

REVIEW_KINDS = (
    "claim",
    "option",
    "objection",
    "product",
    "feature",
    "comms",
    "promise",
    "experiment",
    "decision",
)
UNTOUCHED_KINDS = ("claim", "option", "feature", "promise", "experiment")


def _node_text(case: DecisionCase, node_id: str, kind: str) -> str:
    if kind == "claim" and (c := case.claim(node_id)):
        return f"{c.type.label_ru}: {c.text}"
    if kind == "option" and (o := case.option(node_id)):
        return f"вариант «{o.title}»: {o.summary}"
    if kind == "objection":
        obj = next((x for x in case.objections if x.id == node_id), None)
        return f"возражение: {obj.text}" if obj else node_id
    if kind == "product" and case.product:
        return f"направление продукта: {case.product.direction}"
    if kind == "feature" and case.product:
        f = next((x for x in case.product.features if x.id == node_id), None)
        return f"функция «{f.name}»: {f.description}" if f else node_id
    if kind == "comms" and case.comms:
        return f"ключевое сообщение: {case.comms.key_message}"
    if kind == "promise" and case.comms:
        p = next((x for x in case.comms.promises if x.id == node_id), None)
        return f"обещание: {p.text}" if p else node_id
    if kind == "experiment":
        e = next((x for x in case.experiments if x.id == node_id), None)
        return f"эксперимент: {e.hypothesis}" if e else node_id
    if kind == "decision":
        d = next((x for x in case.decisions if x.id == node_id), None)
        return f"решение: {d.rationale[:300]}" if d else node_id
    return node_id


def _apply_verdict(
    ctx: AppContext, case: DecisionCase, node_id: str, kind: str, verdict: NodeVerdict, text: str
) -> str:
    """Применяет вердикт к узлу; возвращает прежний текст."""
    before = _node_text(case, node_id, kind)
    if verdict == NodeVerdict.UNCHANGED:
        if kind == "claim" and (c := case.claim(node_id)) and c.status == ClaimStatus.SUSPECT:
            c.status = ClaimStatus.ACTIVE
        return before
    if kind == "claim" and (c := case.claim(node_id)):
        if verdict == NodeVerdict.RETRACTED:
            c.status = ClaimStatus.RETRACTED
        elif text:
            c.text, c.status, c.updated_at = text, ClaimStatus.ACTIVE, ctx.clock.now()
        ctx.ledger.append(
            [
                LedgerEntry(
                    case_id=case.id,
                    claim_id=c.id,
                    action=verdict.value,
                    at=ctx.clock.now(),
                    actor="revision",
                    snapshot={"before": before, "after": text},
                )
            ]
        )
    elif kind == "option" and (o := case.option(node_id)):
        if verdict == NodeVerdict.RETRACTED:
            o.status = OptionStatus.REJECTED
            o.rejected_reason = "Снят при пересмотре: основания отозваны"
        elif text:
            o.summary = text
    elif kind == "product" and case.product and text:
        case.product.direction = text
    elif kind == "feature" and case.product:
        for f in case.product.features:
            if f.id == node_id and text:
                f.description = text
    elif kind == "comms" and case.comms and text:
        case.comms.key_message = text
    elif kind == "promise" and case.comms:
        if verdict == NodeVerdict.RETRACTED:
            case.comms.promises = [p for p in case.comms.promises if p.id != node_id]
        else:
            for p in case.comms.promises:
                if p.id == node_id and text:
                    p.text = text
    elif kind == "experiment":
        for e in case.experiments:
            if e.id == node_id and text:
                e.hypothesis = text
    elif kind == "decision":
        for d in case.decisions:
            if d.id == node_id and d.status == DecisionStatus.ACCEPTED:
                d.status = DecisionStatus.REOPENED
    elif kind == "objection":
        for obj in case.objections:
            if obj.id == node_id and text:
                obj.text = text
    return before


MEASUREMENTS = (ClaimType.FACT, ClaimType.DERIVED_METRIC)

SIGNAL_OPTIONS = {
    "not_fired": "Не сработал: условие в новых данных не выполнено, данные подтверждают допущение или их недостаточно",
    "fired": "Сработал: числа в новых строках показывают, что условие выполнено или порог пересечён",
}


def _signal_state(description: str, condition: str, new_rows: str) -> str:
    """Сигнал подаётся как то, что проверяем, а срабатывание — только через условие.

    Прежняя подача «Сигнал: Опровергнуто допущение …» звучала как свершившийся факт: в демо A/B-тест подтвердил
    выбранный вариант, а два сигнала «сработали» (проба: 0,62 и 0,72 против not_fired 0,98 и 0,80 при этой
    подаче). Старые формулировки в журналах решений приводятся к новой."""
    watched = re.sub(r"^Опровергнуто допущение", "допущение решения", description)
    watched = re.sub(r"^Допущение решения", "допущение решения", watched)
    return (
        f"Что проверяем: {watched}\nСигнал срабатывает, только если: {condition or '—'}\n\n"
        f"Новые строки реестра:\n{new_rows}"
    )


def check_signals(
    ctx: AppContext, case: DecisionCase, decision: Decision, new_claim_ids: list[str]
) -> list[str]:
    """Сигналы пересмотра и kill-критерии решения против новых строк реестра — «умный if» через decide().

    Без этой проверки kill-критерий с порогом («отток при продлении выше 21,7%») не замечается, пока модель
    извлечения не свяжет новые данные с допущением. Kill-критерий без сигнала-триггера добавляется в журнал
    решения как сработавший триггер. Возвращает ID сработавших триггеров."""
    if not new_claim_ids:
        return []
    new_rows = claims_digest(case, new_claim_ids)
    checks: list[tuple[str, str, Trigger | None]] = [
        (t.description, t.condition, t)
        for t in decision.triggers
        if t.kind in (TriggerKind.SIGNAL, TriggerKind.KILL) and not t.fired
    ]
    known = {t.description for t in decision.triggers}
    checks += [
        (f"Kill-критерий: {k.signal}", k.threshold, None)
        for k in decision.kill_criteria
        if f"Kill-критерий: {k.signal}" not in known
    ]
    if not checks:
        return []
    questions = [
        DecisionQuestion(
            id=f"{case.id}:{decision.id}:signal:{i}",
            kind=DecisionKind.CHOICE,
            instructions="Сработал ли сигнал пересмотра решения по новым данным? Считай пороги по числам из новых строк.",
            state=_signal_state(desc, cond, new_rows),
            options=SIGNAL_OPTIONS,
            task="signal_check",
            case_id=case.id,
            run_id=ctx.run_id,
            step="revision:signals",
        )
        for i, (desc, cond, _) in enumerate(checks)
    ]
    threshold = float(ctx.setting("decide.confidence_threshold", 0.6))
    fired: list[str] = []
    for (desc, cond, trig), result in zip(checks, ctx.decision.decide_many(questions), strict=True):
        if result.choice != "fired" or result.confidence < threshold:
            continue
        if trig is None:
            trig = Trigger(id=case.new_id("trigger"), kind=TriggerKind.KILL, description=desc, condition=cond)
            decision.triggers.append(trig)
        trig.fired, trig.fired_at = True, ctx.clock.now()
        trig.fired_reason = f"Новые данные пересекают сигнал (уверенность {result.confidence:.2f})"
        fired.append(trig.id)
    if fired:
        ctx.emit("revision.signals_fired", case.id, triggers=fired)
    return fired


def apply_relations(
    ctx: AppContext,
    case: DecisionCase,
    relations: list[RelationDTO],
    key_to_id: dict[str, str],
    claims_before: set[str],
) -> list[str]:
    """Связи новых строк с прежними → основания под вопросом.

    «Заменяет» — только более новое измерение той же величины: измерение не заменяет порог, цель, прогноз или
    допущение (новые данные с ними сравниваются, а не отменяют их). Такие связи игнорируются и журналируются.
    """
    invalidated: list[str] = []
    for rel in relations:
        target = case.claim(rel.existing_claim_id)
        new_id = key_to_id.get(rel.new_claim_key, rel.new_claim_key)
        if target is None or target.id not in claims_before:
            continue
        if rel.relation == "contradicts":
            target.defeaters.append(
                Defeater(
                    kind=DefeaterKind.REBUTTING,
                    description=f"{new_id}: {rel.explanation}"[:300],
                    evidence_id=(case.claim(new_id).evidence_ids[:1] or [None])[0]
                    if case.claim(new_id)
                    else None,
                    recorded_at=ctx.clock.now(),
                )
            )
            target.status = ClaimStatus.SUSPECT
            invalidated.append(target.id)
        elif rel.relation == "supersedes":
            newer = case.claim(new_id)
            if target.type not in MEASUREMENTS or (newer is not None and newer.type not in MEASUREMENTS):
                ctx.emit(
                    "revision.relation_ignored",
                    case.id,
                    existing=target.id,
                    new=new_id,
                    relation="supersedes",
                    reason="заменить можно только измерение более новым измерением",
                )
                continue
            target.status = ClaimStatus.SUPERSEDED
            target.superseded_by = new_id
            invalidated.append(target.id)
    return list(dict.fromkeys(invalidated))


def _refine_reopened(
    ctx: AppContext, case: DecisionCase, option_id: str, invalidated: list[str], new_rows: list[Claim]
) -> NodeRevision:
    """Вариант, открытый заново, писался до новых данных: его содержание уточняется по ним (например, для кого
    повышать цену). Тот же промпт пересмотра узлов, один узел."""
    text = _node_text(case, option_id, "option")
    verdicts, _ = ask_structured(
        ctx,
        task="revise",
        prompt="revise_nodes",
        values={
            "frame": frame_digest(case),
            "invalidated": claims_digest(case, invalidated) if invalidated else "—",
            "new_claims": "\n".join(f"[{c.id}] {c.text}" for c in new_rows) or "—",
            "nodes": f"{option_id} (option): {text}",
        },
        schema=RevisionVerdictsDTO,
        case_id=case.id,
        step="revision:reopened",
    )
    v = next((x for x in verdicts.verdicts if x.node_id == option_id), None)
    verdict = NodeVerdict.REVISED if v and v.verdict == "revised" and v.new_text else NodeVerdict.UNCHANGED
    before = _apply_verdict(ctx, case, option_id, "option", verdict, v.new_text if v else "")
    return NodeRevision(
        node_id=option_id,
        node_kind="option",
        verdict=verdict,
        before=before[:400],
        after=(v.new_text if v and verdict == NodeVerdict.REVISED else "")[:400],
        reason=clip_words(v.reason if v else "открыт заново; уточнение не потребовалось", 300),
    )


def revise_on_new_evidence(ctx: AppContext, case: DecisionCase, new_paths: list[str]) -> RevisionRecord:
    tokens_full = sum(r.input_tokens + r.output_tokens for r in ctx.costs.records(case.id))
    tokens_before = tokens_full
    new_sources = register_materials(ctx, case, new_paths, subdir="new")
    results = [ingest_source(ctx, case, s.id) for s in new_sources]
    claims_before = {c.id for c in case.claims}
    merge_ingest(ctx, case, results)
    new_source_ids = {s.id for s in new_sources}
    new_evidence = [e for e in case.evidence if e.source_id in new_source_ids]
    metric_claims = [c for c in case.claims if c.id not in claims_before]

    existing = [c for c in case.active_claims() if c.id in claims_before]
    dto, _ = ask_structured(
        ctx,
        task="revise",
        prompt="revision_claims",
        values={
            "frame": frame_digest(case),
            "new_evidence": evidence_digest(case, [e.id for e in new_evidence]),
            "new_metrics": "\n".join(f"[{c.id}] {c.text}" for c in metric_claims) or "—",
            "existing": claims_digest(case, [c.id for c in existing]),
        },
        schema=RevisionClaimsDTO,
        case_id=case.id,
        step="revision:claims",
    )
    reserved: list[str] = []
    key_to_id: dict[str, str] = {}
    new_claims = []
    for item in dto.claims:
        claim = claim_from_dto(ctx, case, item, reserved)
        key_to_id[item.key] = claim.id
        new_claims.append(claim)
    for metric in metric_claims:
        key_to_id.setdefault(metric.id, metric.id)
    classify_types(ctx, case, new_claims)
    case.claims.extend(new_claims)
    verify_claims(ctx, case, new_claims)

    invalidated = apply_relations(ctx, case, dto.relations, key_to_id, claims_before)
    active = case.active_decision()
    if active is not None:
        check_signals(ctx, case, active, [c.id for c in [*new_claims, *metric_claims]])
    news = [*new_claims, *metric_claims]
    return _propagate(
        ctx,
        case,
        invalidated=invalidated,
        news=news,
        news_text="\n".join(f"[{c.id}] {c.text}" for c in news) or "—",
        tokens_before=tokens_before,
        tokens_full=tokens_full,
        new_source_ids=sorted(new_source_ids),
        head=f"{len(new_sources)} новых источника → {len(news)} новых строк реестра → ",
        fired_prefix="Новые данные затронули основание: ",
    )


def revise_on_contest(
    ctx: AppContext, case: DecisionCase, claim_ids: list[str], reason: str
) -> RevisionRecord:
    """Руководитель оспорил основания без новых файлов: строка получает опровергатель и статус «под подозрением»,
    пересматриваются только зависимые узлы. Причина идёт в тот же слот промпта, что и новые строки реестра."""
    tokens_full = sum(r.input_tokens + r.output_tokens for r in ctx.costs.records(case.id))
    reason = reason.strip() or "без пояснения"
    contested = [cid for cid in dict.fromkeys(claim_ids) if (c := case.claim(cid)) and c.is_active]
    if not contested:
        raise ValueError(f"Нет активных строк реестра среди {claim_ids}")
    for cid in contested:
        claim = case.claim(cid)
        assert claim is not None
        claim.defeaters.append(
            Defeater(
                kind=DefeaterKind.UNDERCUTTING,
                description=f"Оспорено руководителем: {reason}",
                recorded_at=ctx.clock.now(),
            )
        )
        claim.status = ClaimStatus.SUSPECT
    ctx.emit("revision.contested", case.id, claims=contested)
    return _propagate(
        ctx,
        case,
        invalidated=contested,
        news=[],
        news_text=f"Руководитель оспорил {', '.join(contested)}: {reason}",
        tokens_before=tokens_full,
        tokens_full=tokens_full,
        new_source_ids=[],
        head=f"Оспорено руководителем: {', '.join(contested)} → ",
        fired_prefix="Руководитель оспорил основание: ",
        trigger="contest",
        contested=contested,
    )


def _propagate(
    ctx: AppContext,
    case: DecisionCase,
    *,
    invalidated: list[str],
    news: list[Claim],
    news_text: str,
    tokens_before: int,
    tokens_full: int,
    new_source_ids: list[str],
    head: str,
    fired_prefix: str,
    trigger: str = "new_evidence",
    contested: list[str] | None = None,
) -> RevisionRecord:
    """Ядро пересмотра: от оснований под вопросом — к подозрительным узлам, вердиктам модели, сигналам и записи."""
    graph = build_graph(case)
    depth = int(ctx.setting("revision.suspect_depth", 6))
    suspects = [
        n for n in graph.downstream(invalidated, max_depth=depth) if graph.nodes[n].kind in REVIEW_KINDS
    ]
    for node_id in suspects:
        if (
            graph.nodes[node_id].kind == "claim"
            and (c := case.claim(node_id))
            and c.status == ClaimStatus.ACTIVE
        ):
            c.status = ClaimStatus.SUSPECT

    results_out: list[NodeRevision] = []
    if suspects:
        nodes_text = "\n".join(
            f"{n} ({graph.nodes[n].kind}): {_node_text(case, n, graph.nodes[n].kind)}" for n in suspects
        )
        verdicts, _ = ask_structured(
            ctx,
            task="revise",
            prompt="revise_nodes",
            values={
                "frame": frame_digest(case),
                "invalidated": claims_digest(case, invalidated) if invalidated else "—",
                "new_claims": news_text,
                "nodes": nodes_text,
            },
            schema=RevisionVerdictsDTO,
            case_id=case.id,
            step="revision:nodes",
        )
        by_node = {v.node_id: v for v in verdicts.verdicts}
        for node_id in suspects:
            kind = graph.nodes[node_id].kind
            v = by_node.get(node_id)
            verdict = (
                NodeVerdict(v.verdict)
                if v and v.verdict in NodeVerdict._value2member_map_
                else NodeVerdict.UNCHANGED
            )
            before = _apply_verdict(ctx, case, node_id, kind, verdict, v.new_text if v else "")
            results_out.append(
                NodeRevision(
                    node_id=node_id,
                    node_kind=kind,
                    verdict=verdict,
                    before=before[:400],
                    after=(v.new_text if v else "")[:400],
                    reason=clip_words(
                        v.reason if v else "модель не вернула вердикт — оставлено без изменений", 300
                    ),
                )
            )

    decision = case.active_decision()
    reopened = None
    if decision is not None:
        changed = {r.node_id for r in results_out if r.verdict != NodeVerdict.UNCHANGED}
        for trig in decision.triggers:
            if set(trig.assumption_claim_ids) & set(invalidated) or trig.id in changed:
                trig.fired = True
                trig.fired_at = ctx.clock.now()
                trig.fired_reason = fired_prefix + ", ".join(
                    sorted(set(trig.assumption_claim_ids) & set(invalidated)) or [trig.id]
                )
        selected_hit = case.selected_option_id in changed or PRODUCT_NODE in changed
        if (
            any(t.fired for t in decision.triggers)
            or selected_hit
            or decision.status == DecisionStatus.REOPENED
        ):
            decision.status = DecisionStatus.REOPENED
            reopened = decision.id
            if case.research_plan is not None and case.research_plan.matrix.uncertainties:
                stress = run_stress_test(ctx, case, [c.id for c in news])
                if stress.base_winner != case.selected_option_id and (alt := case.option(stress.base_winner)):
                    alt.status = OptionStatus.REOPENED
                    alt.revisit_when = "Открыт заново при пересмотре: стал лучшим в базовом сценарии"
                    results_out.append(_refine_reopened(ctx, case, alt.id, invalidated, news))

    affected = set(suspects) | set(invalidated)
    untouched = [
        n for n, node in build_graph(case).nodes.items() if node.kind in UNTOUCHED_KINDS and n not in affected
    ]
    tokens_after = sum(r.input_tokens + r.output_tokens for r in ctx.costs.records(case.id))
    record = RevisionRecord(
        id=case.new_id("revision"),
        at=ctx.clock.now(),
        new_source_ids=new_source_ids,
        new_claim_ids=[c.id for c in news],
        invalidated_claim_ids=invalidated,
        suspect_ids=suspects,
        untouched_ids=untouched,
        results=results_out,
        reopened_decision_id=reopened,
        tokens_used=tokens_after - tokens_before,
        tokens_full_regeneration_estimate=tokens_full,
        summary=(
            head
            + f"{len(invalidated)} оснований под вопросом → {sum(r.verdict != NodeVerdict.UNCHANGED for r in results_out)} "
            f"выводов пересмотрено, {len(untouched)} узлов не затронуто"
            + (f"; решение {reopened} открыто заново" if reopened else "")
        ),
        trigger=trigger,
        contested_claim_ids=list(contested or []),
    )
    case.revisions.append(record)
    ctx.emit("revision.completed", case.id, revision=record.id, suspects=len(suspects), reopened=reopened)
    return record
