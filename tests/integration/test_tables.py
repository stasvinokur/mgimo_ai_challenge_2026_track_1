"""PandasDuckdbTableEngine на временных CSV/XLSX: паспорт, агрегации, фильтры, группы, согласие движков."""

from __future__ import annotations

import statistics
from datetime import datetime

import pytest

from strategist.application.ports import MetricFilter, MetricSpec
from strategist.infrastructure.tools.tables import PandasDuckdbTableEngine, column_letter, row_ranges

CSV = """month,region,segment,users,active_users,retention_m3,revenue_rub,arpu_pct
2026-01,Москва,b2c,100,40,0.41,1000.5,12.5
2026-01,Казань,b2b,80,30,0.38,2000,
2026-02,Москва,b2c,120,,0.45,1500,13
2026-02,Казань,b2c,90,35,,1100,11.5
2026-03,Москва,b2b,130,60,0.52,2500,14
2026-03,Казань,b2b,70,20,0.33,900,10
2026-04,Москва,b2c,,50,0.47,1700,12
2026-04,Казань,b2c,95,38,0.36,1200,11
"""

USERS = [100, 80, 120, 90, 130, 70, 95]
RETENTION = [0.41, 0.38, 0.45, 0.52, 0.33, 0.47, 0.36]


@pytest.fixture
def engine():
    return PandasDuckdbTableEngine()


@pytest.fixture
def csv_path(tmp_path):
    path = tmp_path / "cohorts.csv"
    path.write_text(CSV, encoding="utf-8")
    (tmp_path / "cohorts.csv.meta.yaml").write_text(
        "title: Когорты\nunits:\n  users: абоненты\n", encoding="utf-8"
    )
    return str(path)


def spec(agg, column=None, **kw):
    return MetricSpec(name=f"{agg}_{column}", agg=agg, column=column, **kw)


# --- Паспорт -------------------------------------------------------------------------------------


def test_csv_passport(engine, csv_path):
    p = engine.passport(csv_path)
    assert p.rows == 8 and p.sheet is None
    assert p.columns[:3] == ["month", "region", "segment"]
    assert p.dtypes["users"] == "float64" and p.dtypes["region"] == "str"
    assert p.units == {
        "users": "абоненты",  # из <файл>.meta.yaml — важнее эвристики
        "active_users": "пользователи",
        "retention_m3": "доля",
        "revenue_rub": "₽",
        "arpu_pct": "%",  # значения > 1 — это проценты, а не доля
    }
    assert p.period == "2026-01–2026-04"
    assert p.missing_share["users"] == pytest.approx(1 / 8, abs=1e-4)
    assert p.missing_share["region"] == 0.0
    preview = p.preview.splitlines()
    assert len(preview) == 7  # заголовок, разделитель и 5 строк
    assert preview[0].startswith("| month | region |")
    assert preview[3] == "| 2026-01 | Казань | b2b | 80 | 30 | 0.38 | 2000 |  |"


def test_period_from_meta_and_dates(engine, tmp_path):
    path = tmp_path / "daily.csv"
    path.write_text("дата,value\n05.01.2026,1\n31.03.2026,2\n15.02.2026,3\n", encoding="utf-8")
    assert engine.passport(str(path)).period == "2026-01-05–2026-03-31"
    (tmp_path / "daily.csv.meta.yaml").write_text("period: I квартал 2026\n", encoding="utf-8")
    assert engine.passport(str(path)).period == "I квартал 2026"


# --- Агрегации -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agg", "expected"),
    [
        ("mean", statistics.mean(USERS)),
        ("sum", sum(USERS)),
        ("count", len(USERS)),
        ("median", statistics.median(USERS)),
        ("min", min(USERS)),
        ("max", max(USERS)),
    ],
)
def test_each_aggregation_both_engines(engine, csv_path, agg, expected):
    result = engine.compute(csv_path, spec(agg, "users"))
    assert result.value == pytest.approx(expected)
    assert result.check_value == pytest.approx(expected)
    assert result.consistent
    assert result.primary_engine == "pandas" and result.check_engine == "duckdb"
    assert result.rows_used == 7  # пропуск в users не участвует
    assert result.cells == ["столбец users (D), строки 2–7, 9"]


def test_median_of_even_count_is_interpolated(engine, csv_path):
    result = engine.compute(csv_path, spec("median", "retention_m3"))
    assert result.value == pytest.approx(statistics.median(RETENTION))
    assert result.consistent


def test_count_rows_without_column(engine, csv_path):
    result = engine.compute(csv_path, spec("count"))
    assert result.value == 8 and result.check_value == 8 and result.consistent
    assert result.cells == ["строки 2–9 из 2–9"]


def test_ratio(engine, csv_path):
    result = engine.compute(csv_path, spec("ratio", "active_users", denominator_column="users"))
    # Только строки, где есть оба значения (строки с пропуском в числителе или знаменателе исключаются).
    expected = (40 + 30 + 35 + 60 + 20 + 38) / (100 + 80 + 90 + 130 + 70 + 95)
    assert result.value == pytest.approx(expected)
    assert result.consistent
    assert result.rows_used == 6


def test_share_of_rows_passing_filters(engine, csv_path):
    result = engine.compute(
        csv_path, spec("share", filters=[MetricFilter(column="segment", op="==", value="b2b")])
    )
    assert result.value == pytest.approx(3 / 8)
    assert result.check_value == pytest.approx(3 / 8)
    assert result.consistent
    assert result.rows_used == 3
    assert result.cells[0] == "строки 3, 6–7 из 2–9"
    assert result.cells[1] == "фильтр: segment (C) == 'b2b'"


def test_share_by_group_is_within_group(engine, csv_path):
    result = engine.compute(
        csv_path,
        spec("share", filters=[MetricFilter(column="segment", op="==", value="b2c")], group_by="region"),
    )
    assert result.value == pytest.approx(5 / 8)
    assert result.by_group == pytest.approx({"Казань": 2 / 4, "Москва": 3 / 4})
    assert result.check_by_group == pytest.approx(result.by_group)
    assert result.consistent


# --- Фильтры -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "value", "rows"),
    [
        ("==", "Москва", 4),
        ("!=", "Москва", 4),
        (">", 95, 3),
        (">=", "95", 4),  # строка приводится к числу для числового столбца
        ("<", 90.0, 2),
        ("<=", 90, 3),
        ("in", ["2026-01", "2026-03"], 4),
        ("in", "2026-02", 2),  # одиночное значение для in — как список из одного
    ],
)
def test_filters(engine, csv_path, op, value, rows):
    column = {"==": "region", "!=": "region", "in": "month"}.get(op, "users")
    result = engine.compute(
        csv_path, spec("count", filters=[MetricFilter(column=column, op=op, value=value)])
    )
    assert result.value == rows
    assert result.check_value == rows
    assert result.consistent


def test_missing_values_never_pass_comparison(engine, csv_path):
    # в SQL сравнение с NULL ложно; pandas должен вести себя так же, иначе движки разойдутся
    result = engine.compute(
        csv_path, spec("count", filters=[MetricFilter(column="users", op="!=", value=100)])
    )
    assert result.value == 6 and result.consistent


def test_filters_and_mean(engine, csv_path):
    result = engine.compute(
        csv_path,
        spec(
            "mean",
            "retention_m3",
            filters=[
                MetricFilter(column="region", op="==", value="Москва"),
                MetricFilter(column="segment", op="==", value="b2c"),
            ],
        ),
    )
    assert result.value == pytest.approx(statistics.mean([0.41, 0.45, 0.47]))
    assert result.consistent
    assert result.cells[0] == "столбец retention_m3 (F), строки 2, 4, 8"


def test_bad_filter_value_for_numeric_column(engine, csv_path):
    with pytest.raises(ValueError, match="users"):
        engine.compute(csv_path, spec("count", filters=[MetricFilter(column="users", op=">", value="много")]))


# --- Группы --------------------------------------------------------------------------------------


def test_group_by(engine, csv_path):
    result = engine.compute(csv_path, spec("mean", "retention_m3", group_by="month"))
    assert result.value == pytest.approx(statistics.mean(RETENTION))  # по всем строкам, без группировки
    assert list(result.by_group) == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert result.by_group["2026-01"] == pytest.approx((0.41 + 0.38) / 2)
    assert result.by_group["2026-02"] == pytest.approx(0.45)  # пропуск не участвует
    assert result.check_by_group == pytest.approx(result.by_group)
    assert result.consistent
    assert result.cells[-1] == "группировка: month (A)"


def test_group_by_numeric_keys_are_plain_strings(engine, csv_path):
    result = engine.compute(csv_path, spec("count", group_by="users"))
    assert "100" in result.by_group and "100.0" not in result.by_group
    assert result.by_group["(пусто)"] == 1  # строка без значения группы — отдельная группа
    assert result.consistent


def test_mismatch_between_engines_is_detected(engine, csv_path, monkeypatch):
    monkeypatch.setattr(engine, "_duckdb", lambda table, spec: (999.0, {}))
    result = engine.compute(csv_path, spec("mean", "users"))
    assert result.value == pytest.approx(statistics.mean(USERS))
    assert result.check_value == 999.0
    assert not result.consistent


def test_group_mismatch_is_detected(engine, csv_path, monkeypatch):
    real = engine._duckdb

    def skewed(table, s):
        value, groups = real(table, s)
        return value, {**groups, "Москва": groups["Москва"] + 1e-3}

    monkeypatch.setattr(engine, "_duckdb", skewed)
    result = engine.compute(csv_path, spec("mean", "users", group_by="region"))
    assert not result.consistent


# --- Ошибки и значения ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        spec("mean", "retention"),
        spec("mean", "users", group_by="regoin"),
        spec("count", filters=[MetricFilter(column="sigment", op="==", value="b2b")]),
        spec("ratio", "users", denominator_column="nope"),
    ],
)
def test_unknown_column_lists_available(engine, csv_path, bad):
    with pytest.raises(ValueError, match="Доступные столбцы: month, region, segment"):
        engine.compute(csv_path, bad)


def test_column_name_case_is_forgiven(engine, csv_path):
    assert engine.compute(csv_path, spec("max", "Users")).value == 130


def test_non_numeric_column_for_mean(engine, csv_path):
    with pytest.raises(ValueError, match="не числовой"):
        engine.compute(csv_path, spec("mean", "region"))


def test_distinct_values(engine, csv_path):
    assert engine.distinct_values(csv_path, "region") == ["Казань", "Москва"]
    assert engine.distinct_values(csv_path, "users", limit=3) == ["70", "80", "90"]  # числа по значению
    with pytest.raises(ValueError, match="Доступные столбцы"):
        engine.distinct_values(csv_path, "city")


def test_supports(engine):
    assert all(engine.supports(f"t{ext}") for ext in (".csv", ".tsv", ".xlsx", ".xls", ".CSV"))
    assert not engine.supports("t.json")


# --- Локальные форматы CSV -----------------------------------------------------------------------


def test_semicolon_decimal_comma_cp1251(engine, tmp_path):
    path = tmp_path / "выгрузка.csv"
    path.write_bytes("Регион;Выручка, руб.\nМосква;1 000,5\nКазань;2000,25\n".encode("cp1251"))
    p = engine.passport(str(path))
    assert p.columns == ["Регион", "Выручка, руб."]
    assert p.units == {"Выручка, руб.": "₽"}
    result = engine.compute(str(path), spec("sum", "Выручка, руб."))
    assert result.value == pytest.approx(3000.75)
    assert result.check_value == pytest.approx(3000.75)  # DuckDB читает перекодированную копию сам
    assert result.consistent


def test_tsv(engine, tmp_path):
    path = tmp_path / "t.tsv"
    path.write_text("a\tb\n1\tx\n3\ty\n", encoding="utf-8")
    result = engine.compute(str(path), spec("mean", "a", group_by="b"))
    assert result.value == 2 and result.by_group == {"x": 1.0, "y": 3.0} and result.consistent


# --- XLSX ----------------------------------------------------------------------------------------


@pytest.fixture
def xlsx_path(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    cover = wb.active
    cover.title = "Обложка"
    cover.append(["Отчёт", "Версия"])
    cover.append(["Q1", 1])
    data = wb.create_sheet("Данные")
    data.append(["date", "channel", "users", "conversion_rate"])
    rows = [
        (datetime(2026, 1, 1), "online", 100, 0.10),
        (datetime(2026, 2, 1), "online", 150, 0.12),
        (datetime(2026, 3, 1), "retail", 80, 0.05),
        (datetime(2026, 3, 1), "online", None, 0.11),
    ]
    for row in rows:
        data.append(list(row))
    path = tmp_path / "funnel.xlsx"
    wb.save(path)
    return str(path)


def test_xlsx_first_sheet_by_default(engine, xlsx_path):
    p = engine.passport(xlsx_path)
    assert p.sheet == "Обложка" and p.rows == 1 and p.columns == ["Отчёт", "Версия"]


def test_xlsx_passport_and_compute(engine, xlsx_path):
    p = engine.passport(xlsx_path, sheet="Данные")
    assert p.sheet == "Данные" and p.rows == 4
    assert p.period == "2026-01-01–2026-03-01"
    assert p.units == {"users": "пользователи", "conversion_rate": "доля"}
    assert p.missing_share["users"] == 0.25
    assert "| 2026-01-01 | online | 100 | 0.1 |" in p.preview

    result = engine.compute(xlsx_path, spec("sum", "users", group_by="channel"), sheet="Данные")
    assert result.value == 330
    assert result.by_group == {"online": 250.0, "retail": 80.0}
    assert result.check_by_group == result.by_group
    assert result.consistent
    assert result.cells[0] == "лист «Данные», столбец users (C), строки 2–4"

    dated = engine.compute(
        xlsx_path,
        spec("mean", "conversion_rate", filters=[MetricFilter(column="date", op=">=", value="2026-02-01")]),
        sheet="Данные",
    )
    assert dated.value == pytest.approx((0.12 + 0.05 + 0.11) / 3)
    assert dated.consistent
    assert engine.distinct_values(xlsx_path, "date", sheet="Данные") == [
        "2026-01-01",
        "2026-02-01",
        "2026-03-01",
    ]
    by_date = engine.compute(
        xlsx_path,
        spec("count", filters=[MetricFilter(column="date", op="==", value="2026-03-01")]),
        sheet="Данные",
    )
    assert by_date.value == 2 and by_date.consistent


def test_xlsx_unknown_sheet(engine, xlsx_path):
    with pytest.raises(ValueError, match="Листы: Обложка, Данные"):
        engine.passport(xlsx_path, sheet="Итоги")


# --- Помощники адресов ---------------------------------------------------------------------------


def test_address_helpers():
    assert [column_letter(i) for i in (0, 25, 26, 27, 701, 702)] == ["A", "Z", "AA", "AB", "ZZ", "AAA"]
    assert row_ranges([2, 3, 4, 7, 9, 10]) == "2–4, 7, 9–10"
    assert row_ranges(list(range(2, 40, 2))) == "2–38 (выборочно 19 строк)"


def test_share_by_group_without_filters_is_distribution(engine, csv_path):
    result = engine.compute(csv_path, spec("share", group_by="region"))
    assert result.consistent
    assert sum(result.by_group.values()) == pytest.approx(1.0)
    assert result.by_group == pytest.approx(result.check_by_group)


def test_ratio_ignores_censored_rows(tmp_path):
    """Когорта, у которой месяц ещё не наступил (пустая ячейка), не занижает удержание."""
    path = tmp_path / "cohorts.csv"
    path.write_text("cohort,new,m2\n2026-07,100,50\n2026-08,100,60\n2026-09,100,\n", encoding="utf-8")
    result = PandasDuckdbTableEngine().compute(
        str(path), MetricSpec(name="m2", agg="ratio", column="m2", denominator_column="new")
    )
    assert result.consistent
    assert result.value == pytest.approx(0.55)
    assert result.rows_used == 2


def test_sum_share_by_group(engine, csv_path):
    result = engine.compute(csv_path, spec("sum_share", "users", group_by="region"))
    assert result.consistent
    assert sum(result.by_group.values()) == pytest.approx(1.0)
    moscow = 100 + 120 + 130
    assert result.by_group["Москва"] == pytest.approx(moscow / sum(USERS))
