"""Линтер утверждений: «факты отдельно от гипотез и прогнозов» — свойство схемы, а не промпта.

Строка реестра с ошибкой не выводится в отчёт. Правила:
- факт без источника и цитаты — ошибка;
- расчётная метрика без расчёта в коде или с расхождением двух движков — ошибка;
- прогноз без вероятности, горизонта и критерия разрешения — ошибка;
- гипотеза без проверки на опровержение — ошибка;
- «неизвестно» без причины — ошибка;
- синтетика в роли факта или доказательства спроса/доли — ошибка;
- доля строк агрегированной таблицы (контракт расчёта) — ошибка: в реестре остаётся, в отчёт не идёт.
"""

from __future__ import annotations

import re

from strategist.domain.common import LintIssue, Severity
from strategist.domain.entities.case import DecisionCase
from strategist.domain.entities.claims import Claim, ClaimType

DEMAND_PATTERN = re.compile(
    r"(спрос|дол[яиюе]\b|доля|распростран|процент|%|готов[аы]?\s+(?:купить|платить|перейти)|"
    r"купил[иа]?\s+бы|захотел[иа]?\s+бы|хотят|покупательск|willingness|demand|market share|"
    r"adoption|prevalence|conversion|конверси)",
    re.IGNORECASE,
)


def mentions_demand(text: str) -> bool:
    return bool(DEMAND_PATTERN.search(text))


def _evidence_sources_synthetic(claim: Claim, case: DecisionCase) -> list[bool]:
    flags: list[bool] = []
    for ev_id in claim.evidence_ids:
        ev = case.evidence_item(ev_id)
        src = case.source(ev.source_id) if ev else None
        if src is not None:
            flags.append(src.is_model_respondents)
    return flags


def lint_claim(claim: Claim, case: DecisionCase) -> list[LintIssue]:
    issues: list[LintIssue] = []

    def err(code: str, message: str, severity: Severity = Severity.ERROR) -> None:
        issues.append(LintIssue(code=code, severity=severity, entity_id=claim.id, message=message))

    synthetic_flags = _evidence_sources_synthetic(claim, case)
    only_synthetic = bool(synthetic_flags) and all(synthetic_flags)

    if claim.type == ClaimType.FACT:
        if not claim.evidence_ids:
            err("fact_without_source", "Факт без источника и цитаты")
        for ev_id in claim.evidence_ids:
            ev = case.evidence_item(ev_id)
            if ev is None:
                err("missing_evidence", f"Ссылка на несуществующую цитату {ev_id}")
                continue
            if not ev.quote.strip():
                err("empty_quote", f"Пустая цитата {ev_id}")
            src = case.source(ev.source_id)
            if src is None:
                err("missing_source", f"Цитата {ev_id} ссылается на неизвестный источник {ev.source_id}")
            elif not src.is_valid:
                err("invalid_source", f"Источник {src.id} отозван или заменён", Severity.WARNING)
        if only_synthetic:
            err("synthetic_as_fact", "Факт держится только на синтетических данных — это не факт")

    elif claim.type == ClaimType.DERIVED_METRIC:
        comp = claim.computation
        if comp is None:
            err("metric_without_computation", "Расчётная метрика без расчёта в коде")
        elif comp.check_engine and comp.check_value is None:
            err("metric_not_double_checked", "Метрика не пересчитана вторым движком")
        elif comp.check_engine and not comp.consistent:
            err(
                "metric_mismatch",
                f"Расхождение движков: {comp.primary_engine}={comp.value}, {comp.check_engine}={comp.check_value}",
            )
        if only_synthetic and mentions_demand(claim.text):
            err("synthetic_demand_metric", "Метрика спроса/доли посчитана только на синтетике")
        if comp is not None and comp.blocked:
            err(
                "row_share_aggregated",
                "Доля строк агрегированной таблицы: строка — группа, а не человек (контракт расчёта)",
            )

    elif claim.type == ClaimType.FORECAST:
        if claim.probability is None:
            err("forecast_without_probability", "Прогноз без вероятности")
        if claim.horizon is None:
            err("forecast_without_horizon", "Прогноз без горизонта (даты разрешения)")
        if not (claim.resolution_criterion or "").strip():
            err("forecast_without_resolution", "Прогноз без критерия разрешения")

    elif claim.type == ClaimType.HYPOTHESIS:
        if not (claim.falsification_test or "").strip():
            err("hypothesis_without_falsification", "Гипотеза без проверки на опровержение")

    elif claim.type == ClaimType.ASSUMPTION:
        if not (claim.what_would_change or "").strip():
            err(
                "assumption_without_signal",
                "Допущение без сигнала, который его опроверг бы",
                Severity.WARNING,
            )

    elif claim.type == ClaimType.UNKNOWN:
        if not (claim.verification_note or claim.what_would_change or "").strip():
            err("unknown_without_reason", "«Неизвестно» без причины и способа узнать")

    elif claim.type == ClaimType.SYNTHETIC:
        if claim.support.value == "supported" and mentions_demand(claim.text):
            err(
                "synthetic_marked_supported",
                "Синтетическое утверждение о спросе помечено как подтверждённое",
            )

    return issues


def _support_refs(case: DecisionCase) -> list[tuple[str, str]]:
    """Где утверждения используются как опора: (claim_id, место использования)."""
    refs: list[tuple[str, str]] = []
    for option in case.options:
        refs += [(cid, f"вариант {option.id}") for cid in option.supporting_claim_ids]
    if case.product:
        refs += [(cid, "продуктовая стратегия") for cid in case.product.claim_ids]
        for feature in case.product.features:
            refs += [(cid, f"функция {feature.id}") for cid in feature.claim_ids]
    if case.comms:
        refs += [(cid, "подтверждение ключевого сообщения") for cid in case.comms.proof_claim_ids]
        for promise in case.comms.promises:
            refs += [(cid, f"обещание {promise.id}") for cid in promise.claim_ids]
    return refs


def lint_case_claims(case: DecisionCase) -> list[LintIssue]:
    """Все правила по реестру + запрет синтетики в роли доказательства спроса/доли."""
    issues: list[LintIssue] = []
    for claim in case.active_claims():
        issues.extend(lint_claim(claim, case))
    for claim_id, place in _support_refs(case):
        claim = case.claim(claim_id)
        if claim is None:
            issues.append(
                LintIssue(code="dangling_claim_ref", entity_id=claim_id, message=f"{place}: нет такой строки")
            )
            continue
        synthetic = claim.type == ClaimType.SYNTHETIC or (
            bool(flags := _evidence_sources_synthetic(claim, case)) and all(flags)
        )
        if synthetic and mentions_demand(claim.text):
            issues.append(
                LintIssue(
                    code="synthetic_as_demand_proof",
                    entity_id=claim_id,
                    message=f"{place}: синтетика использована как доказательство спроса или доли",
                )
            )
    return issues


def reportable(claim: Claim, case: DecisionCase) -> bool:
    """В отчёт попадают только строки без ошибок линтера."""
    return claim.is_active and not any(i.severity == Severity.ERROR for i in lint_claim(claim, case))
