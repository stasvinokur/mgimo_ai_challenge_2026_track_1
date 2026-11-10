import pytest

from factories import add_claim, add_option, add_source, empty_case, grounded_fact
from strategist.domain.common import next_id
from strategist.domain.entities import (
    ClaimType,
    Likelihood,
    Objection,
    ObjectionKind,
    OptionKind,
    Origin,
    Reliability,
    SourceKind,
    SupportLabel,
    likelihood_from_probability,
)
from strategist.domain.services.citations import referenced_ids, uncited_sentences
from strategist.domain.services.reliability import assess_claim, source_prior
from strategist.domain.services.self_audit import compute_self_audit
from strategist.domain.services.similarity import cosine, find_clone_pairs


def test_next_id_never_reuses_numbers():
    assert next_id("CL", ["CL-001", "CL-007", "OPT-009"]) == "CL-008"
    assert next_id("DEC", [], width=4) == "DEC-0001"


def test_case_new_id_counts_all_collections():
    case = empty_case()
    grounded_fact(case)
    assert case.new_id("claim") == "CL-002"


@pytest.mark.parametrize(
    ("p", "label"),
    [
        (0.03, Likelihood.ALMOST_NO_CHANCE),
        (0.3, Likelihood.UNLIKELY),
        (0.5, Likelihood.ROUGHLY_EVEN),
        (0.7, Likelihood.LIKELY),
        (0.97, Likelihood.ALMOST_CERTAIN),
    ],
)
def test_icd203_scale(p, label):
    assert likelihood_from_probability(p) == label


def test_similarity_and_clones():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    pairs = find_clone_pairs({"A": [1, 0], "B": [0.99, 0.01], "C": [0, 1]}, 0.95)
    assert pairs[0][:2] == ("A", "B") and len(pairs) == 1


def test_citations_rule():
    text = "Удержание падает из-за цены, а не онбординга, судя по обращениям [CL-003]. Это предложение без ссылки вообще."
    assert referenced_ids(text) == {"CL-003"}
    assert uncited_sentences(text, min_length=20) == ["Это предложение без ссылки вообще."]


def test_reliability_grades():
    case = empty_case()
    synthetic = add_source(case, synthetic=True)
    assert source_prior(synthetic) == Reliability.VERY_LOW
    table = add_source(case, kind=SourceKind.TABLE)
    assert source_prior(table) == Reliability.MODERATE
    inference = add_claim(case, "Вывод агента", ClaimType.HYPOTHESIS, origin=Origin.AGENT_INFERENCE)
    assert assess_claim(inference, case) == Reliability.VERY_LOW


def test_self_audit_metrics():
    case = empty_case()
    good = grounded_fact(case)
    bad = grounded_fact(case, "Неподтверждённый факт")
    bad.support = SupportLabel.UNSUPPORTED
    add_source(case, title="Неиспользованный")
    opt = add_option(case, OptionKind.STATUS_QUO, supporting_claim_ids=[good.id])
    add_option(case, OptionKind.KILL)
    case.objections.append(Objection(id="OBJ-001", option_id=opt.id, kind=ObjectionKind.PREMORTEM, text="x"))
    audit = compute_self_audit(case)
    assert audit.claims_total == 2
    assert audit.unsupported_share == 0.5
    assert audit.citation_accuracy == 0.5
    assert audit.unused_sources_share == pytest.approx(1 / 3, abs=1e-3)
    assert audit.one_sided is True and audit.options_without_objections == ["OPT-002"]
    assert "без опоры 50%" in audit.headline()


def test_number_mismatch_is_caught():
    from strategist.domain.services.citations import number_mismatches, numbers_in

    claims = {"CL-001": "Удержание M1 organic: 66.2%", "CL-002": "Удержание M2 organic: 49.2%"}
    assert numbers_in("рост на 3,5 п.п. и 199 ₽") == [3.5, 199.0]
    ok = "Удержание M1 у органики 66,2% [CL-001]."
    bad = "M1 внутри органики снизился с 66,2% до 49,2% [CL-001]."
    assert number_mismatches(ok, claims) == []
    assert len(number_mismatches(bad, claims)) == 1


def test_rounded_numbers_are_not_mismatches():
    from strategist.domain.services.citations import number_mismatches

    claims = {"CL-007": "Доля отключившихся: 2026-04: 11.9%; 2026-05: 13.4%"}
    assert number_mismatches("Доля ушедших по этой причине 10–13% [CL-007].", claims) == []
    assert number_mismatches("Около 12% ушедших [CL-007].", claims) == []
    assert len(number_mismatches("Доля ушедших 14% [CL-007].", claims)) == 1
    assert (
        len(number_mismatches("Доля ушедших 13,1% [CL-007].", claims)) == 1
    )  # знак после запятой — точность 0,05


def test_reference_ranges_are_expanded():
    from strategist.domain.services.citations import (
        expand_ranges,
        number_mismatches,
        referenced_ids,
        uncited_sentences,
    )

    assert referenced_ids("Новые данные [CL-075–CL-078]") == {"CL-075", "CL-076", "CL-077", "CL-078"}
    assert expand_ranges("CL-009-012") == "CL-009 CL-010 CL-011 CL-012"
    assert expand_ranges("[CL-010–CL-005]") == "[CL-010–CL-005]"  # обратный диапазон не трогаем
    claims = {"CL-077": "M1 от подключений: +3.5 п.п."}
    sentence = "A/B-тест [CL-075–CL-078] показал, что M1 от подключений выросла на +3,5 п.п."
    assert number_mismatches(sentence, claims) == []
    assert uncited_sentences(sentence) == []


def test_mean_of_rates_across_rows_of_different_size_is_flagged():
    from strategist.domain.services.simpson import is_rate_column, mean_of_rates_warning

    assert (
        is_rate_column("churn_at_renewal_pct")
        and is_rate_column("m3", "%")
        and not is_rate_column("revenue_rub")
    )
    # два сегмента в одной группе «тест»: (24,8 + 7,3) / 2 = 16,05 — не доля оттока теста
    assert mean_of_rates_warning("mean", "churn_at_renewal_pct", None, rows_used=4, groups=2, binary=False)
    # одна строка в группе — среднее равно значению строки
    assert (
        mean_of_rates_warning("mean", "churn_at_renewal_pct", None, rows_used=2, groups=2, binary=False)
        is None
    )
    # столбец 0/1 по пользователям — среднее и есть доля
    assert mean_of_rates_warning("mean", "churned", None, rows_used=900, groups=3, binary=True) is None
    assert mean_of_rates_warning("ratio", "renewed", None, rows_used=4, groups=2, binary=False) is None


def test_effects_recentered_when_base_level_moves():
    from strategist.domain.entities import DecisionMatrix, Uncertainty, UncertaintyLevel
    from strategist.domain.services.decision_analysis import recenter_effects, stress_test

    effects = {"ALL": {"U-001": {"high": -30.0}}, "NEW_ONLY": {"U-001": {"high": -5.0}}}
    moved = recenter_effects(effects, {"U-001": "high"})
    assert moved == {"ALL": {"U-001": {"high": 0.0}}, "NEW_ONLY": {"U-001": {"high": 0.0}}}
    # полезность уже с учётом наблюдаемого «high»: лидер базового сценария — по ней, без двойного вычета
    unc = Uncertainty(
        id="U-001",
        name="эластичность",
        levels=[
            UncertaintyLevel(name="low", probability=0.1),
            UncertaintyLevel(name="high", probability=0.9),
        ],
        base_level="high",
    )
    matrix = DecisionMatrix(
        uncertainties=[unc],
        base_utility={"ALL": 40.0, "NEW_ONLY": 62.0, "KEEP": 48.0},
        effects={**{k: {"U-001": {**v["U-001"], "low": 10.0}} for k, v in moved.items()}, "KEEP": {}},
    )
    assert stress_test(matrix).base_winner == "NEW_ONLY"
    assert recenter_effects(effects, {}) == effects


def test_churn_label_on_retention_ratio():
    from strategist.domain.services.simpson import churn_label_on_retention

    assert churn_label_on_retention("Доля оттока между M1 и M2", "ratio", "active_m2")
    assert churn_label_on_retention("churn (1 - доля продливших)", "ratio", "renewed")
    assert not churn_label_on_retention("Доля оттока", "ratio", "cancelled_autorenew_30d")
    assert not churn_label_on_retention("Удержание M2", "ratio", "active_m2")
    assert not churn_label_on_retention("Доля оттока", "share", "active_m2")


def test_clip_words_cuts_at_a_word_boundary_with_an_ellipsis():
    from strategist.domain.common import clip_words

    text = "А/B показывает рост конверсии [CL-073] [CL-076], поэтому возражение нужно скорректировать"
    assert clip_words(text, 200) == text
    clipped = clip_words(text, 60)
    assert len(clipped) <= 60 and clipped.endswith("…")
    assert clipped == "А/B показывает рост конверсии [CL-073] [CL-076], поэтому…"
    assert "[CL-07…" not in clip_words(text, 37)  # код не разрезается
