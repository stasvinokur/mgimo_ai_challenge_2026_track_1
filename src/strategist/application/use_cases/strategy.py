"""CompileProduct, CompileComms, LintPromises: согласованные стратегии и сверка «обещание = продукт».

Сверка — структурное ограничение, а не просьба к модели «проверь себя»: обещание без функции продукта
и подтверждённой строки реестра, обещание другому сегменту или обещание на одной синтетике блокируется.
Поверх структуры — смысловой вопрос decide(): «подтверждает ли функция именно это обещание?».
"""

from __future__ import annotations

from strategist.application.context import AppContext
from strategist.application.digests import claims_digest, frame_digest
from strategist.application.dto.llm_outputs import ActionStepDTO, CommsStrategyDTO, ProductStrategyDTO
from strategist.application.llm_calls import ask_structured
from strategist.application.ports import DecisionKind, DecisionQuestion
from strategist.application.utils import enum_or, known_ids
from strategist.domain.entities import (
    ActionStep,
    BarrierType,
    ChannelRole,
    ChannelRoleKind,
    CommsStrategy,
    DecisionCase,
    Feature,
    FeatureStatus,
    ProductStrategy,
    Promise,
    PromiseLocation,
    PromiseStatus,
    Segment,
    SegmentMessage,
)
from strategist.domain.services.promise_linter import PromiseFinding, apply_findings, lint_promises


def _step(s: ActionStepDTO) -> ActionStep:
    return ActionStep(when=s.when, action=s.action, owner=s.owner, metric=s.metric)


def _selected(case: DecisionCase):
    option = case.option(case.selected_option_id or "")
    if option is None:
        raise ValueError("Вариант не выбран — сначала шлюз выбора варианта")
    return option


def compile_product(ctx: AppContext, case: DecisionCase, feedback: str = "") -> ProductStrategy:
    option = _selected(case)
    dto, _ = ask_structured(
        ctx,
        task="product_strategy",
        prompt="product_strategy",
        values={
            "frame": frame_digest(case),
            "option": f"[{option.id}] {option.title}: {option.summary}\nОбоснование: {option.rationale}\n"
            f"Осознанные отказы: {'; '.join(option.conscious_refusals)}",
            "claims": claims_digest(case),
            "feedback": feedback or "—",
            "today": f"Сегодня {ctx.clock.today().isoformat()}.",
        },
        schema=ProductStrategyDTO,
        case_id=case.id,
        step="product",
    )
    valid = {c.id for c in case.claims}
    seg_ids = {s.key: f"SEG-{i:03d}" for i, s in enumerate(dto.segments, start=1)}
    feat_ids = {f.key: f"FEAT-{i:03d}" for i, f in enumerate(dto.features, start=1)}
    product = ProductStrategy(
        option_id=option.id,
        audience=dto.audience,
        segments=[Segment(id=seg_ids[s.key], name=s.name, description=s.description) for s in dto.segments],
        segment_ids=list(seg_ids.values()),
        job_to_be_done=dto.job_to_be_done,
        alternatives=dto.alternatives,
        direction=dto.direction,
        conscious_refusals=dto.conscious_refusals or option.conscious_refusals,
        value_logic=dto.value_logic,
        costs=dto.costs,
        revenue=dto.revenue,
        features=[
            Feature(
                id=feat_ids[f.key],
                name=f.name,
                description=f.description,
                status=enum_or(FeatureStatus, f.status, FeatureStatus.HYPOTHETICAL),
                claim_ids=known_ids(f.claim_ids, valid),
            )
            for f in dto.features
        ],
        metrics=dto.metrics,
        claim_ids=known_ids(dto.claim_ids, valid),
        action_plan=[_step(s) for s in dto.action_plan[:8]],
    )
    case.product = product
    ctx.emit("product.compiled", case.id, features=len(product.features), segments=len(seg_ids))
    return product


def segments_text(case: DecisionCase) -> str:
    product = case.product
    if product is None or not product.segments:
        return "—"
    return "\n".join(f"{s.id}: {s.name} — {s.description}" for s in product.segments)


def compile_comms(ctx: AppContext, case: DecisionCase, feedback: str = "") -> CommsStrategy:
    product = case.product
    if product is None:
        raise ValueError("Нет продуктовой стратегии")
    features = "\n".join(
        f"{f.id} ({f.status.value}): {f.name} — {f.description} [{', '.join(f.claim_ids) or 'нет опоры'}]"
        for f in product.features
    )
    dto, _ = ask_structured(
        ctx,
        task="comms_strategy",
        prompt="comms_strategy",
        values={
            "frame": frame_digest(case),
            "product": f"Аудитория: {product.audience}\nЗадача: {product.job_to_be_done}\n"
            f"Направление: {product.direction}\nОтказы: {'; '.join(product.conscious_refusals)}",
            "segments": segments_text(case),
            "features": features or "—",
            "claims": claims_digest(case),
            "feedback": feedback or "—",
            "today": f"Сегодня {ctx.clock.today().isoformat()}.",
        },
        schema=CommsStrategyDTO,
        case_id=case.id,
        step="comms",
    )
    valid_claims = {c.id for c in case.claims}
    feature_ids = {f.id for f in product.features}
    # Модель может ссылаться на функции по ключам из ответа о продукте (F1) или по ID (FEAT-001).
    key_to_feature = {f"F{i}": fid for i, fid in enumerate(sorted(feature_ids), start=1)}

    def resolve_feature(k: str) -> str | None:
        k = k.strip()
        return k if k in feature_ids else key_to_feature.get(k)

    segment_ids = set(product.segment_ids)
    prm_ids = {p.key: f"PRM-{i:03d}" for i, p in enumerate(dto.promises, start=1)}
    promises = [
        Promise(
            id=prm_ids[p.key],
            text=p.text,
            location=enum_or(PromiseLocation, p.location, PromiseLocation.KEY_MESSAGE),
            segment_id=p.segment_key if p.segment_key in segment_ids else (p.segment_key or None),
            feature_ids=[f for f in (resolve_feature(k) for k in p.feature_keys) if f],
            claim_ids=known_ids(p.claim_ids, valid_claims),
        )
        for p in dto.promises
    ]
    comms = CommsStrategy(
        audience=dto.audience,
        segment_id=dto.segment_key,
        situation=dto.situation,
        barrier=enum_or(BarrierType, dto.barrier, BarrierType.UNDERSTANDING),
        barrier_explanation=dto.barrier_explanation,
        key_message=dto.key_message,
        proof_claim_ids=known_ids(dto.proof_claim_ids, valid_claims),
        channels=[
            ChannelRole(
                channel=c.channel,
                role=enum_or(ChannelRoleKind, c.role, ChannelRoleKind.EXPLANATION),
                message=c.message,
                promise_ids=[prm_ids[k] for k in c.promise_keys if k in prm_ids],
            )
            for c in dto.channels
        ],
        press_release=dto.press_release,
        promises=promises,
        segment_messages=[
            SegmentMessage(
                segment_id=m.segment_key if m.segment_key in segment_ids else None,
                message=m.message,
                channel=m.channel,
                timing=m.timing,
            )
            for m in dto.segment_messages[:8]
        ],
        timeline=[_step(s) for s in dto.timeline[:8]],
    )
    case.comms = comms
    ctx.emit("comms.compiled", case.id, promises=len(promises), channels=len(comms.channels))
    return comms


def semantic_promise_check(
    ctx: AppContext, case: DecisionCase, findings: list[PromiseFinding]
) -> list[PromiseFinding]:
    """Структурно подкреплённые обещания дополнительно проверяются по смыслу (decide, Noul)."""
    if case.comms is None or case.product is None:
        return findings
    features = {f.id: f for f in case.product.features}
    by_id = {f.promise_id: f for f in findings}
    questions, targets = [], []
    for promise in case.comms.promises:
        finding = by_id.get(promise.id)
        if finding is None or finding.status != PromiseStatus.SUPPORTED:
            continue
        support = (
            "\n".join(
                f"Функция {fid}: {features[fid].name} — {features[fid].description}"
                for fid in promise.feature_ids
                if fid in features
            )
            + "\n"
            + "\n".join(f"[{cid}] {c.text}" for cid in promise.claim_ids if (c := case.claim(cid)))
        )
        questions.append(
            DecisionQuestion(
                id=f"{case.id}:{promise.id}:semantic",
                kind=DecisionKind.NOUL,
                instructions="Подтверждают ли эти функции продукта и данные именно это обещание (без преувеличения)?",
                state=f"Обещание: {promise.text}\nОпора:\n{support}",
                task="promise_supported",
                case_id=case.id,
                run_id=ctx.run_id,
                step="promise_lint",
            )
        )
        targets.append(promise.id)
    if not questions:
        return findings
    threshold = float(ctx.setting("decide.confidence_threshold", 0.6))
    for promise_id, result in zip(targets, ctx.decision.decide_many(questions), strict=True):
        if result.probability is not None and result.probability < 0.5 and result.confidence >= threshold:
            by_id[promise_id] = PromiseFinding(
                promise_id=promise_id,
                status=PromiseStatus.UNSUPPORTED,
                message=f"Функция не подтверждает обещание по смыслу (P(да)={result.probability:.2f})",
            )
    return list(by_id.values())


def lint_promises_step(ctx: AppContext, case: DecisionCase) -> int:
    findings = semantic_promise_check(ctx, case, lint_promises(case))
    blocked = apply_findings(case, findings)
    ctx.emit(
        "promises.linted",
        case.id,
        blocked=blocked,
        statuses={f.promise_id: f.status.value for f in findings},
    )
    return blocked


def promise_feedback(case: DecisionCase) -> str:
    if case.comms is None:
        return ""
    bad = [
        p for p in case.comms.promises if p.status not in (PromiseStatus.SUPPORTED, PromiseStatus.UNCHECKED)
    ]
    if not bad:
        return ""
    return "Сверка «обещание = продукт» заблокировала обещания — перепиши их или убери:\n" + "\n".join(
        f"- {p.id} «{p.text}»: {p.status.value} — {p.issue}" for p in bad
    )


def drop_blocked_promises(case: DecisionCase) -> list[str]:
    """После исчерпания попыток заблокированные обещания не выходят в коммуникацию (остаются в журнале)."""
    if case.comms is None:
        return []
    removed = [p.id for p in case.comms.promises if p.status != PromiseStatus.SUPPORTED]
    note = "; ".join(f"{p.id}: {p.issue}" for p in case.comms.promises if p.id in removed)
    case.comms.promises = [p for p in case.comms.promises if p.id not in removed]
    for channel in case.comms.channels:
        channel.promise_ids = [pid for pid in channel.promise_ids if pid not in removed]
    if removed:
        case.comms.revision_note = f"Сняты неподкреплённые обещания: {note}"
    return removed
