"""Сверка чисел v2: единицы, выведенные числа, цели и честные вердикты."""

from __future__ import annotations

import pytest

from strategist.domain.services.numbers import Verdict, check_sentence, check_text, extract_quantities


def _q(text: str) -> list[tuple[float, str]]:
    return [(round(q.value, 6), q.unit) for q in extract_quantities(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Цена 1 234 ₽", [(1234.0, "rub")]),
        ("Цена 1 234,5 руб.", [(1234.5, "rub")]),
        ("Сдвиг −3,2 п.п.", [(-3.2, "pp")]),
        ("Сдвиг -3,2 п. п.", [(-3.2, "pp")]),
        ("Рост +5%", [(5.0, "pct")]),
        ("Доля 53–60%", [(53.0, "pct"), (60.0, "pct")]),
        ("Конкуренты подняли цены на 12,5–33%", [(12.5, "pct"), (33.0, "pct")]),
        ("Подключений 6,8 тыс.", [(6800.0, "count")]),
        ("Бюджет 38 млн ₽", [(38_000_000.0, "rub")]),
        ("Срок 1,34 мес.", [(1.34, "month")]),
        ("Перенос до 8 дней", [(8.0, "day")]),
        ("Тест шёл 6 недель", [(6.0, "week")]),
    ],
)
def test_extract_quantities_with_units(text, expected):
    assert _q(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "В 2028 году повторить",  # календарный год
        "Проверить до 15.03.2027",  # дата
        "Когорта 2026-01 и 2026-02",  # месяцы когорт
        "Удержание M1 и M3 [CL-012]",  # когорты и коды строк
        "Вариант OPT-003, источник SRC-005",  # коды
        "Опрошено 300 человек",  # число без единицы не сверяется
        "Семьи с детьми до 14 лет и подростки 14–17 лет",  # возраст
    ],
)
def test_years_dates_ids_cohorts_and_bare_numbers_are_skipped(text):
    assert extract_quantities(text) == []


CLAIMS = {
    "CL-001": "Цена тарифа выросла с 399 ₽ до 479 ₽",
    "CL-002": "Удержание M1 составило 84,1%",
    "CL-003": "Средний срок до отмены 1,34 мес.",
    "CL-004": "Доля ушедших из-за цены 13%",
    "CL-005": "Удержание M2 снизилось на −3,2 п.п.",
    "CL-006": "Конверсия пробного периода 42,4%",
}


def _verdicts(sentence: str, **kw) -> list[Verdict]:
    return [f.verdict for f in check_sentence(sentence, CLAIMS, **kw)]


def test_exact_and_rounded_numbers_are_supported():
    assert _verdicts("Удержание M1 — 84,1% [CL-002].") == [Verdict.OK]
    assert _verdicts("Удержание M1 — 84% [CL-002].") == [Verdict.ROUNDED]
    assert _verdicts("Цена 479 ₽ [CL-001].") == [Verdict.OK]


def test_derived_numbers_from_cited_rows_are_supported():
    assert _verdicts("Цена выросла на 80 ₽ [CL-001].") == [Verdict.DERIVED]  # 479 − 399
    assert _verdicts("Цена выросла на 20,1% [CL-001].") == [Verdict.DERIVED]  # (479 − 399) / 399
    assert _verdicts("Разрыв 71,1 п.п. между 84,1% [CL-002] и 13% [CL-004].") == [
        Verdict.DERIVED,
        Verdict.OK,
        Verdict.OK,
    ]


def test_units_matter():
    assert _verdicts("Доля ушедших из-за цены 13 п.п. [CL-004].") == [Verdict.UNIT_MISMATCH]
    assert _verdicts("Средний срок до отмены 1,34 дня [CL-003].") == [Verdict.UNIT_MISMATCH]
    assert _verdicts("Цена 479% [CL-001].") == [Verdict.UNIT_MISMATCH]


def test_sign_is_checked_unless_the_text_says_it_decreased():
    assert _verdicts("Удержание M2 изменилось на 3,2 п.п. [CL-005].") == [Verdict.UNIT_MISMATCH]
    assert _verdicts("Удержание M2 снизилось на 3,2 п.п. [CL-005].") == [Verdict.OK]
    assert _verdicts("Удержание M2: −3,2 п.п. [CL-005].") == [Verdict.OK]


def test_shifted_value_is_unsupported():
    assert _verdicts("Удержание M1 — 58,9% [CL-002].") == [Verdict.UNSUPPORTED]


def test_number_from_another_row_or_without_reference():
    assert _verdicts("Конверсия пробного периода 42,4% [CL-002].") == [Verdict.ELSEWHERE]
    assert _verdicts("Конверсия пробного периода 42,4%.") == [Verdict.NO_REF]
    finding = check_sentence("Конверсия 42,4% [CL-002].", CLAIMS)[0]
    assert finding.other_ids == ["CL-006"] and finding.flagged


def test_targets_and_brief_numbers_are_not_errors():
    assert _verdicts("Удержание 84,1% [CL-002]; цель — не ниже 50%.") == [Verdict.OK, Verdict.TARGET]
    assert _verdicts("Kill-критерий: если конверсия ниже 30% [CL-006].") == [Verdict.TARGET]
    assert _verdicts("Маржа должна вернуться к 30% [CL-006].", brief="Маржа упала ниже целевых 30%.") == [
        Verdict.BRIEF
    ]


def test_dates_ids_and_years_in_a_cited_sentence_are_ignored():
    assert _verdicts("Удержание 84,1% [CL-002]; проверить до 15.03.2027, повтор — в 2028 году; OPT-003.") == [
        Verdict.OK
    ]


def test_thousands_separator_matches_both_ways():
    claims = {"CL-010": "Средний чек 2590 ₽"}
    assert [f.verdict for f in check_sentence("Чек 2 590 ₽ [CL-010].", claims)] == [Verdict.OK]
    claims = {"CL-010": "Средний чек 2 590 ₽"}
    assert [f.verdict for f in check_sentence("Чек 2590 ₽ [CL-010].", claims)] == [Verdict.OK]


def test_element_support_counts_as_cited():
    assert _verdicts("Цена 479 ₽.", support_ids=["CL-001"]) == [Verdict.OK]


def test_check_text_returns_every_number_and_flags_only_errors():
    text = "Удержание M1 — 84,1% [CL-002]. Цена выросла на 80 ₽ [CL-001]. Доля 13 п.п. [CL-004]."
    findings = check_text(text, CLAIMS)
    assert [f.verdict for f in findings] == [Verdict.OK, Verdict.DERIVED, Verdict.UNIT_MISMATCH]
    assert [f.flagged for f in findings] == [False, False, True]


def test_ranges_check_both_ends():
    claims = {"CL-020": "Конкуренты подняли цены на 12,5–33%"}
    assert [f.verdict for f in check_sentence("Рост цен у конкурентов 12,5–33% [CL-020].", claims)] == [
        Verdict.OK,
        Verdict.OK,
    ]
    assert [f.verdict for f in check_sentence("Рост цен 12,5–43% [CL-020].", claims)][
        1
    ] == Verdict.UNSUPPORTED


def test_churn_written_over_a_retention_figure_is_flagged():
    claims = {
        "CL-007": "Доля оставшихся: active_m2 / active_m1: organic 84,1%",
        "CL-008": "Доля оттока в первый месяц 72,7%",
    }
    assert [f.verdict for f in check_sentence("Отток между M1 и M2 — 84,1% [CL-007].", claims)] == [
        Verdict.UNIT_MISMATCH
    ]
    assert [f.verdict for f in check_sentence("Удержание между M1 и M2 — 84,1% [CL-007].", claims)] == [
        Verdict.OK
    ]
    assert [f.verdict for f in check_sentence("Отток в первый месяц 72,7% [CL-008].", claims)] == [Verdict.OK]
    both = {"CL-009": "Удержание 84,1%, отток 15,9%"}
    assert [f.verdict for f in check_sentence("Отток 15,9% [CL-009].", both)] == [Verdict.OK]


def test_conditions_horizons_and_thresholds_are_not_facts():
    assert _verdicts("Через 12 месяцев удержание не растёт [CL-002].") == [Verdict.TARGET]
    assert _verdicts(
        "Пересмотреть, если доля потерь составит менее 20% или рост меньше 2 п.п. [CL-002]."
    ) == [
        Verdict.TARGET,
        Verdict.TARGET,
    ]
    assert _verdicts("Удержание выше 60% [CL-002].") == [Verdict.TARGET]
    assert _verdicts("Удержание составило 60% [CL-002].") == [Verdict.UNSUPPORTED]  # факт без опоры — ошибка


def test_from_to_gives_the_first_number_the_unit_of_the_second():
    assert _q("Поднять цену с 399 до 479 ₽ в месяц") == [(399.0, "rub"), (479.0, "rub")]
    assert _q("Маржа упала от 31 до 26%") == [(31.0, "pct"), (26.0, "pct")]


def test_brief_numbers_are_operands_for_derived_values():
    brief = "Хотим поднять цену с 399 до 479 ₽ в месяц."
    claims = {"CL-030": "Маржа снизилась с 31% до 26%"}
    findings = check_sentence("Повышение на 80 ₽ (+20%) [CL-030].", claims, brief=brief)
    assert [f.verdict for f in findings] == [Verdict.DERIVED, Verdict.DERIVED]


def test_approximate_wording_allows_coarser_rounding():
    claims = {"CL-008": "Конверсия списков в корзину 61,7%"}
    assert [f.verdict for f in check_sentence("Конверсия около 60% [CL-008].", claims)] == [Verdict.ROUNDED]
    assert [f.verdict for f in check_sentence("Конверсия 60% [CL-008].", claims)] == [Verdict.UNSUPPORTED]


def test_a_registry_row_without_units_still_supports_its_values():
    claims = {
        "CL-015": "metric [расчёт: среднее retention_m3_pct; по «group»]: сопоставимые: 75.4; пользователи: 78.4"
    }
    assert [f.verdict for f in check_sentence("Удержание 78,4% против 75,4% [CL-015].", claims)] == [
        Verdict.ROUNDED,
        Verdict.ROUNDED,
    ]
