from datetime import date

from factories import add_claim, add_evidence, add_option, add_source, empty_case, grounded_fact
from strategist.domain.entities import ClaimType, Computation, OptionKind, SupportLabel
from strategist.domain.services.claim_linter import lint_case_claims, lint_claim, mentions_demand, reportable


def codes(issues):
    return {i.code for i in issues}


def test_fact_without_source_is_rejected():
    case = empty_case()
    claim = add_claim(case, type=ClaimType.FACT)
    assert "fact_without_source" in codes(lint_claim(claim, case))
    assert not reportable(claim, case)


def test_fact_with_quote_and_source_is_reportable():
    case = empty_case()
    claim = grounded_fact(case)
    assert lint_claim(claim, case) == []
    assert reportable(claim, case)


def test_fact_on_synthetic_only_is_rejected():
    case = empty_case()
    claim = grounded_fact(case, synthetic=True)
    assert "synthetic_as_fact" in codes(lint_claim(claim, case))


def test_forecast_requires_probability_horizon_and_resolution():
    case = empty_case()
    claim = add_claim(case, "Удержание вырастет", ClaimType.FORECAST)
    assert {
        "forecast_without_probability",
        "forecast_without_horizon",
        "forecast_without_resolution",
    } <= codes(lint_claim(claim, case))
    ok = add_claim(
        case,
        "Удержание M3 вырастет до 38%",
        ClaimType.FORECAST,
        probability=0.6,
        horizon=date(2027, 3, 1),
        resolution_criterion="M3 когорты февраля ≥ 38%",
    )
    assert lint_claim(ok, case) == []


def test_hypothesis_requires_falsification_test():
    case = empty_case()
    claim = add_claim(case, "Причина — слабый онбординг", ClaimType.HYPOTHESIS)
    assert "hypothesis_without_falsification" in codes(lint_claim(claim, case))
    claim.falsification_test = "Если когорты с новым онбордингом не лучше на 3 п.п. — гипотеза отвергнута"
    assert lint_claim(claim, case) == []


def test_derived_metric_requires_double_computation():
    case = empty_case()
    claim = add_claim(case, "Средний M3 = 0.33", ClaimType.DERIVED_METRIC)
    assert "metric_without_computation" in codes(lint_claim(claim, case))
    claim.computation = Computation(metric="m3", value=0.33, check_value=None)
    assert "metric_not_double_checked" in codes(lint_claim(claim, case))
    claim.computation = Computation(metric="m3", value=0.33, check_value=0.34)
    assert "metric_mismatch" in codes(lint_claim(claim, case))
    claim.computation = Computation(metric="m3", value=0.33, check_value=0.33)
    assert lint_claim(claim, case) == []


def test_unknown_requires_reason():
    case = empty_case()
    claim = add_claim(case, "Неизвестна готовность платить", ClaimType.UNKNOWN)
    assert "unknown_without_reason" in codes(lint_claim(claim, case))


def test_synthetic_cannot_prove_demand_in_option():
    case = empty_case()
    src = add_source(case, synthetic=True)
    ev = add_evidence(case, src, "68% персон готовы платить 299 ₽")
    synthetic = add_claim(
        case, "68% готовы платить 299 ₽", ClaimType.SYNTHETIC, [ev], support=SupportLabel.UNVERIFIED
    )
    add_option(case, OptionKind.INVEST, supporting_claim_ids=[synthetic.id])
    assert "synthetic_as_demand_proof" in codes(lint_case_claims(case))


def test_demand_pattern():
    assert mentions_demand("доля пользователей, готовых платить")
    assert not mentions_demand("Онбординг состоит из пяти шагов")
