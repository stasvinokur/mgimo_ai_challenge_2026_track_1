"""Подложенные ошибки в расчётах: контракт расчёта (TASK-60) на настоящих таблицах брифов dev.

    uv run python evals/injected/calc.py build            # → evals/injected/calc/dev.json
    uv run python evals/injected/calc.py score            # контракт v0.2.0 против v0.1.1

Ошибки смысла рождаются на шаге расчёта, поэтому подкладываются в спецификацию расчёта, а не в текст:
- calc_unit_time — среднее по столбцу-длительности в чужой единице (months_subscribed в «днях»);
- calc_churn_label — «доля оттока» над отношением оставшихся (active_m2 / active_m1);
- calc_row_share — «доля строк» по таблице, где строка — группа (когорта × канал, месяц);
- calc_share_percent — доли 0–1 с единицей «%» или проценты с единицей «доля».
Безобидные двойники: та же спецификация с верной единицей или подписью, частота вместо длительности
(orders_per_month), «доля строк» по таблице, где строка — человек.

Правда о данных (какой столбец — длительность, какая таблица агрегирована, что в числителе) размечена вручную по
таблицам dev ниже, а не выведена проверяемым правилом. Holdout размечается после заморозки правил (17.11).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BRIEFS = ROOT / "evals" / "briefs"
OUT = HERE / "calc" / "dev.json"
RESULTS = ROOT / "evals" / "results" / "injected"

# Ручная разметка таблиц dev: строка — группа (True) или человек/объект (False)
AGGREGATED = {
    ("family-pack", "support_tickets.csv"): False,  # строка — обращение
    ("family-pack", "survey_family.csv"): False,  # строка — респондент
    ("feature-sunset", "app_reviews_list.csv"): False,  # строка — отзыв
    ("feature-sunset", "feature_usage_monthly.csv"): True,  # строка — месяц со счётчиками
    ("feature-sunset", "retention_by_list_usage.csv"): True,  # строка — группа пользователей
    ("price-increase-a", "price_test_m1.csv"): True,  # строка — группа × сегмент
    ("subscription-retention", "cohorts_by_channel.csv"): True,  # строка — когорта × канал
    ("subscription-retention", "exit_survey.csv"): False,  # строка — ответ ушедшего
}
# Столбцы-длительности и их единица
DURATIONS = {("subscription-retention", "exit_survey.csv", "months_subscribed"): "month"}
# Частоты (не длительности) — безобидные двойники единицы времени
RATES = {
    ("feature-sunset", "retention_by_list_usage.csv"): ["orders_per_month_before", "orders_per_month_after"]
}
# Отношения «оставшиеся / база»: числитель — оставшиеся
RETAINED = {
    ("subscription-retention", "cohorts_by_channel.csv"): [
        ("active_m1", "new_subscribers"),
        ("active_m2", "active_m1"),
        ("active_m3", "active_m2"),
        ("active_m3", "new_subscribers"),
    ],
    ("price-increase-a", "price_test_m1.csv"): [
        ("paid_after_trial", "users"),
        ("renewals_at_new_price", "users"),
    ],
}
# Отношения, где «отток» — верная подпись (в числителе ушедшие)
CHURNED = {("price-increase-a", "price_test_m1.csv"): [("cancelled_autorenew_30d", "users")]}
# Столбцы, где уже проценты
PERCENT_COLUMNS = {("feature-sunset", "retention_by_list_usage.csv"): ["retention_m3_pct"]}
# Отложенная выборка (holdout): размечена вручную по таблицам 09.11.2026 — после заморозки правил контракта
# (последняя правка правил — коммит 1693bc3, заморозка на f3fa63b). Строки всех таблиц — группы со счётчиками.
HOLDOUT = {
    "AGGREGATED": {
        ("app-onboarding", "callcenter_reasons.csv"): True,  # строка — месяц × причина
        ("app-onboarding", "onboarding_funnel.csv"): True,  # строка — неделя × платформа
        ("b2b-field-service", "sales_funnel.csv"): True,  # строка — канал × квартал
        ("price-increase-b", "price_test_m1.csv"): True,  # строка — группа × сегмент
        ("price-increase-b", "price_test_m2_renewals.csv"): True,  # строка — группа × сегмент
    },
    "DURATIONS": {},
    "RATES": {},
    "RETAINED": {
        ("price-increase-b", "price_test_m1.csv"): [
            ("paid_after_trial", "users"),
            ("renewals_at_new_price", "users"),
        ],
        ("price-increase-b", "price_test_m2_renewals.csv"): [("renewed", "renewals_due")],
    },
    "CHURNED": {("price-increase-b", "price_test_m1.csv"): [("cancelled_autorenew_30d", "users")]},
    "PERCENT_COLUMNS": {("price-increase-b", "price_test_m2_renewals.csv"): ["churn_at_renewal_pct"]},
}
WRONG_TIME = {"month": ["дни", "недели", "часы"], "day": ["месяцы", "недели"], "week": ["дни", "месяцы"]}
RIGHT_TIME = {"month": ["месяцы", "мес"], "day": ["дни"], "week": ["недели"]}


def _engine() -> Any:
    from strategist.infrastructure.tools.tables import PandasDuckdbTableEngine

    return PandasDuckdbTableEngine()


def _values(engine: Any, path: Path, spec: dict[str, Any]) -> list[float]:
    from strategist.application.ports import MetricSpec

    result = engine.compute(str(path), MetricSpec(**spec))
    return [v for v in (result.value, *result.by_group.values()) if v is not None]


def _facts(engine: Any, path: Path) -> dict[str, Any]:
    """Факты о таблице для правила агрегата: столбцы, типы и значения уникальных текстовых столбцов."""
    p = engine.passport(str(path))
    unique = {}
    for c in p.columns:
        dtype = p.dtypes.get(c, "object")
        if dtype.startswith(("object", "str")) or dtype == "category":
            values = engine.distinct_values(str(path), c, limit=p.rows + 1)
            if len(values) >= p.rows:
                unique[c] = [str(v) for v in values]
    return {"columns": p.columns, "dtypes": p.dtypes, "unique_text": unique}


def build(split: str = "dev") -> dict[str, Any]:
    engine = _engine()
    labels = (
        HOLDOUT
        if split == "holdout"
        else {
            "AGGREGATED": AGGREGATED,
            "DURATIONS": DURATIONS,
            "RATES": RATES,
            "RETAINED": RETAINED,
            "CHURNED": CHURNED,
            "PERCENT_COLUMNS": PERCENT_COLUMNS,
        }
    )
    items: list[dict[str, Any]] = []

    def add(kind: str, expect: str | None, brief: str, table: str, spec: dict[str, Any], facts: dict) -> None:
        path = BRIEFS / brief / "materials" / table
        spec = {"name": spec.get("name", kind), **spec}
        items.append(
            {
                "id": f"{brief}-{kind}-{sum(it['kind'] == kind and it['brief'] == brief for it in items) + 1:02d}",
                "brief": brief,
                "table": table,
                "kind": kind,
                "expect": expect,
                "spec": spec,
                "values": _values(engine, path, spec),
                "facts": facts,
            }
        )

    for (brief, table), aggregated in labels["AGGREGATED"].items():
        path = BRIEFS / brief / "materials" / table
        facts = _facts(engine, path)
        text_cols = [c for c in facts["columns"] if facts["dtypes"].get(c, "").startswith(("object", "str"))]
        groups = [c for c in text_cols if c not in facts["unique_text"] or aggregated][:3]
        for col in groups:  # «доля строк» по группам: ошибка, только если строка — группа
            add(
                "calc_row_share" if aggregated else "calc_row_share_person",
                "row_share_aggregated" if aggregated else None,
                brief,
                table,
                {"agg": "share", "group_by": col, "description": f"Доля по «{col}»"},
                facts,
            )
        for (b, t, column), unit_kind in labels["DURATIONS"].items():
            if (b, t) != (brief, table):
                continue
            for agg in ("mean", "median", "max"):
                for unit in WRONG_TIME[unit_kind]:
                    add(
                        "calc_unit_time",
                        "unit_time",
                        brief,
                        table,
                        {"agg": agg, "column": column, "unit": unit},
                        facts,
                    )
                for unit in RIGHT_TIME[unit_kind]:
                    add(
                        "calc_unit_time_ok",
                        None,
                        brief,
                        table,
                        {"agg": agg, "column": column, "unit": unit},
                        facts,
                    )
        for column in labels["RATES"].get((brief, table), []):
            add(
                "calc_rate_ok", None, brief, table, {"agg": "mean", "column": column, "unit": "заказы"}, facts
            )
        for num, den in labels["RETAINED"].get((brief, table), []):
            base = {"agg": "ratio", "column": num, "denominator_column": den}
            add(
                "calc_churn_label", "churn_label", brief, table, {**base, "description": "Доля оттока"}, facts
            )
            add("calc_retention_ok", None, brief, table, {**base, "description": "Удержание"}, facts)
            add("calc_share_percent", "share_vs_percent", brief, table, {**base, "unit": "%"}, facts)
            add("calc_share_ok", None, brief, table, {**base, "unit": "доля"}, facts)
        for num, den in labels["CHURNED"].get((brief, table), []):
            spec = {"agg": "ratio", "column": num, "denominator_column": den, "description": "Доля оттока"}
            add("calc_churn_ok", None, brief, table, spec, facts)
        for column in labels["PERCENT_COLUMNS"].get((brief, table), []):
            add(
                "calc_share_percent",
                "share_vs_percent",
                brief,
                table,
                {"agg": "mean", "column": column, "unit": "доля"},
                facts,
            )
            add("calc_share_ok", None, brief, table, {"agg": "mean", "column": column, "unit": "%"}, facts)
    data = {"split": split, "items": items}
    out = OUT.with_name(f"{split}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return data


def fired_contract(item: dict[str, Any]) -> set[str]:
    """Контракт v0.2.0: какие правила сработали на этой спецификации."""
    from strategist.domain.services.metric_contract import (
        contract_issues,
        is_aggregated_table,
        looks_like_id,
    )

    s, f = item["spec"], item["facts"]
    ids = {c for c, values in f["unique_text"].items() if looks_like_id(values)}
    issues = contract_issues(
        label=f"{s.get('name', '')} {s.get('description', '')}",
        agg=s["agg"],
        column=s.get("column"),
        denominator_column=s.get("denominator_column"),
        unit=s.get("unit"),
        values=item["values"],
        aggregated_table=s["agg"] == "share" and is_aggregated_table(f["columns"], f["dtypes"], ids),
    )
    return {i.kind.value for i in issues}


def fired_v011(item: dict[str, Any]) -> set[str]:
    """v0.1.1 ничего не исправлял; «отток» над оставшимися только помечался предупреждением."""
    from strategist.domain.services.metric_contract import churn_label_on_retention

    s = item["spec"]
    label = f"{s.get('name', '')} {s.get('description', '')}"
    return (
        {"churn_label (только предупреждение)"}
        if churn_label_on_retention(label, s["agg"], s.get("column"))
        else set()
    )


def score(data: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, dict[str, int]] = {}
    misses = []
    for it in data["items"]:
        new, old = fired_contract(it), fired_v011(it)
        r = rows.setdefault(it["kind"], {"n": 0, "contract": 0, "v011": 0, "expect": it["expect"] or "—"})
        r["n"] += 1
        ok_new = (it["expect"] in new) if it["expect"] else not new
        r["contract"] += ok_new
        r["v011"] += (it["expect"] is None and not old) or (it["expect"] == "churn_label" and bool(old))
        if not ok_new:
            misses.append({"id": it["id"], "expect": it["expect"], "fired": sorted(new), "spec": it["spec"]})
    return {"rows": rows, "misses": misses}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["build", "score"])
    parser.add_argument(
        "--split", choices=["dev", "holdout"], default="dev", help="holdout — только после заморозки правил"
    )
    args = parser.parse_args(argv)
    out = OUT.with_name(f"{args.split}.json")
    if args.action == "build":
        data = build(args.split)
        print(f"{out.relative_to(ROOT)}: {len(data['items'])} спецификаций")
        return 0
    data = json.loads(out.read_text(encoding="utf-8"))
    result = score(data)
    print("| Вид | Ожидается | n | Контракт v0.2.0 верно | v0.1.1 верно |\n|---|---|---|---|---|")
    for kind, r in result["rows"].items():
        print(f"| {kind} | {r['expect']} | {r['n']} | {r['contract']} | {r['v011']} |")
    mutants = [r for r in result["rows"].values() if r["expect"] != "—"]
    benign = [r for r in result["rows"].values() if r["expect"] == "—"]
    print(
        f"\nОшибки исправлены контрактом: {sum(r['contract'] for r in mutants)} из {sum(r['n'] for r in mutants)}; "
        f"безобидные без срабатываний: {sum(r['contract'] for r in benign)} из {sum(r['n'] for r in benign)}. "
        f"v0.1.1 не исправлял ничего; предупреждал об «оттоке» над оставшимися: "
        f"{sum(r['v011'] for k, r in result['rows'].items() if k == 'calc_churn_label')}."
    )
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"calc-{args.split}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
