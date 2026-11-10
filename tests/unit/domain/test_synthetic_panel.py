from strategist.domain.services.synthetic_panel import (
    QuestionIssue,
    lint_interview_guide,
    lint_question,
    panel_verdict,
)


def test_hypothetical_and_idea_questions_are_flagged_and_rewritten():
    finding = lint_question("Купили бы вы подписку на семейную геолокацию?")
    assert QuestionIssue.HYPOTHETICAL in finding.issues
    assert finding.suggestion.startswith("Расскажите, когда в последний раз")
    assert (
        QuestionIssue.OPINION_ABOUT_IDEA
        in lint_question("Нравится ли вам идея родительского контроля?").issues
    )


def test_leading_double_and_generic_questions():
    assert QuestionIssue.LEADING in lint_question("Согласитесь, настройка слишком сложная?").issues
    assert QuestionIssue.DOUBLE in lint_question("Как вы подключали ребёнка и почему бросили?").issues
    assert QuestionIssue.GENERIC in lint_question("Как часто вы проверяете, где ребёнок?").issues


def test_rewrites_drop_leading_and_split_double_questions():
    leading = lint_question("Согласитесь, настройка слишком сложная?").suggestion
    assert "Согласитесь" not in leading and "«настройка слишком сложная»" in leading
    hypothetical = lint_question("Купили бы вы подписку на семейную геолокацию?").suggestion
    assert "бы вы" not in hypothetical and "«подписку на семейную геолокацию»" in hypothetical
    double = lint_question("Как вы подключали ребёнка и почему бросили?").suggestion
    assert double.startswith("Разделите на отдельные вопросы")
    assert "«подключали ребёнка»" in double and "«бросили»" in double
    # переписанный вопрос сам проходит линтер
    for question in [
        "Купили бы вы подписку?",
        "Согласитесь, это неудобно?",
        "Как часто вы проверяете, где ребёнок?",
    ]:
        assert lint_question(lint_question(question).suggestion).issues == []


def test_good_question_passes():
    findings = lint_interview_guide(
        ["Расскажите, когда в последний раз вы искали, где ребёнок. Что вы сделали?"]
    )
    assert findings[0].issues == []


def test_placebo_liked_lowers_trust():
    verdict = panel_verdict({"real": [3.0, 3.5, 2.5], "placebo": [4.0, 4.5, 4.0]}, "placebo")
    assert verdict.placebo_liked and verdict.trust == "low"
    assert verdict.label.startswith("СИНТЕТИКА")


def test_framing_flip_and_spread_warning():
    verdict = panel_verdict(
        {"real": [3.9, 4.0, 4.1], "placebo": [1.0, 1.5]},
        "placebo",
        framing_pairs={"Удобно ли?": (4.5, 4.0), "Цена приемлема?": (4.0, 1.5)},
    )
    assert verdict.framing_sensitive_questions == ["Удобно ли?"]
    assert verdict.trust == "low"
    assert verdict.spread_warning


def test_ssr_turns_text_similarity_into_a_score():
    from strategist.domain.services.synthetic_panel import ssr_score

    assert ssr_score([0.1, 0.1, 0.2, 0.6, 0.9]) > 4.0
    assert ssr_score([0.9, 0.6, 0.2, 0.1, 0.1]) < 2.0
    assert ssr_score([0.5] * 5) is None  # одинаково похоже на все опоры — оценки нет
    assert ssr_score([0.5, 0.6]) is None


def test_disagreements_show_who_and_why_not_averages():
    from strategist.domain.entities import PersonaAnswer
    from strategist.domain.services.synthetic_panel import find_disagreements

    answers = [
        PersonaAnswer(
            persona_id="P1", concept_id="OPT-003", reaction="Сэкономит мне час в неделю", score=4.6
        ),
        PersonaAnswer(
            persona_id="P2", concept_id="OPT-003", reaction="Не доверяю", concern="боюсь слежки", score=1.4
        ),
        PersonaAnswer(persona_id="P1", concept_id="OPT-004", reaction="Нормально", score=3.0),
        PersonaAnswer(persona_id="P2", concept_id="OPT-004", reaction="Нормально", score=3.2),
        PersonaAnswer(persona_id="P1", concept_id="PLACEBO", reaction="Ну, новое имя", score=1.0),
        PersonaAnswer(persona_id="P2", concept_id="PLACEBO", reaction="Зачем", score=5.0),
    ]
    found = find_disagreements(answers, "PLACEBO")
    assert [d.concept_id for d in found] == ["OPT-003"]  # без согласного OPT-004 и без плацебо
    d = found[0]
    assert d.supporters == ["P1"] and d.opponents == ["P2"] and d.reasons_against == ["боюсь слежки"]
    assert not hasattr(d, "mean")


def test_quarantine_catches_model_respondents_as_fact_or_demand_number():
    from factories import add_claim, add_evidence, add_source, empty_case
    from strategist.domain.entities import ClaimType
    from strategist.domain.services.synthetic_panel import quarantine_violations

    case = empty_case()
    panel_src = add_source(case, synthetic=True)  # модельные респонденты
    simulated = add_source(case, synthetic=True, synthetic_kind="simulated_data")
    ev_panel, ev_sim = add_evidence(case, panel_src), add_evidence(case, simulated)
    fact = add_claim(case, "Персоны выбирают семейный тариф", ClaimType.FACT, [ev_panel])
    demand = add_claim(case, "62% персон купили бы подписку", ClaimType.HYPOTHESIS, [ev_panel])
    add_claim(case, "Персоны сомневаются в цене", ClaimType.SYNTHETIC, [ev_panel])
    add_claim(case, "Удержание M3 в смоделированных данных — 33%", ClaimType.FACT, [ev_sim])
    problems = quarantine_violations(case)
    assert [p.split(":")[0] for p in problems] == [fact.id, demand.id]


def test_neutral_answers_on_1_to_5_scale_are_not_a_framing_flip():
    verdict = panel_verdict(
        {"real": [5.0, 1.0, 3.0], "placebo": [1.0, 1.0, 1.0]},
        "placebo",
        framing_pairs={"real": (3.0, 3.0), "placebo": (1.0, 5.0)},
    )
    assert verdict.framing_sensitive_questions == [] and verdict.trust == "normal"
    assert panel_verdict({"real": [2.0], "placebo": [3.2]}, "placebo").placebo_liked  # не хуже реального
    assert not panel_verdict(
        {"real": [4.5], "placebo": [3.2]}, "placebo"
    ).placebo_liked  # ниже 3,5 и реального
