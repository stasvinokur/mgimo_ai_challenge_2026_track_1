"""Оценка надёжности по мотивам GRADE (собственная адаптация для бизнес-решений).

Отправная точка зависит от типа источника, затем понижается за синтетику, противоречия и происхождение
«вывод агента без источника». Готовой «GRADE для бизнеса» в литературе нет — это допущение проекта.
"""

from __future__ import annotations

from strategist.domain.entities.case import DecisionCase
from strategist.domain.entities.claims import Claim, ClaimType
from strategist.domain.entities.sources import Origin, Reliability, Source, SourceKind

_ORDER = [Reliability.VERY_LOW, Reliability.LOW, Reliability.MODERATE, Reliability.HIGH]

OFFICIAL_HINTS = ("rosstat", "cbr.ru", "gov.ru", "отчётност", "годовой отчёт", "investor", "раскрыти")


def _down(level: Reliability, steps: int = 1) -> Reliability:
    return _ORDER[max(0, _ORDER.index(level) - steps)]


def source_prior(source: Source) -> Reliability:
    if source.is_model_respondents:
        return Reliability.VERY_LOW
    if source.synthetic:  # смоделированные данные демо: обозначены, но не «вывод модели»
        return Reliability.LOW
    address = f"{source.url or ''} {source.publisher or ''} {source.title}".lower()
    if source.kind == SourceKind.TABLE:
        return Reliability.MODERATE
    if any(hint in address for hint in OFFICIAL_HINTS):
        return Reliability.MODERATE
    if source.kind in (SourceKind.INTERVIEW, SourceKind.WEB, SourceKind.NOTE):
        return Reliability.LOW
    if source.kind == SourceKind.USER_INPUT:
        return Reliability.LOW
    return Reliability.MODERATE


def assess_claim(claim: Claim, case: DecisionCase) -> Reliability:
    if claim.origin == Origin.AGENT_INFERENCE and not claim.evidence_ids:
        return Reliability.VERY_LOW
    if claim.type == ClaimType.SYNTHETIC:
        return Reliability.VERY_LOW
    if claim.type == ClaimType.DERIVED_METRIC and claim.computation and claim.computation.consistent:
        base = Reliability.HIGH
        src = case.source(claim.computation.source_id) if claim.computation.source_id else None
        if src is not None and src.is_model_respondents:
            base = Reliability.VERY_LOW
        elif src is not None and src.synthetic:
            base = Reliability.LOW
        return base
    levels = []
    for ev_id in claim.evidence_ids:
        ev = case.evidence_item(ev_id)
        src = case.source(ev.source_id) if ev else None
        if src is not None:
            levels.append(source_prior(src))
    if not levels:
        return Reliability.VERY_LOW if claim.type == ClaimType.FACT else Reliability.LOW
    best = max(levels, key=_ORDER.index)
    if claim.contradicting_evidence_ids:
        best = _down(best)
    if claim.type in (ClaimType.HYPOTHESIS, ClaimType.ASSUMPTION, ClaimType.FORECAST):
        best = _down(best)
    return best
