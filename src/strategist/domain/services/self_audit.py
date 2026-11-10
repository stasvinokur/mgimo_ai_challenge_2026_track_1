"""Панель самоаудита: метрики DeepTRACE, посчитанные агентом на собственном реестре."""

from __future__ import annotations

from collections import Counter

from pydantic import Field

from strategist.domain.common import Entity, Severity
from strategist.domain.entities.case import DecisionCase
from strategist.domain.entities.claims import ClaimType, SupportLabel
from strategist.domain.entities.sources import Reliability
from strategist.domain.services.claim_linter import lint_case_claims

ASSERTIVE_TYPES = (ClaimType.FACT, ClaimType.DERIVED_METRIC)


class SelfAudit(Entity):
    claims_total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    assertive_total: int = 0
    unsupported_share: float = 0.0
    unverified_share: float = 0.0
    verified_count: int = 0
    citation_accuracy: float | None = None
    sources_total: int = 0
    unused_source_ids: list[str] = Field(default_factory=list)
    unused_sources_share: float = 0.0
    options_without_objections: list[str] = Field(default_factory=list)
    one_sided: bool = False
    overconfident_claim_ids: list[str] = Field(default_factory=list)
    lint_errors: int = 0
    lint_warnings: int = 0
    synthetic_claims: int = 0
    unknowns_open: int = 0
    uncited_sentences: int = 0
    number_mismatches: int = 0
    # Сверка чисел v2 (numbers_v2): сколько чисел проверено и что с ними не так
    numbers_checked: int = 0
    numbers_unsupported: int = 0
    numbers_unit_mismatch: int = 0
    numbers_miscited: int = 0  # число есть в реестре, но в другой строке или без ссылки
    injections_detected: int = 0

    def headline(self) -> str:
        acc = f"{self.citation_accuracy:.0%}" if self.citation_accuracy is not None else "—"
        return (
            f"утверждений {self.claims_total} · без опоры {self.unsupported_share:.0%} · "
            f"точность цитат {acc} · неиспользованных источников {self.unused_sources_share:.0%} · "
            f"открытых неизвестных {self.unknowns_open}"
        )


def compute_self_audit(
    case: DecisionCase, uncited_sentences: int = 0, number_mismatches: int = 0
) -> SelfAudit:
    claims = case.active_claims()
    by_type = Counter(c.type.value for c in claims)
    assertive = [c for c in claims if c.type in ASSERTIVE_TYPES]
    unsupported = [c for c in assertive if c.support in (SupportLabel.UNSUPPORTED, SupportLabel.CONTRADICTED)]
    unverified = [c for c in assertive if c.support == SupportLabel.UNVERIFIED]
    verified = [c for c in assertive if c.support != SupportLabel.UNVERIFIED]
    accurate = [c for c in verified if c.support in (SupportLabel.SUPPORTED, SupportLabel.PARTIAL)]

    used_sources: set[str] = set()
    for c in claims:
        for ev_id in c.evidence_ids:
            ev = case.evidence_item(ev_id)
            if ev:
                used_sources.add(ev.source_id)
        if c.computation and c.computation.source_id:
            used_sources.add(c.computation.source_id)
    valid_sources = [s for s in case.sources if s.is_valid]
    unused = [s.id for s in valid_sources if s.id not in used_sources]

    without_objections = [o.id for o in case.options if not case.objections_for(o.id)]
    selected = case.selected_option_id
    one_sided = bool(without_objections) or (selected is not None and selected in without_objections)

    # Излишняя уверенность: крайние прогнозы без надёжной опоры и «почти наверняка» при частичной опоре.
    overconfident = [
        c.id
        for c in claims
        if (
            c.type == ClaimType.FORECAST
            and c.probability is not None
            and (c.probability >= 0.9 or c.probability <= 0.1)
            and c.reliability != Reliability.HIGH
        )
        or (c.type == ClaimType.FACT and c.support == SupportLabel.PARTIAL and (c.confidence or 0) >= 0.9)
    ]
    issues = lint_case_claims(case)
    return SelfAudit(
        claims_total=len(claims),
        by_type=dict(by_type),
        assertive_total=len(assertive),
        unsupported_share=round(len(unsupported) / len(assertive), 4) if assertive else 0.0,
        unverified_share=round(len(unverified) / len(assertive), 4) if assertive else 0.0,
        verified_count=len(verified),
        citation_accuracy=round(len(accurate) / len(verified), 4) if verified else None,
        sources_total=len(valid_sources),
        unused_source_ids=unused,
        unused_sources_share=round(len(unused) / len(valid_sources), 4) if valid_sources else 0.0,
        options_without_objections=without_objections,
        one_sided=one_sided,
        overconfident_claim_ids=overconfident,
        lint_errors=sum(1 for i in issues if i.severity == Severity.ERROR),
        lint_warnings=sum(1 for i in issues if i.severity == Severity.WARNING),
        synthetic_claims=by_type.get(ClaimType.SYNTHETIC.value, 0),
        unknowns_open=sum(1 for u in case.unknowns if u.status.value in ("open", "researching")),
        uncited_sentences=uncited_sentences,
        number_mismatches=number_mismatches,
        injections_detected=sum(len(s.instructions_detected) for s in case.sources),
    )
