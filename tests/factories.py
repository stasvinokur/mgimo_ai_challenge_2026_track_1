"""Фабрики тестовых кейсов."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from strategist.domain.entities import (
    Claim,
    ClaimType,
    DecisionCase,
    Evidence,
    Locator,
    Option,
    OptionKind,
    Source,
    SourceKind,
    SupportLabel,
)

NOW = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)


def empty_case(case_id: str = "case-1") -> DecisionCase:
    return DecisionCase(
        id=case_id, title="Тест", brief="Почему падает удержание?", created_at=NOW, updated_at=NOW
    )


def add_source(
    case: DecisionCase, *, synthetic: bool = False, kind: SourceKind = SourceKind.DOCUMENT, **kw: Any
) -> Source:
    # По умолчанию синтетика в тестах — модельные респонденты (строгие правила); смоделированные данные — явно.
    synthetic_kind = kw.pop("synthetic_kind", "model_respondents" if synthetic else None)
    src = Source(
        id=case.new_id("source"),
        title=kw.pop("title", "Источник"),
        kind=kind,
        synthetic=synthetic,
        synthetic_kind=synthetic_kind,
        added_at=NOW,
        **kw,
    )
    case.sources.append(src)
    return src


def add_evidence(
    case: DecisionCase, source: Source, quote: str = "Удержание M3 снизилось с 41% до 33%."
) -> Evidence:
    ev = Evidence(
        id=case.new_id("evidence"),
        source_id=source.id,
        quote=quote,
        locator=Locator(line_start=3),
        extracted_at=NOW,
    )
    case.evidence.append(ev)
    return ev


def add_claim(
    case: DecisionCase,
    text: str = "Удержание M3 снизилось до 33%",
    type: ClaimType = ClaimType.FACT,
    evidence: list[Evidence] | None = None,
    **kw: Any,
) -> Claim:
    claim = Claim(
        id=case.new_id("claim"),
        text=text,
        type=type,
        evidence_ids=[e.id for e in evidence or []],
        created_at=NOW,
        **kw,
    )
    case.claims.append(claim)
    return claim


def grounded_fact(
    case: DecisionCase, text: str = "Удержание M3 снизилось до 33%", synthetic: bool = False
) -> Claim:
    src = add_source(case, synthetic=synthetic)
    ev = add_evidence(case, src)
    return add_claim(case, text, ClaimType.FACT, [ev], support=SupportLabel.SUPPORTED, confidence=0.8)


def add_option(case: DecisionCase, kind: OptionKind, title: str | None = None, **kw: Any) -> Option:
    opt = Option(
        id=case.new_id("option"),
        kind=kind,
        title=title or kind.label_ru,
        summary=kw.pop("summary", "—"),
        **kw,
    )
    case.options.append(opt)
    return opt
