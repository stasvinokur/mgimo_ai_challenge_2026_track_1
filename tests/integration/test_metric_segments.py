"""Сравнение групп всегда разбивается по главному сегменту таблицы — на настоящем движке pandas + DuckDB."""

from __future__ import annotations

import re

from factories import add_source, empty_case
from fakes import make_context
from strategist.application.dto.llm_outputs import MetricProposalDTO
from strategist.application.use_cases.collect import compute_metric
from strategist.domain.entities import SourceKind
from strategist.infrastructure.tools.tables import PandasDuckdbTableEngine

RENEWALS = (
    "group,segment,price_rub,renewals_due,renewed\n"
    "контроль,текущие,399,36120,33953\n"
    "тест,текущие,479,35210,26478\n"
    "контроль,новые,399,1785,1660\n"
    "тест,новые,479,1697,1573\n"
)


def test_group_comparison_is_broken_down_by_segment_automatically(tmp_path):
    table = tmp_path / "renewals.csv"
    table.write_text(RENEWALS, encoding="utf-8")
    ctx = make_context(tables=PandasDuckdbTableEngine())
    case = empty_case()
    src = add_source(case, kind=SourceKind.TABLE, path=str(table))
    proposal = MetricProposalDTO(
        name="renewal_share",
        agg="ratio",
        column="renewed",
        denominator_column="renewals_due",
        group_by="group",
    )
    claim = compute_metric(ctx, case, src, proposal, [])
    # в целом: тест 76,0% против 94,0% — а внутри сегментов картина разная
    assert "тест: 76.0%" in claim.text
    assert "По «segment»:" in claim.text
    assert "текущие: контроль 94.0%, тест 75.2%" in claim.text
    assert "новые: контроль 93.0%, тест 92.7%" in claim.text


def test_self_ratio_is_rejected(tmp_path):
    table = tmp_path / "renewals.csv"
    table.write_text(RENEWALS, encoding="utf-8")
    ctx = make_context(tables=PandasDuckdbTableEngine())
    case = empty_case()
    src = add_source(case, kind=SourceKind.TABLE, path=str(table))
    errors: list[str] = []
    proposal = MetricProposalDTO(
        agg="ratio", column="renewals_due", denominator_column="renewals_due", group_by="group"
    )
    assert compute_metric(ctx, case, src, proposal, [], errors) is None
    assert "к самому себе" in errors[0]


def test_missing_segment_column_does_not_break_the_metric(tmp_path):
    table = tmp_path / "renewals.csv"
    table.write_text(RENEWALS, encoding="utf-8")
    ctx = make_context(tables=PandasDuckdbTableEngine())
    case = empty_case()
    src = add_source(case, kind=SourceKind.TABLE, path=str(table))
    errors: list[str] = []
    proposal = MetricProposalDTO(
        agg="ratio",
        column="renewed",
        denominator_column="renewals_due",
        group_by="group",
        segment_column="platform",
    )
    claim = compute_metric(ctx, case, src, proposal, [], errors)
    assert claim is not None and "тест: 76.0%" in claim.text
    assert "нет столбца сегмента «platform»" in errors[0]


def test_share_above_100_percent_is_flagged(tmp_path):
    table = tmp_path / "renewals.csv"
    table.write_text(RENEWALS, encoding="utf-8")
    ctx = make_context(tables=PandasDuckdbTableEngine())
    case = empty_case()
    src = add_source(case, kind=SourceKind.TABLE, path=str(table))
    proposal = MetricProposalDTO(
        name="churn_rate",
        description="Уровень оттока на продлении",
        agg="ratio",
        column="renewals_due",
        denominator_column="renewed",
        group_by="group",
    )
    claim = compute_metric(ctx, case, src, proposal, [])
    assert claim.vulnerable and "больше 100%" in claim.text


COHORTS = "channel,active_m1,active_m2\norganic,1000,841\npartner_bundle,2000,1498\n"


def test_churn_label_on_retention_ratio_gets_flipped_values(tmp_path):
    table = tmp_path / "cohorts.csv"
    table.write_text(COHORTS, encoding="utf-8")
    ctx = make_context(tables=PandasDuckdbTableEngine())
    case = empty_case()
    src = add_source(case, kind=SourceKind.TABLE, path=str(table))
    proposal = MetricProposalDTO(
        name="churn_m1_m2",
        description="Доля оттока между M1 и M2",
        agg="ratio",
        column="active_m2",
        denominator_column="active_m1",
        group_by="channel",
        segment_column="channel",
    )
    claim = compute_metric(ctx, case, src, proposal, [])
    assert "organic: 84.1%" in claim.text
    assert "отток = 1 − значение: organic 15.9%; partner_bundle 25.1%" in claim.text
    assert (
        claim.vulnerable and "По «channel»" not in claim.text
    )  # сегмент, совпадающий с группой, не разбивается


def test_plain_segment_breakdown_is_not_a_vulnerability(tmp_path):
    table = tmp_path / "renewals.csv"
    table.write_text(RENEWALS, encoding="utf-8")
    ctx = make_context(tables=PandasDuckdbTableEngine())
    case = empty_case()
    src = add_source(case, kind=SourceKind.TABLE, path=str(table))
    proposal = MetricProposalDTO(
        name="renewal_share",
        agg="ratio",
        column="renewed",
        denominator_column="renewals_due",
        group_by="group",
    )
    claim = compute_metric(ctx, case, src, proposal, [])
    assert "По «segment»:" in claim.text and not claim.vulnerable


# ----------------------------------------------------------------------------- контракт расчёта (TASK-60)

CONTRACT_ON = {"quality": {"metric_contract": True}}
# В духе брифа subscription-retention: ответы ушедших (строка — человек) и когорты (строка — когорта × канал)
EXIT_SURVEY = (
    "response_id,cancel_month,channel,months_subscribed,main_reason\n"
    "1,2026-07,organic,2,дорого\n2,2026-07,partner_bundle,1,нечего смотреть\n3,2026-08,organic,4,дорого\n"
    "4,2026-08,paid,3,нечего смотреть\n5,2026-09,organic,1,дорого\n6,2026-09,partner_bundle,5,другое\n"
)
COHORT_TABLE = (
    "cohort_month,channel,new_subscribers,active_m1,active_m2\n"
    "2026-01,organic,1000,620,510\n2026-01,partner_bundle,800,410,300\n"
    "2026-02,organic,1100,700,560\n2026-02,partner_bundle,900,450,320\n"
)


def _table(tmp_path, name, text, settings):
    table = tmp_path / name
    table.write_text(text, encoding="utf-8")
    ctx = make_context(tables=PandasDuckdbTableEngine(), settings=settings)
    case = empty_case()
    return ctx, case, add_source(case, kind=SourceKind.TABLE, path=str(table))


def test_months_column_reported_in_days_is_fixed_only_with_the_contract(tmp_path):
    proposal = MetricProposalDTO(
        name="tenure",
        description="Средний срок подписки до отмены",
        agg="mean",
        column="months_subscribed",
        group_by="main_reason",
        unit="дни",
    )
    ctx, case, src = _table(tmp_path, "exit.csv", EXIT_SURVEY, CONTRACT_ON)
    claim = compute_metric(ctx, case, src, proposal, [])
    assert "дорого: 2.33 мес;" in claim.text and not re.search(r"\bдни\b", claim.text.split("Исправлено")[0])
    assert ".." not in claim.text
    assert "в столбце «months_subscribed» месяцы, а не «дни»" in claim.text
    assert claim.computation.contract == ["unit_time"] and claim.computation.rows == 6
    assert (
        claim.computation.period == "2026-07–2026-09" and "2026-07–2026-09" not in claim.text
    )  # не в тексте
    ctx, case, src = _table(tmp_path, "exit.csv", EXIT_SURVEY, {})
    old = compute_metric(ctx, case, src, proposal, [])
    assert "дорого: 2.33 дни" in old.text and old.computation.period is None and old.computation.rows is None


def test_churn_label_over_retention_is_relabelled_with_the_contract(tmp_path):
    proposal = MetricProposalDTO(
        name="churn_m1_m2",
        description="Доля оттока между M1 и M2",
        agg="ratio",
        column="active_m2",
        denominator_column="active_m1",
        group_by="channel",
        segment_column="channel",
    )
    ctx, case, src = _table(tmp_path, "cohorts.csv", COHORTS, CONTRACT_ON)
    claim = compute_metric(ctx, case, src, proposal, [])
    assert claim.text.startswith("Доля оставшихся: active_m2 / active_m1 [расчёт:")
    assert "organic: 84.1%" in claim.text
    assert "подпись «Доля оттока между M1 и M2» исправлена" in claim.text
    assert "отток = 1 − значение: organic 15.9%; partner_bundle 25.1%" in claim.text
    assert claim.text.count("отток = 1 − значение") == 1  # прежнее предупреждение не дублируется
    assert claim.computation.contract == ["churn_label"]


def test_row_share_of_an_aggregated_table_stays_in_the_registry_but_not_in_the_report(tmp_path):
    from strategist.application.digests import claims_digest
    from strategist.domain.services.claim_linter import reportable

    proposal = MetricProposalDTO(
        name="channel_share", description="Доля канала по когортам", agg="share", group_by="cohort_month"
    )
    ctx, case, src = _table(tmp_path, "cohorts.csv", COHORT_TABLE, CONTRACT_ON)
    claim = compute_metric(ctx, case, src, proposal, [])
    case.claims.append(claim)
    assert claim.computation.blocked and claim.vulnerable
    assert "доля строк агрегированной таблицы" in claim.text
    assert not reportable(claim, case)
    assert claim.id not in claims_digest(case)  # модели не видят эту строку как опору
    ctx, case, src = _table(tmp_path, "exit.csv", EXIT_SURVEY, CONTRACT_ON)
    person_share = compute_metric(
        ctx, case, src, MetricProposalDTO(name="by_reason", agg="share", group_by="main_reason"), []
    )
    assert not person_share.computation.blocked  # строка — человек: доля строк = доля ответивших
