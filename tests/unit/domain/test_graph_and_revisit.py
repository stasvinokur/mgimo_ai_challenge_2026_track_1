from datetime import date

from factories import NOW, add_claim, add_option, empty_case, grounded_fact
from strategist.domain.entities import (
    ClaimStatus,
    ClaimType,
    Commitment,
    Decision,
    DecisionStatus,
    Defeater,
    DefeaterKind,
    KillCriterion,
    OptionKind,
    Trigger,
    TriggerKind,
)
from strategist.domain.services.dependency_graph import build_graph
from strategist.domain.services.revisit_rules import AgendaKind, stale_grounds, what_to_decide_now


def _case():
    case = empty_case()
    a = grounded_fact(case, "Удержание M3 снизилось до 33%")
    b = grounded_fact(case, "Цена конкурента 499 ₽")
    assumption = add_claim(
        case, "Мы сохраняем ценовое преимущество 20%", ClaimType.ASSUMPTION, depends_on=[b.id]
    )
    opt_focus = add_option(
        case, OptionKind.FOCUS, supporting_claim_ids=[a.id], assumption_claim_ids=[assumption.id]
    )
    opt_sq = add_option(case, OptionKind.STATUS_QUO, supporting_claim_ids=[a.id])
    return case, a, b, assumption, opt_focus, opt_sq


def test_downstream_marks_only_dependents():
    case, a, b, assumption, opt_focus, opt_sq = _case()
    g = build_graph(case)
    src_b = case.evidence_item(b.evidence_ids[0]).source_id
    affected = g.downstream([src_b])
    assert b.id in affected and assumption.id in affected and opt_focus.id in affected
    assert a.id not in affected and opt_sq.id not in affected


def test_label_or_of_and():
    case, a, b, assumption, opt_focus, opt_sq = _case()
    g = build_graph(case)
    src_b = case.evidence_item(b.evidence_ids[0]).source_id
    status = g.label(extra_out=[src_b])
    assert status[a.id] is True
    assert status[b.id] is False
    assert status[assumption.id] is False
    assert status[opt_focus.id] is False
    assert status[opt_sq.id] is True


def test_claim_with_two_evidence_survives_losing_one():
    case = empty_case()
    fact = grounded_fact(case)
    second = grounded_fact(case, "дубль")
    fact.evidence_ids.append(second.evidence_ids[0])
    g = build_graph(case)
    first_source = case.evidence_item(fact.evidence_ids[0]).source_id
    assert g.label(extra_out=[first_source])[fact.id] is True


def test_retracted_claim_is_out():
    case, a, *_ = _case()
    a.status = ClaimStatus.RETRACTED
    assert build_graph(case).label()[a.id] is False


def test_what_to_decide_now_collects_signals():
    case, a, b, assumption, opt_focus, _ = _case()
    decision = Decision(
        id="DEC-0001",
        selected_option_id=opt_focus.id,
        rationale="тест",
        created_at=NOW,
        revisit_date=date(2026, 11, 1),
        triggers=[
            Trigger(id="TRG-001", kind=TriggerKind.SIGNAL, description="Цена конкурента упала", fired=True),
            Trigger(
                id="TRG-002",
                kind=TriggerKind.DATE,
                description="Квартальный пересмотр",
                due=date(2026, 11, 3),
            ),
        ],
        kill_criteria=[KillCriterion(signal="M3 < 30%", deadline=date(2026, 11, 2))],
        commitments=[Commitment(id="CMT-001", description="Выгрузить когорты", due=date(2026, 10, 30))],
    )
    case.decisions.append(decision)
    items = what_to_decide_now(case, date(2026, 11, 4))
    kinds = {i.kind for i in items}
    assert {AgendaKind.TRIGGER, AgendaKind.KILL, AgendaKind.COMMITMENT, AgendaKind.REVISIT} <= kinds
    assert items[0].urgency == 3


def test_reopened_decision_is_on_agenda():
    case, *_, opt_focus, _ = _case()
    case.decisions.append(
        Decision(
            id="DEC-0001",
            selected_option_id=opt_focus.id,
            rationale="т",
            created_at=NOW,
            status=DecisionStatus.REOPENED,
        )
    )
    assert any(i.kind == AgendaKind.REOPENED for i in what_to_decide_now(case, date(2026, 11, 4)))


def test_stale_grounds_sorted_by_load_bearing_vulnerable_confidence():
    case, a, b, assumption, *_ = _case()
    a.valid_until = date(2026, 10, 1)
    a.load_bearing = True
    a.vulnerable = True
    a.confidence = 0.4
    b.defeaters.append(
        Defeater(kind=DefeaterKind.REBUTTING, description="Новый прайс 299 ₽", recorded_at=NOW)
    )
    b.confidence = 0.9
    stale = stale_grounds(case, date(2026, 11, 4))
    assert [s.claim_id for s in stale] == [a.id, b.id]
    assert "истёк" in stale[0].reasons[0]
