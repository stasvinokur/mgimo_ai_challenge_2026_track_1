"""Use cases постановки, плана, реестра и вариантов на фейковой модели."""

from __future__ import annotations

import pytest

from factories import empty_case, grounded_fact
from fake_brain import FakeBrain
from fakes import ScriptedDecision, make_context
from strategist.application.use_cases import claims as claims_uc
from strategist.application.use_cases import frame, options, research
from strategist.domain.entities import ClaimType, OptionKind, SupportLabel, UnknownStatus


def _with_frame(clarify: bool = False):
    ctx = make_context(llm=FakeBrain(clarify=clarify), settings={"max_clarifying_questions": 3})
    case = empty_case()
    frame.frame_decision(ctx, case)
    return ctx, case


def test_frame_keeps_at_most_three_changing_questions():
    ctx, case = _with_frame(clarify=True)
    assert case.frame.premise and case.frame.competing_frames
    assert 1 <= len(case.frame.clarifying_questions) <= 3
    assert case.frame.playbook  # плейбук выбран decide()


def test_plan_researches_only_winner_changing_factors():
    ctx, case = _with_frame()
    plan = research.plan_research(ctx, case)
    by_unc = {q.uncertainty_id: q for q in plan.questions if q.uncertainty_id}
    assert by_unc["U-001"].status.value == "planned"  # готовность платить меняет победителя
    assert by_unc["U-002"].status.value == "skipped" and by_unc["U-002"].saved_tokens_estimate > 0
    nice = [q for q in plan.questions if not q.uncertainty_id]
    assert nice and nice[0].status.value == "skipped"
    unk = {u.question: u for u in case.unknowns}
    flipping = next(u for u in unk.values() if u.flips_decision)
    assert flipping.status == UnknownStatus.OPEN and flipping.estimated_cost and flipping.deadline
    assert any(u.status == UnknownStatus.SKIPPED for u in unk.values())
    assert ctx.events.of("research.planned")[0].data["saved_tokens"] > 0


def test_claims_written_from_quotes_and_typed_by_decide():
    def rule(q):
        if "Неизвестна" in q.state:
            return "unknown", 0.95
        return ("fact", 0.95) if "снизил" in q.state else ("hypothesis", 0.95)

    ctx = make_context(llm=FakeBrain(), decision=ScriptedDecision(rule))
    case = empty_case()
    grounded_fact(case, "исходная строка")  # даёт цитату EV-001 в кейсе
    new = claims_uc.extract_claims(ctx, case)
    facts = [c for c in new if c.type == ClaimType.FACT]
    assert facts and all(c.evidence_ids for c in facts)
    assert any(c.type == ClaimType.UNKNOWN and c.verification_note for c in new)
    counters = claims_uc.verify_claims(ctx, case, facts)
    assert counters["checked"] == len(facts)
    assert all(c.support in (SupportLabel.SUPPORTED, SupportLabel.PARTIAL) for c in facts)


def test_mandatory_options_added_when_model_skips_them():
    class NoMandatory(FakeBrain):
        def _OptionsDTO(self, text, untrusted):
            data = super()._OptionsDTO(text, untrusted)
            data["options"] = [o for o in data["options"] if o["kind"] not in ("status_quo", "kill")]
            return data

    ctx = make_context(llm=NoMandatory())
    case = empty_case()
    grounded_fact(case)
    opts = options.generate_options(ctx, case)
    kinds = {o.kind for o in opts}
    assert {OptionKind.STATUS_QUO, OptionKind.KILL} <= kinds
    assert len(ctx.events.of("options.mandatory_added")) == 2
    assert [c.task for c in ctx.llm.calls].count("options") == 2  # повтор с замечанием, затем правило


def test_numbers_from_listed_support_are_accepted_and_wrong_ones_flagged():
    from factories import add_option
    from strategist.application.use_cases.report import narrative_number_mismatches
    from strategist.domain.entities.options import Objection, ObjectionKind

    case = empty_case()
    case.brief = "Удержание падает"
    locator = grounded_fact(case, "Встроенный «Локатор» на iPhone бесплатен")
    switchers = grounded_fact(case, "37% противников подписок заменяют их бесплатными аналогами")
    stress = grounded_fact(case, "Вариант лучший в 57 из 81 сценария: 70.4%")
    option = add_option(case, OptionKind.KILL)
    case.objections += [
        Objection(
            id="OBJ-001",
            option_id=option.id,
            kind=ObjectionKind.STEELMAN,
            text=f"37% противников подписок уходят к бесплатным аналогам, есть «Локатор» ({locator.id}).",
            claim_ids=[switchers.id],
        ),
        Objection(
            id="OBJ-002",
            option_id=option.id,
            kind=ObjectionKind.STEELMAN,
            text=f"Вариант лучший в 57 из 81 сценария (≈76%) [{stress.id}].",
            claim_ids=[stress.id],
        ),
    ]
    problems = narrative_number_mismatches(case)
    assert len(problems) == 1 and problems[0].startswith("76")


def test_fact_without_quote_becomes_hypothesis():
    from strategist.application.dto.llm_outputs import ClaimDTO
    from strategist.application.use_cases.claims import claim_from_dto

    ctx = make_context()
    case = empty_case()
    quoted = grounded_fact(case)
    no_quote = ClaimDTO(key="C1", text="Конверсия в оплату 46,8% в новом онбординге", type="fact")
    assert claim_from_dto(ctx, case, no_quote, []).type == ClaimType.HYPOTHESIS
    with_quote = no_quote.model_copy(update={"evidence_ids": quoted.evidence_ids})
    assert claim_from_dto(ctx, case, with_quote, []).type == ClaimType.FACT


def test_mean_of_percentages_gets_a_warning_in_claim_text(tmp_path):
    from factories import add_source
    from fakes import SimpleTableEngine
    from strategist.application.dto.llm_outputs import MetricProposalDTO
    from strategist.application.use_cases.collect import compute_metric
    from strategist.domain.entities import SourceKind

    table = tmp_path / "renewals.csv"
    table.write_text(
        "group,segment,renewals_due,churn_at_renewal_pct,churned\n"
        "тест,текущие,35210,24.8,1\nтест,новые,1697,7.3,0\n",
        encoding="utf-8",
    )
    ctx = make_context(tables=SimpleTableEngine())
    case = empty_case()
    src = add_source(case, kind=SourceKind.TABLE, path=str(table))
    rate = compute_metric(
        ctx, case, src, MetricProposalDTO(name="churn", agg="mean", column="churn_at_renewal_pct"), []
    )
    assert rate.vulnerable and "среднее столбца с долями «churn_at_renewal_pct»" in rate.text
    assert ctx.events.of("metric.mean_of_rates")
    flag = compute_metric(ctx, case, src, MetricProposalDTO(name="churned", agg="mean", column="churned"), [])
    assert not flag.vulnerable and "Внимание" not in flag.text  # 0/1 по объектам — честная доля


def test_measurement_supersedes_measurement_but_not_threshold_or_forecast():
    from factories import add_claim
    from strategist.application.dto.llm_outputs import RelationDTO
    from strategist.application.use_cases.revise import apply_relations
    from strategist.domain.entities import ClaimStatus

    ctx = make_context()
    case = empty_case()
    old_metric = grounded_fact(case, "Отток при продлении в тесте: 0,0% (продлений ещё не было)")
    threshold = add_claim(
        case, "Повышение окупается, если отток при продлении не превысит 21,7%", ClaimType.FORECAST
    )
    before = {c.id for c in case.claims}
    new = grounded_fact(case, "Отток при первом продлении по новой цене: 24,8%")
    rels = [
        RelationDTO(
            new_claim_key="N0", existing_claim_id=old_metric.id, relation="supersedes", explanation="свежее"
        ),
        RelationDTO(
            new_claim_key="N0", existing_claim_id=threshold.id, relation="supersedes", explanation="порог"
        ),
    ]
    invalidated = apply_relations(ctx, case, rels, {"N0": new.id}, before)
    assert invalidated == [old_metric.id]
    assert old_metric.status == ClaimStatus.SUPERSEDED and threshold.status == ClaimStatus.ACTIVE
    assert [e.data["existing"] for e in ctx.events.of("revision.relation_ignored")] == [threshold.id]


def test_report_leads_with_new_recommendation_when_decision_reopened():
    from factories import NOW, add_option
    from strategist.application.use_cases.report import render_markdown
    from strategist.domain.entities import Decision, DecisionStatus, OptionStatus

    ctx = make_context()
    case = empty_case()
    old = add_option(case, OptionKind.FOCUS, "Повысить для всех")
    alt = add_option(case, OptionKind.PIVOT, "Повысить только для новых")
    case.selected_option_id = old.id
    case.decisions.append(
        Decision(
            id="DEC-0001",
            selected_option_id=old.id,
            rationale="—",
            status=DecisionStatus.REOPENED,
            created_at=NOW,
        )
    )
    alt.status = OptionStatus.REOPENED
    head = render_markdown(ctx, case).split("## Панель самоаудита")[0]
    assert "Новая рекомендация агента: Повысить только для новых" in head
    assert "Прежняя рекомендация: Повысить для всех" in head


def test_kill_criterion_crossed_by_new_data_fires_and_is_journaled():
    from factories import NOW
    from fakes import ScriptedDecision
    from strategist.application.use_cases.revise import check_signals
    from strategist.domain.entities import Decision, KillCriterion, Trigger, TriggerKind

    def rule(q):
        return ("fired", 0.9) if "окупаемости" in q.state else ("not_fired", 0.9)

    ctx = make_context(decision=ScriptedDecision(rule))
    case = empty_case()
    new = grounded_fact(case, "Доля продлений по новой цене в тесте: 76,0% (отток 24%)")
    decision = Decision(
        id="DEC-0001",
        selected_option_id="OPT-001",
        rationale="—",
        created_at=NOW,
        triggers=[Trigger(id="TRG-001", kind=TriggerKind.SIGNAL, description="Конкуренты откатят цены")],
        kill_criteria=[KillCriterion(signal="Отток на продлении выше порога окупаемости", threshold="21,7%")],
    )
    case.decisions.append(decision)
    fired = check_signals(ctx, case, decision, [new.id])
    assert len(fired) == 1
    kill = next(t for t in decision.triggers if t.id == fired[0])
    assert kill.kind == TriggerKind.KILL and kill.fired and kill.condition == "21,7%"
    assert not decision.triggers[0].fired
    assert ctx.events.of("revision.signals_fired")
    assert check_signals(ctx, case, decision, []) == []


def test_signal_check_asks_about_the_condition_not_a_stated_fact():
    """Описание «Опровергнуто допущение …» в прошедшем времени модель читала как наступившее событие: в демо
    A/B-тест подтвердил выбранный вариант, а сигналы «сработали». Вопрос должен спрашивать про условие."""
    from factories import NOW
    from fakes import ScriptedDecision
    from strategist.application.use_cases.revise import check_signals
    from strategist.domain.entities import Decision, Trigger, TriggerKind

    asked = []

    def rule(q):
        asked.append(q)
        return ("not_fired", 0.95)

    ctx = make_context(decision=ScriptedDecision(rule))
    case = empty_case()
    new = grounded_fact(case, "Новый онбординг: M1 46,8% против 43,3% в контроле")
    legacy = Trigger(
        id="TRG-001",
        kind=TriggerKind.SIGNAL,
        description="Опровергнуто допущение [CL-064]: Правка экрана повышает долю разрешений",
        condition="A/B-тест не даст прироста конверсии шага",
    )
    decision = Decision(
        id="DEC-0001", selected_option_id="OPT-001", rationale="—", created_at=NOW, triggers=[legacy]
    )
    case.decisions.append(decision)
    assert check_signals(ctx, case, decision, [new.id]) == []
    state = asked[0].state
    assert "Опровергнуто" not in state
    assert "Что проверяем: допущение решения [CL-064]" in state
    assert "Сигнал срабатывает, только если: A/B-тест не даст прироста конверсии шага" in state
    assert "подтверждают допущение" in asked[0].options["not_fired"]


def test_assumption_triggers_describe_what_is_watched():
    from factories import add_claim, add_option
    from fakes import ScriptedLLM
    from strategist.application.ports import GateAction, GateResponse
    from strategist.application.use_cases.decision import commit_decision

    record = {"rationale": "—", "confirmation": "M1 через 45 дней"}
    ctx = make_context(llm=ScriptedLLM({"experiments": record}))
    case = empty_case()
    assumption = add_claim(case, "Правка экрана повышает долю разрешений", ClaimType.ASSUMPTION, [])
    option = add_option(case, OptionKind.FOCUS, "Починить онбординг")
    option.assumption_claim_ids = [assumption.id]
    case.selected_option_id = option.id
    decision = commit_decision(ctx, case, GateResponse(action=GateAction.APPROVE, approver="руководитель"))
    watched = [t for t in decision.triggers if assumption.id in t.assumption_claim_ids]
    assert watched and watched[0].description.startswith(f"Допущение решения [{assumption.id}]")
    assert not watched[0].fired


def test_reopened_option_is_refined_with_new_data():
    from factories import add_option
    from fakes import ScriptedLLM
    from strategist.application.use_cases.revise import _refine_reopened
    from strategist.domain.entities import NodeVerdict

    case = empty_case()
    option = add_option(
        case, OptionKind.PIVOT, "Повысить сегментированно", summary="479 ₽ давним подписчикам"
    )
    new = grounded_fact(case, "Отток при продлении: текущие 24,8%, новые 7,3%")
    llm = ScriptedLLM(
        {
            "revise": {
                "verdicts": [
                    {
                        "node_id": option.id,
                        "verdict": "revised",
                        "new_text": "479 ₽ только новым, текущим — 399 ₽",
                        "reason": f"[{new.id}]",
                    }
                ]
            }
        }
    )
    ctx = make_context(llm=llm)
    result = _refine_reopened(ctx, case, option.id, [], [new])
    assert result.verdict == NodeVerdict.REVISED and option.summary == "479 ₽ только новым, текущим — 399 ₽"
    assert new.id in llm.calls[0].messages[0].content


def test_new_data_resolve_uncertainty_only_with_reference_to_new_rows():
    from strategist.application.dto.llm_outputs import LevelUpdateDTO
    from strategist.application.use_cases.options import _apply_level_updates
    from strategist.domain.entities import Uncertainty, UncertaintyLevel

    ctx = make_context()
    case = empty_case()
    unc = Uncertainty(
        id="U-001",
        name="Отток текущих при продлении",
        levels=[
            UncertaintyLevel(name="низкий", probability=0.6),
            UncertaintyLevel(name="высокий", probability=0.4),
        ],
        base_level="низкий",
    )
    ok = LevelUpdateDTO(uncertainty_id="U-001", observed_level="высокий", evidence=["CL-040"])
    [resolved] = _apply_level_updates(ctx, case, [unc], [ok], ["CL-040"])
    assert resolved.base_level == "высокий"
    assert [lv.probability for lv in resolved.levels] == [pytest.approx(0.1), pytest.approx(0.9)]
    assert ctx.events.of("stress.uncertainty_resolved")
    # без ссылки на новую строку — мнение модели, а не данные
    no_evidence = LevelUpdateDTO(uncertainty_id="U-001", observed_level="высокий", evidence=["CL-001"])
    assert _apply_level_updates(ctx, case, [unc], [no_evidence], ["CL-040"])[0].base_level == "низкий"
    wrong_level = LevelUpdateDTO(uncertainty_id="U-001", observed_level="очень высокий", evidence=["CL-040"])
    assert _apply_level_updates(ctx, case, [unc], [wrong_level], ["CL-040"])[0].base_level == "низкий"


def test_report_with_numbers_v2_groups_problems_and_counts_numbers_and_labels_model_results():
    from factories import add_option
    from strategist.application.use_cases.report import render_markdown
    from strategist.domain.entities import ClaimStatus, OptionKind

    on = {"quality": {"numbers_v2": True, "model_labels": True}}
    case = empty_case()
    live = grounded_fact(case, "Удержание M1 составило 84,1%")
    gone = grounded_fact(case, "Удержание M1 составило 58,9%")
    gone.status = ClaimStatus.RETRACTED  # отозванная строка не опора (v1 сверял и с ней)
    add_option(
        case,
        OptionKind.FOCUS,
        "Удержать",
        rationale=f"Удержание M1 — 84,1% [{live.id}], а по старым данным 58,9% [{gone.id}]; доля 84,1 п.п. [{live.id}].",
    )
    text = render_markdown(make_context(settings=on), case)
    assert "| Числа в тексте: проверено / без опоры / другая единица / не та строка | 3 / 1 / 1 / 0 |" in text
    block = text.split("**Проверьте числа:**")[1].split("## Картина ситуации")[0]
    assert "*Нет опоры* (1):" in block and "58,9%" in block
    assert "*Другая единица, знак или подпись* (1):" in block and "84,1 п.п." in block
    assert "Панель ролей (оценка модели)" in text
    old = render_markdown(make_context(), case)  # флаги выключены — прежняя строка и прежний блок
    assert "| Числа, не совпадающие с опорой |" in old and "Панель ролей (оценка модели)" not in old
