"""Реестр утверждений: извлечение из цитат, тип через decide(), дешёвая проверка цитат с эскалацией.

До 57% формально «правильных» ссылок подставлены задним числом, поэтому утверждение пишется из цитаты.
Проверка — двухуровневая: дешёвый проверяющий на каждой паре «цитата → утверждение», reasoning-модель —
только на пограничных и несущих утверждениях. Числа считает код (см. collect.py).
"""

from __future__ import annotations

from strategist.application.context import AppContext
from strategist.application.digests import evidence_digest, frame_digest
from strategist.application.dto.llm_outputs import ClaimDTO, ClaimsDTO, StrongVerifyDTO
from strategist.application.llm_calls import ask_structured
from strategist.application.ports import DecisionKind, DecisionQuestion, LedgerEntry, NLILabel
from strategist.application.use_cases.collect import research_questions_text
from strategist.application.utils import enum_or, known_ids, parse_date
from strategist.domain.entities import (
    Claim,
    ClaimType,
    DecisionCase,
    Evidence,
    Origin,
    SupportLabel,
)
from strategist.domain.services.reliability import assess_claim

CLAIM_TYPE_OPTIONS = {
    "fact": "Факт: наблюдаемое сведение, прямо подтверждённое цитатой из источника",
    "hypothesis": "Гипотеза: объяснение причины или механизма, которое ещё нужно проверить",
    "forecast": "Прогноз: утверждение о будущем значении или событии",
    "assumption": "Допущение: принимаем без проверки, чтобы двигаться дальше",
    "unknown": "Неизвестно: явный пробел — данных нет",
}

EVIDENCE_BATCH = 60


def _synthetic_only(case: DecisionCase, evidence_ids: list[str]) -> bool:
    flags = []
    for ev_id in evidence_ids:
        ev = case.evidence_item(ev_id)
        src = case.source(ev.source_id) if ev else None
        if src is not None:
            flags.append(src.is_model_respondents)
    return bool(flags) and all(flags)


def claim_from_dto(ctx: AppContext, case: DecisionCase, dto: ClaimDTO, reserved: list[str]) -> Claim:
    evidence_ids = known_ids(dto.evidence_ids, {e.id for e in case.evidence})
    contra = known_ids(dto.contradicting_evidence_ids, {e.id for e in case.evidence})
    claim_type = enum_or(ClaimType, dto.type, ClaimType.HYPOTHESIS)
    if claim_type == ClaimType.FACT and not evidence_ids:
        # Нет цитаты — не факт: модель могла сама посчитать число по таблице или додумать
        claim_type = ClaimType.HYPOTHESIS
    if claim_type == ClaimType.FACT and _synthetic_only(case, evidence_ids):
        claim_type = ClaimType.SYNTHETIC
    claim = Claim(
        id=case.new_id("claim", reserved=reserved),
        text=dto.text.strip(),
        type=claim_type,
        evidence_ids=evidence_ids,
        contradicting_evidence_ids=contra,
        probability=dto.probability if claim_type == ClaimType.FORECAST else None,
        horizon=parse_date(dto.horizon) if claim_type == ClaimType.FORECAST else None,
        resolution_criterion=dto.resolution_criterion,
        falsification_test=dto.falsification_test,
        what_would_change=dto.what_would_change,
        verification_note=dto.reason_unknown or "",
        origin=Origin.SOURCE_DOCUMENT if evidence_ids else Origin.AGENT_INFERENCE,
        topic=dto.topic or None,
        created_at=ctx.clock.now(),
    )
    reserved.append(claim.id)
    return claim


def classify_types(ctx: AppContext, case: DecisionCase, claims: list[Claim]) -> int:
    """Тип строки определяет decide() (Choice). Возвращает число исправленных типов."""
    threshold = float(ctx.setting("decide.confidence_threshold", 0.6))
    questions = [
        DecisionQuestion(
            id=f"{case.id}:{c.id}:type",
            kind=DecisionKind.CHOICE,
            instructions="К какому типу относится утверждение из реестра продуктовой стратегии?",
            state=f"Утверждение: {c.text}\nОпора: {len(c.evidence_ids)} цитат(ы)",
            options=CLAIM_TYPE_OPTIONS,
            task="claim_type",
            case_id=case.id,
            run_id=ctx.run_id,
            step="claims",
        )
        for c in claims
        if c.type not in (ClaimType.DERIVED_METRIC, ClaimType.SYNTHETIC)
    ]
    if not questions:
        return 0
    results = ctx.decision.decide_many(questions)
    by_claim = {q.id.split(":")[1]: r for q, r in zip(questions, results, strict=True)}
    changed = 0
    for c in claims:
        r = by_claim.get(c.id)
        if r is None or r.choice not in CLAIM_TYPE_OPTIONS or r.confidence < threshold:
            continue
        new_type = ClaimType(r.choice)
        if new_type == ClaimType.FACT and not c.evidence_ids:
            continue  # факт без цитаты — не факт; оставляем тип модели-извлекателя
        if new_type != c.type:
            c.type = new_type
            changed += 1
    return changed


def extract_claims(
    ctx: AppContext, case: DecisionCase, evidence: list[Evidence] | None = None
) -> list[Claim]:
    pool = evidence if evidence is not None else case.evidence
    new_claims: list[Claim] = []
    reserved: list[str] = []
    for start in range(0, len(pool), EVIDENCE_BATCH):
        batch = pool[start : start + EVIDENCE_BATCH]
        dto, _ = ask_structured(
            ctx,
            task="extract_claims",
            prompt="extract_claims",
            values={
                "frame": frame_digest(case),
                "questions": research_questions_text(case),
                "evidence": evidence_digest(case, [e.id for e in batch]),
            },
            schema=ClaimsDTO,
            case_id=case.id,
            step="claims",
        )
        for item in dto.claims:
            new_claims.append(claim_from_dto(ctx, case, item, reserved))
    classify_types(ctx, case, new_claims)
    case.claims.extend(new_claims)
    ctx.ledger.append(
        [
            LedgerEntry(
                case_id=case.id,
                claim_id=c.id,
                action="created",
                at=ctx.clock.now(),
                actor="extract_claims",
                snapshot={"text": c.text, "type": c.type.value, "evidence": c.evidence_ids},
            )
            for c in new_claims
        ]
    )
    ctx.emit("claims.extracted", case.id, count=len(new_claims))
    return new_claims


def _strong_verify(ctx: AppContext, case: DecisionCase, claim: Claim) -> StrongVerifyDTO:
    dto, _ = ask_structured(
        ctx,
        task="verify_claim",
        prompt="verify_claim",
        values={"claim": claim.text, "evidence": evidence_digest(case, claim.evidence_ids)},
        schema=StrongVerifyDTO,
        case_id=case.id,
        step="claims:verify",
        escalation=1,
    )
    return dto


def verify_claims(ctx: AppContext, case: DecisionCase, claims: list[Claim] | None = None) -> dict[str, int]:
    """Проверка опоры. Дешёвый проверяющий → эскалация пограничных и несущих на сильную модель."""
    band = ctx.setting("claims.verify_escalation_band", [0.35, 0.75])
    low, high = float(band[0]), float(band[1])
    targets = claims if claims is not None else case.active_claims()
    counters = {"checked": 0, "escalated": 0, "supported": 0, "unsupported": 0}
    entries = []
    for claim in targets:
        if claim.type in (ClaimType.DERIVED_METRIC,):
            claim.reliability = assess_claim(claim, case)
            continue
        if not claim.evidence_ids:
            claim.support = (
                SupportLabel.UNSUPPORTED if claim.type == ClaimType.FACT else SupportLabel.UNVERIFIED
            )
            claim.reliability = assess_claim(claim, case)
            continue
        best = None
        for ev_id in claim.evidence_ids:
            ev = case.evidence_item(ev_id)
            if ev is None:
                continue
            result = ctx.nli.check(ev.quote, claim.text, case_id=case.id)
            if (
                best is None
                or (result.label == NLILabel.ENTAILMENT and best.label != NLILabel.ENTAILMENT)
                or (result.label == best.label and result.score > best.score)
            ):
                best = result
        counters["checked"] += 1
        if best is None:
            claim.support = SupportLabel.UNSUPPORTED
            continue
        label = {
            NLILabel.ENTAILMENT: SupportLabel.SUPPORTED,
            NLILabel.CONTRADICTION: SupportLabel.CONTRADICTED,
            NLILabel.NEUTRAL: SupportLabel.PARTIAL if best.score >= 0.5 else SupportLabel.UNSUPPORTED,
        }[best.label]
        confidence = best.score
        verified_by = best.model or "nli"
        borderline = low <= best.score <= high or best.label == NLILabel.NEUTRAL
        if borderline or claim.load_bearing:
            strong = _strong_verify(ctx, case, claim)
            label = enum_or(SupportLabel, strong.label, label)
            confidence = strong.confidence
            verified_by = f"{verified_by} → эскалация"
            counters["escalated"] += 1
        claim.support = label
        claim.confidence = round(confidence, 3)
        claim.verified_by = verified_by
        claim.reliability = assess_claim(claim, case)
        counters[
            "supported" if label in (SupportLabel.SUPPORTED, SupportLabel.PARTIAL) else "unsupported"
        ] += 1
        entries.append(
            LedgerEntry(
                case_id=case.id,
                claim_id=claim.id,
                action="verified",
                at=ctx.clock.now(),
                actor=verified_by,
                snapshot={"support": label.value, "confidence": claim.confidence},
            )
        )
    if entries:
        ctx.ledger.append(entries)
    ctx.emit("claims.verified", case.id, **counters)
    return counters
