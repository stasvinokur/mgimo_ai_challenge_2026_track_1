"""Контракт расчёта: смысл числа проверяется на шаге расчёта — единица, подпись, доля строк, период."""

from __future__ import annotations

import pytest

from strategist.domain.services.metric_contract import (
    IssueKind,
    column_time_unit,
    contract_issues,
    is_aggregated_table,
    looks_like_id,
    period_of,
    relabel_retention,
    unit_time_kind,
)


@pytest.mark.parametrize(
    ("column", "kind"),
    [
        ("months_subscribed", "month"),
        ("paid_months_before_cancel", "month"),
        ("location_days_last_30", "day"),
        ("tenure_weeks", "week"),
        ("Недель в пилоте", "week"),
        ("Срок подписки, мес.", "month"),
        ("orders_per_month_after", None),  # частота, а не длительность
        ("Заявок на активного сотрудника в неделю", None),
        ("Готовность платить, ₽ за сотрудника в месяц (со слов)", None),
        ("Доля активных в неделю, %", None),
        ("price_rub", None),
        (None, None),
    ],
)
def test_column_time_unit_tells_durations_from_rates(column, kind):
    assert column_time_unit(column) == kind


@pytest.mark.parametrize(
    ("unit", "kind"),
    [
        ("дни", "day"),
        ("дн.", "day"),
        ("месяцы", "month"),
        ("мес", "month"),
        ("weeks", "week"),
        ("человек", None),
    ],
)
def test_unit_time_kind(unit, kind):
    assert unit_time_kind(unit) == kind


def _issues(**kw):
    base = {
        "label": "metric",
        "agg": "mean",
        "column": None,
        "denominator_column": None,
        "unit": None,
        "values": [],
    }
    return contract_issues(**{**base, **kw})


def test_months_column_reported_in_days_gets_its_unit_back():
    (issue,) = _issues(column="months_subscribed", unit="дни", values=[2.6, 2.73])
    assert issue.kind == IssueKind.UNIT_TIME and issue.unit == "мес"
    assert "months_subscribed" in issue.message and "«дни»" in issue.message


def test_consistent_or_non_time_units_are_left_alone():
    assert _issues(column="paid_months_before_cancel", unit="месяцы", values=[3.1]) == []
    assert _issues(column="orders_per_month_after", unit="заказы", values=[4.2]) == []
    assert (
        _issues(column="months_subscribed", unit="₽", values=[2.6]) == []
    )  # не единица времени — не трогаем
    assert _issues(column="months_subscribed", agg="count", unit="дни", values=[260]) == []  # число строк


def test_missing_time_unit_is_added_from_the_column():
    (issue,) = _issues(column="months_subscribed", unit=None, values=[2.6])
    assert issue.unit == "мес" and "единица по столбцу" in issue.message


def test_churn_label_over_retention_is_relabelled():
    (issue,) = _issues(
        label="churn_m1_m2 Доля оттока между M1 и M2",
        agg="ratio",
        column="active_m2",
        denominator_column="active_m1",
    )
    assert issue.kind == IssueKind.CHURN_LABEL
    assert (
        issue.label == relabel_retention("active_m2", "active_m1") == "Доля оставшихся: active_m2 / active_m1"
    )
    assert (
        _issues(label="Удержание M2", agg="ratio", column="active_m2", denominator_column="active_m1") == []
    )


def test_share_and_percent_mixups():
    (issue,) = _issues(agg="ratio", unit="%", values=[0.841, 0.749])
    assert issue.kind == IssueKind.SHARE_VS_PERCENT and issue.unit == "доля"
    (issue,) = _issues(agg="mean", unit="доля", values=[62.5, 48.0])
    assert issue.unit == "%"
    assert _issues(agg="ratio", unit="доля", values=[0.84]) == []
    assert _issues(agg="ratio", unit="%", values=[0.84, 1.3]) == []  # не все значения — доли


def test_row_share_of_an_aggregated_table_is_blocked():
    (issue,) = _issues(agg="share", aggregated_table=True, values=[0.083])
    assert issue.kind == IssueKind.ROW_SHARE_AGGREGATED
    assert _issues(agg="share", aggregated_table=False, values=[0.34]) == []


def test_aggregated_table_detection():
    cohorts = ["cohort_month", "channel", "new_subscribers", "active_m1", "active_m2"]
    types = {"cohort_month": "object", "channel": "object", "new_subscribers": "int64", "active_m1": "int64"}
    assert is_aggregated_table(cohorts, types, id_columns=set())
    monthly = [
        "month",
        "mau",
        "list_users",
        "orders_total",
    ]  # строка — месяц: уникальная подпись, не идентификатор
    assert is_aggregated_table(monthly, {"mau": "int64", "list_users": "int64"}, id_columns=set())
    survey = ["respondent_id", "household_type", "aware_of_pack"]
    assert not is_aggregated_table(survey, {"respondent_id": "int64"}, set())  # строка — человек
    clients = ["Клиент", "Сегмент", "Сотрудников в поле"]
    assert not is_aggregated_table(clients, {"Сотрудников в поле": "int64"}, {"Клиент"})  # строка — клиент
    assert not is_aggregated_table(
        ["group", "segment", "price_rub"], {"price_rub": "int64"}, set()
    )  # нет счётчика


def test_ids_are_codes_not_group_labels():
    assert looks_like_id(["T-0015", "T-0016", "R-0001", "12345"])
    assert not looks_like_id(["2025-11", "2025-12"])
    assert not looks_like_id(["organic", "partner_bundle"])
    assert not looks_like_id(["пользователи списка (начали пользоваться в январе–марте 2026)"])


def test_period_prefers_a_period_filter_over_the_table_period():
    assert period_of([("cohort_month", "==", "2026-03")], "2026-01–2026-09") == "cohort_month = 2026-03"
    assert period_of([("channel", "==", "organic")], "2026-01–2026-09") == "2026-01–2026-09"
    assert period_of([("cohort_month", ">=", "2026-03")], None) is None
