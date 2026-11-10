"""Проверка оснований: несущие утверждения и пробное исключение опоры — по графу, без моделей."""

from __future__ import annotations

from factories import NOW, add_claim, add_evidence, add_option, add_source, empty_case
from strategist.domain.entities import (
    ClaimType,
    Decision,
    ExecutiveSummary,
    Objection,
    ObjectionKind,
    OptionKind,
    ReasonBasis,
    SupportLabel,
)
from strategist.domain.services.challenge import audit_rows, preview_removal, rank_load_bearing


def _case():
    case = empty_case()
    s1, s2, s3 = (
        add_source(case, title="Когорты"),
        add_source(case, title="Опрос"),
        add_source(case, title="Рынок"),
    )
    e1, e2 = add_evidence(case, s1, "Удержание M3 33%"), add_evidence(case, s2, "Удержание M3 около трети")
    e3, e4 = add_evidence(case, s3, "Рынок растёт на 9%"), add_evidence(case, s3, "Рынок вырос на 9% за год")
    twice = add_claim(
        case, "Удержание M3 33%", ClaimType.FACT, [e1, e2], support=SupportLabel.SUPPORTED, confidence=0.9
    )
    same = add_claim(
        case, "Рынок растёт на 9%", ClaimType.FACT, [e3, e4], support=SupportLabel.SUPPORTED, confidence=0.8
    )
    guess = add_claim(
        case, "Сериалы вернут подписчиков", ClaimType.HYPOTHESIS, falsification_test="тест", confidence=0.3
    )
    opt = add_option(case, OptionKind.FOCUS, "Снять барьер оплаты", supporting_claim_ids=[twice.id, same.id])
    alt = add_option(case, OptionKind.STATUS_QUO, supporting_claim_ids=[guess.id])
    case.selected_option_id = opt.id
    case.decisions.append(
        Decision(
            id="DEC-0001",
            selected_option_id=opt.id,
            rationale="—",
            load_bearing_claim_ids=[twice.id, same.id],
            vulnerable_claim_ids=[same.id],
            created_at=NOW,
        )
    )
    case.objections.append(
        Objection(
            id="OBJ-001",
            option_id=opt.id,
            kind=ObjectionKind.STEELMAN,
            text="Рынок может остыть",
            claim_ids=[same.id],
            strength=3,
        )
    )
    return case, {
        "s1": s1.id,
        "s2": s2.id,
        "s3": s3.id,
        "twice": twice.id,
        "same": same.id,
        "guess": guess.id,
        "opt": opt.id,
        "alt": alt.id,
    }


def test_preview_is_pure_and_order_independent():
    case, ids = _case()
    before = case.model_dump_json()
    a = preview_removal(case, [ids["same"], ids["guess"]])
    b = preview_removal(case, [ids["guess"], ids["same"], ids["same"]])
    assert a == b and case.model_dump_json() == before


def test_irrelevant_source_changes_nothing():
    case, ids = _case()
    unused = add_source(case, title="Пресс-релиз конкурента")
    preview = preview_removal(case, [unused.id])
    assert preview.lost == [] and "ничего не теряет опору" in preview.summary


def test_two_quotes_from_different_sources_keep_the_claim_alive():
    case, ids = _case()
    preview = preview_removal(case, [ids["s1"]])
    assert ids["twice"] not in {x.node_id for x in preview.lost}
    assert all(x.kind != "решение" for x in preview.lost)


def test_two_quotes_from_one_source_fall_together_and_the_decision_loses_a_required_support():
    case, ids = _case()
    preview = preview_removal(case, [ids["s3"]])
    lost = {x.node_id: x for x in preview.lost}
    assert lost[ids["same"]].effect == "теряет опору"
    assert (
        lost["DEC-0001"].effect.startswith("лишается обязательной опоры")
        and "неверн" not in lost["DEC-0001"].effect
    )
    assert "его стоит пересмотреть" in preview.summary


def test_removing_the_only_support_of_a_load_bearing_claim_puts_the_decision_on_the_list():
    case, ids = _case()
    preview = preview_removal(case, [ids["same"]])
    lost = {x.node_id: x for x in preview.lost}
    assert "DEC-0001" in lost and lost[ids["opt"]].direct
    assert lost["DEC-0001"].effect.endswith(f"({ids['opt']}, {ids['same']})")  # что именно выпало
    assert ids["alt"] not in lost  # вариант «оставить» на другой опоре не задет


def test_claim_with_another_quote_is_reported_as_holding():
    case, ids = _case()
    e1 = case.claim(ids["twice"]).evidence_ids[0]
    preview = preview_removal(case, [e1])
    holding = {x.node_id: x for x in preview.holding}
    assert holding[ids["twice"]].effect.startswith("держится на EV-")


def test_unknown_ids_are_reported_not_ignored_silently():
    case, _ = _case()
    preview = preview_removal(case, ["CL-999"])
    assert preview.unknown == ["CL-999"] and preview.summary == "Таких строк в кейсе нет."


def test_ranking_puts_executive_supports_then_vulnerable_then_least_confident():
    case, ids = _case()
    case.executive = ExecutiveSummary(
        answer="—",
        reasons=[f"Удержание M3 — 33% [{ids['twice']}]."],
        basis=[ReasonBasis(claim_ids=[ids["twice"]])],
    )
    order = rank_load_bearing(case)
    assert order[0] == (ids["twice"], "опора довода выжимки")
    assert order[1] == (ids["same"], "уязвимое основание решения")
    assert order[2] == (ids["guess"], "низкая уверенность")


def test_audit_rows_show_quotes_for_and_against():
    case, ids = _case()
    rows = {r.claim_id: r for r in audit_rows(case)}
    same = rows[ids["same"]]
    assert "цитат 2, источников 1" in same.pro
    assert any(c.startswith("возражение (steelman, сила 3/5): Рынок может остыть") for c in same.con)
    assert same.dependents >= 3 and same.sources[0].quote == "Рынок растёт на 9%"
    assert "цитат 2, источников 2" in rows[ids["twice"]].pro
