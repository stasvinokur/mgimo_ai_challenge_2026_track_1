"""Оставшиеся правила линтера утверждений."""

from factories import NOW, add_claim, add_evidence, add_option, add_source, empty_case
from strategist.domain.common import Severity
from strategist.domain.entities import ClaimType, Computation, Evidence, OptionKind, SupportLabel
from strategist.domain.services.claim_linter import lint_case_claims, lint_claim


def codes(issues):
    return {i.code for i in issues}


def test_missing_evidence_and_missing_source():
    case = empty_case()
    claim = add_claim(case, type=ClaimType.FACT)
    claim.evidence_ids = ["EV-404"]
    assert "missing_evidence" in codes(lint_claim(claim, case))
    case.evidence.append(Evidence(id="EV-404", source_id="SRC-404", quote="цитата", extracted_at=NOW))
    assert "missing_source" in codes(lint_claim(claim, case))


def test_retracted_source_is_warning():
    case = empty_case()
    src = add_source(case, retracted=True)
    ev = add_evidence(case, src)
    claim = add_claim(case, evidence=[ev])
    issues = [i for i in lint_claim(claim, case) if i.code == "invalid_source"]
    assert issues and issues[0].severity == Severity.WARNING


def test_demand_metric_on_synthetic_is_rejected():
    case = empty_case()
    src = add_source(case, synthetic=True)
    ev = add_evidence(case, src)
    claim = add_claim(
        case,
        "Доля готовых платить — 40%",
        ClaimType.DERIVED_METRIC,
        [ev],
        computation=Computation(metric="share", value=0.4, check_value=0.4),
    )
    assert "synthetic_demand_metric" in codes(lint_claim(claim, case))


def test_assumption_without_signal_is_warning():
    case = empty_case()
    claim = add_claim(case, "Конкурент не снизит цену", ClaimType.ASSUMPTION)
    issue = next(i for i in lint_claim(claim, case) if i.code == "assumption_without_signal")
    assert issue.severity == Severity.WARNING


def test_synthetic_demand_claim_cannot_be_supported():
    case = empty_case()
    claim = add_claim(case, "70% персон купили бы", ClaimType.SYNTHETIC, support=SupportLabel.SUPPORTED)
    assert "synthetic_marked_supported" in codes(lint_claim(claim, case))


def test_dangling_reference_in_option():
    case = empty_case()
    add_option(case, OptionKind.STATUS_QUO, supporting_claim_ids=["CL-999"])
    assert "dangling_claim_ref" in codes(lint_case_claims(case))


def test_simulated_data_fact_is_not_blocked_but_model_respondents_are():
    from factories import grounded_fact

    case = empty_case()
    simulated = grounded_fact(case, "Когорта сентября продлевает в 48% случаев")
    case.source(case.evidence_item(simulated.evidence_ids[0]).source_id).synthetic = True
    case.source(case.evidence_item(simulated.evidence_ids[0]).source_id).synthetic_kind = "simulated_data"
    assert "synthetic_as_fact" not in codes(lint_claim(simulated, case))
    personas = grounded_fact(case, "Персонам нравится тариф", synthetic=True)
    assert "synthetic_as_fact" in codes(lint_claim(personas, case))
