"""Выжимка с проверкой чисел (summary_repair): один повтор по замечаниям, замена довода, статусы и оговорки."""

from __future__ import annotations

import json

from factories import NOW, add_claim, add_option, empty_case, grounded_fact
from fakes import ListEventBus, ScriptedLLM, make_context
from strategist.application.use_cases.executive import write_executive_summary
from strategist.domain.entities import ClaimType, Decision, OptionKind

ON = {"quality": {"summary_repair": True}}


def _case():
    case = empty_case()
    retention = grounded_fact(case, "Удержание M1 составило 84,1%")
    price = grounded_fact(case, "Цена выросла с 399 ₽ до 479 ₽")
    guess = add_claim(
        case,
        "Сериалы вернут уходящих подписчиков",
        ClaimType.HYPOTHESIS,
        falsification_test="Тест на когорте",
        what_would_change="удержание M1 не вырастет за квартал",
    )
    option = add_option(case, OptionKind.FOCUS, "Снять барьер оплаты", rationale="—")
    case.selected_option_id = option.id
    case.decisions.append(
        Decision(
            id="DEC-0001",
            selected_option_id=option.id,
            rationale="—",
            load_bearing_claim_ids=[retention.id, price.id],
            vulnerable_claim_ids=[guess.id],
            created_at=NOW,
        )
    )
    return case, retention, price, guess


def _summary(reasons: list[str], answer: str = "Снимаем барьер оплаты.", revisit: str = "Пересмотр в марте."):
    return {
        "answer": answer,
        "reasons": reasons,
        "next_steps": [{"when": "2026-12-01", "action": "Запустить тест", "owner": "продакт"}],
        "asks": ["Утвердить тест"],
        "revisit": revisit,
        "say_to_team": "Чиним оплату.",
    }


def _ctx(script, settings=ON):
    llm = ScriptedLLM({"executive_summary": script})
    return make_context(llm=llm, settings=settings, events=ListEventBus()), llm


def _values(call) -> dict:
    """Фейковый шаблон отдаёт значения промпта как JSON — видно, что ушло в слоты."""
    return json.loads(call.messages[0].content)


def test_clean_summary_costs_one_call_and_gets_basis_for_each_reason():
    case, retention, price, guess = _case()
    ctx, llm = _ctx(
        _summary(
            [
                f"Удержание M1 — 84,1% [{retention.id}].",
                f"Цена выросла на 80 ₽ [{price.id}].",
                f"Сериалы могут вернуть уходящих [{guess.id}].",
            ]
        )
    )
    s = write_executive_summary(ctx, case)
    assert llm.tasks() == ["executive_summary"]
    assert llm.calls[0].system == "system:executive_summary_v2" and _values(llm.calls[0])["feedback"] == "—"
    assert [b.kind_label for b in s.basis] == ["факт из материалов", "факт из материалов", "гипотеза"]
    assert s.basis[0].source.startswith("цитата из SRC-") and s.basis[0].verified and not s.basis[0].replaced
    assert "изменит вывод: удержание M1 не вырастет за квартал" in s.basis[2].caveat
    assert "уязвимое основание решения" in s.basis[2].caveat
    assert s.unverified == []


def test_number_error_triggers_one_retry_with_feedback():
    case, retention, price, _ = _case()
    bad = _summary([f"Удержание M1 — 84,1 п.п. [{retention.id}].", f"Цена 479 ₽ [{price.id}]."])
    good = _summary([f"Удержание M1 — 84,1% [{retention.id}].", f"Цена 479 ₽ [{price.id}]."])
    ctx, llm = _ctx([bad, good])
    s = write_executive_summary(ctx, case)
    assert llm.tasks() == ["executive_summary", "executive_summary"]
    retry = llm.calls[1]
    assert [m.role for m in retry.messages] == ["user"] and retry.step == "summary:fix1"
    first, again = _values(llm.calls[0]), _values(retry)
    assert "довод 1: 84,1 п.п. — другая единица" in again["feedback"]
    assert (
        f"1) Удержание M1 — 84,1 п.п. [{retention.id}]." in again["feedback"]
    )  # прошлые доводы видны модели
    assert {k: v for k, v in first.items() if k != "feedback"} == {
        k: v for k, v in again.items() if k != "feedback"
    }
    assert s.reasons[0] == f"Удержание M1 — 84,1% [{retention.id}]." and not any(b.replaced for b in s.basis)
    assert ctx.events.of("summary.repaired")


def test_two_failures_replace_the_reason_with_its_registry_row_and_mark_free_text_unverified():
    case, retention, price, _ = _case()
    bad = _summary(
        [f"Удержание M1 — 61% [{retention.id}].", f"Цена 479 ₽ [{price.id}]."],
        answer="Снимаем барьер, это даст 35% роста.",
    )
    ctx, llm = _ctx([bad, bad])
    s = write_executive_summary(ctx, case)
    assert len(llm.calls) == 2
    assert s.reasons[0] == f"Удержание M1 составило 84,1% [{retention.id}]"
    assert s.basis[0].replaced and s.basis[0].verified
    assert s.reasons[1] == f"Цена 479 ₽ [{price.id}]." and not s.basis[1].replaced  # верный довод не трогаем
    assert s.unverified == ["answer"]
    assert ctx.events.of("summary.fallback")


def test_without_the_flag_the_path_is_v011():
    case, retention, _, _ = _case()
    ctx, llm = _ctx(_summary([f"Удержание M1 — 61% [{retention.id}]."]), settings={})
    s = write_executive_summary(ctx, case)
    assert llm.tasks() == ["executive_summary"] and s.basis == [] and s.unverified == []
    assert llm.calls[0].system == "system:executive_summary" and "feedback" not in _values(llm.calls[0])
    assert s.reasons == [f"Удержание M1 — 61% [{retention.id}]."]


def test_report_shows_basis_and_unverified_marks():
    from strategist.application.use_cases.report import executive_md

    case, retention, price, _ = _case()
    bad = _summary([f"Удержание M1 — 61% [{retention.id}]."], revisit="Пересмотр, если отток превысит 37%.")
    ctx, _ = _ctx([bad, bad])
    write_executive_summary(ctx, case)
    text = "\n".join(executive_md(case))
    assert "*Опора:* факт из материалов: цитата из SRC-" in text
    assert "довод собран из текста строки реестра" in text
    assert (
        "**Когда пересматриваем:**" in text and "*(числа не сверены)*" not in text.split("**Ответ:**")[1][:60]
    )


def test_a_registry_number_cited_from_the_wrong_row_triggers_a_retry():
    """Число есть в списке реестра, переданном модели, но довод ссылается на другую строку — это ошибка ссылки."""
    case, retention, price, _ = _case()
    wrong = _summary([f"Удержание M1 — 84,1% [{price.id}]."])  # 84,1% — в строке retention, а не price
    right = _summary([f"Удержание M1 — 84,1% [{retention.id}]."])
    ctx, llm = _ctx([wrong, right])
    s = write_executive_summary(ctx, case)
    assert (
        len(llm.calls) == 2
        and "довод 1: 84,1% — число есть, но в другой строке реестра" in _values(llm.calls[1])["feedback"]
    )
    assert s.reasons == [f"Удержание M1 — 84,1% [{retention.id}]."]


def test_a_number_from_the_decision_text_does_not_stand_in_for_the_cited_row():
    """Обоснование решения во входах шага цитирует 479 ₽ из строки цены. Довод с этим числом, но со ссылкой только
    на строку удержания — ошибка ссылки: число довода должно прийти через строку, на которую он ссылается."""
    case, retention, price, _ = _case()
    case.decisions[0].rationale = f"Цена выросла до 479 ₽ [{price.id}], удержание M1 84,1% [{retention.id}]"
    wrong = _summary([f"Удержание M1 84,1% даже при цене 479 ₽ [{retention.id}]."])
    right = _summary([f"Удержание M1 84,1% [{retention.id}] даже при цене 479 ₽ [{price.id}]."])
    ctx, llm = _ctx([wrong, right])
    s = write_executive_summary(ctx, case)
    assert len(llm.calls) == 2 and "довод 1: 479 ₽" in _values(llm.calls[1])["feedback"]
    assert s.reasons == right["reasons"]


def test_numbers_from_the_brief_still_count_for_a_reason():
    case, retention, _, _ = _case()
    case.brief = "Бюджет на эксперимент — 2 млн ₽. Почему падает удержание?"
    ctx, llm = _ctx(_summary([f"Удержание M1 — 84,1% [{retention.id}], на проверку есть 2 млн ₽."]))
    write_executive_summary(ctx, case)
    assert len(llm.calls) == 1
