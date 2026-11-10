"""Контракт расчёта: смысл числа проверяется там, где число рождается, — на шаге расчёта.

Около трети утверждений «без опоры» на стенде v0.1.1 — собственные расчёты агента с неверным смыслом:
«средний срок до отмены 2,6 дня» — среднее по `months_subscribed`; «отток 84,1%» — доля оставшихся
(`active_m2 / active_m1`); «доля строк» по таблице, где строка — когорта или сегмент, а не человек. Такое число
совпадает со своей строкой реестра, поэтому сверка текста его не поймает. Здесь — чистые правила: что не так
со спецификацией расчёта и как исправить подпись или единицу. Значение расчёта не меняется никогда.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel


class IssueKind(StrEnum):
    UNIT_TIME = "unit_time"  # единица времени не та, что у столбца (months_subscribed → «дни»)
    SHARE_VS_PERCENT = "share_vs_percent"  # доля 0–1 подписана процентами или проценты — долей
    CHURN_LABEL = "churn_label"  # «отток» над долей оставшихся
    ROW_SHARE_AGGREGATED = "row_share_aggregated"  # «доля строк» по агрегированной таблице


class ContractIssue(BaseModel):
    kind: IssueKind
    message: str
    unit: str | None = None  # исправленная единица (UNIT_TIME, SHARE_VS_PERCENT)
    label: str | None = None  # исправленная подпись (CHURN_LABEL)


# ----------------------------------------------------------------------------- единицы времени

TIME_UNIT = {
    "month": "мес",
    "day": "дн",
    "week": "нед",
    "hour": "ч",
    "year": "лет",
}  # без точки: «2,3 мес. По…»
TIME_WORD = {"month": "месяцы", "day": "дни", "week": "недели", "hour": "часы", "year": "годы"}
_UNIT_KIND = (
    ("month", re.compile(r"мес\.?|месяц\w*|months?", re.I)),
    ("day", re.compile(r"дн\.?|дни|дней|день|дня|days?", re.I)),
    ("week", re.compile(r"нед\.?|недел\w*|weeks?", re.I)),
    ("hour", re.compile(r"ч\.?|час\w*|hours?", re.I)),
    ("year", re.compile(r"г\.?|год\w*|лет|years?", re.I)),
)
_SNAKE = {"month": "month", "months": "month", "day": "day", "days": "day", "week": "week", "weeks": "week"}
_SNAKE |= {"hour": "hour", "hours": "hour", "year": "year", "years": "year"}
_DURATION_SNAKE = re.compile(r"(?:^|_)(months?|days?|weeks?|hours?|years?)(?:_|$)", re.I)
_DURATION_RU = re.compile(
    r"^(месяц\w*|мес\.?|дн\w*|дней|недел\w*|час\w*|лет)(?:\s|,|$)|,\s*(мес|дн|нед|ч)\.?$", re.I
)
_RU_KIND = {
    "мес": "month",
    "дн": "day",
    "дне": "day",
    "нед": "week",
    "час": "hour",
    "ч": "hour",
    "лет": "year",
}
# Частота, а не длительность: orders_per_month, «заявок в неделю», «₽ за сотрудника в месяц»
_RATE = re.compile(
    r"per_?(?:month|day|week|hour|year)|(?:month|week|dai)ly|_per_|\bв (?:неделю|месяц|день|час|год)\b|"
    r"\bза (?:неделю|месяц|день|час)\b|/\s*(?:мес|нед|день|час)",
    re.I,
)
_VALUE_AGGS = ("mean", "median", "sum", "min", "max")


def unit_time_kind(unit: str | None) -> str | None:
    text = (unit or "").strip()
    return next((kind for kind, rx in _UNIT_KIND if rx.fullmatch(text)), None)


def column_time_unit(column: str | None) -> str | None:
    """Длительность в единицах столбца по его имени: months_subscribed → month, «Недель в пилоте» → week.
    Частоты («в неделю», per_month) — не длительность: их единица — счётчик, а не время."""
    if not column or _RATE.search(column):
        return None
    m = _DURATION_SNAKE.search(column)
    if m:
        return _SNAKE[m.group(1).lower()]
    m = _DURATION_RU.search(column.strip())
    if not m:
        return None
    word = (m.group(1) or m.group(2)).lower()
    return next((kind for prefix, kind in _RU_KIND.items() if word.startswith(prefix)), None)


# ----------------------------------------------------------------------------- отток над удержанием

_CHURN_LABEL = re.compile(r"отток|ушедш|ушл|churn|отмен|потер", re.I)
_RETAINED_COLUMN = re.compile(r"active|retain|renew|paid|stay|продл|активн|удерж|остал|(?:^|_)m\d+$", re.I)
_CHURN_COLUMN = re.compile(r"churn|cancel|lost|left|отток|отмен|ушед|ушл", re.I)


def churn_label_on_retention(label: str, agg: str, column: str | None) -> bool:
    """Подпись говорит об оттоке, а в числителе — оставшиеся (active_m2 / active_m1, renewed / due).

    Частая ошибка подписи: 84% «оттока» на деле 84% оставшихся, отток — 16%."""
    return (
        agg == "ratio"
        and bool(column)
        and bool(_CHURN_LABEL.search(label or ""))
        and bool(_RETAINED_COLUMN.search(column or ""))
        and not _CHURN_COLUMN.search(column or "")
    )


def relabel_retention(column: str | None, denominator_column: str | None) -> str:
    return f"Доля оставшихся: {column} / {denominator_column}"


# ----------------------------------------------------------------------------- агрегированная таблица

_ID_COLUMN = re.compile(
    r"(?:^|_)id$|^id(?:_|$)|uuid|ticket|review|respondent|response|^клиент|номер|^№", re.I
)
_COUNT_COLUMN = re.compile(
    r"users|subscribers|customers|clients|accounts|sessions|visits|orders|count|cnt|(?:^|_)n(?:_|$)|num_|"
    r"(?:^|_)new_|(?:^|_)active_|total|пользовател|подписчик|клиент|заказ|количеств|всего",
    re.I,
)


_CODE_VALUE = re.compile(r"^[A-Za-zА-Яа-яЁё]{0,4}[-_]?\d{2,}$")


def looks_like_id(values: list[str]) -> bool:
    """Значения — коды строк («T-0015», «R-0001», «12345»), а не подписи групп («2025-11», «organic»)."""
    clean = [str(v).strip() for v in values if str(v).strip()]
    return bool(clean) and all(_CODE_VALUE.match(v) for v in clean)


def is_aggregated_table(columns: list[str], dtypes: dict[str, str], id_columns: set[str]) -> bool:
    """Строка таблицы — группа (когорта × канал, месяц, группа × сегмент), а не человек: нет столбца-идентификатора
    (по имени или по значениям-кодам, их находит вызывающий код через looks_like_id), зато есть числовой счётчик
    (users, new_subscribers). Тогда «доля строк» — доля групп, а не людей, и в отчёт как доля людей не годится.
    Уникальная подпись группы (месяц, название группы) идентификатором не считается."""
    if id_columns or any(_ID_COLUMN.search(c) for c in columns):
        return False
    numeric = ("int", "float", "uint")
    return any(_COUNT_COLUMN.search(c) and dtypes.get(c, "").startswith(numeric) for c in columns)


# ----------------------------------------------------------------------------- проверка расчёта

_SHARE_UNITS = ("доля", "share", "ratio")


def contract_issues(
    *,
    label: str,
    agg: str,
    column: str | None,
    denominator_column: str | None,
    unit: str | None,
    values: list[float],
    aggregated_table: bool = False,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    kind = column_time_unit(column) if agg in _VALUE_AGGS else None
    if kind is not None:
        stated = unit_time_kind(unit)
        if stated is not None and stated != kind:
            issues.append(
                ContractIssue(
                    kind=IssueKind.UNIT_TIME,
                    unit=TIME_UNIT[kind],
                    message=f"единица исправлена: в столбце «{column}» {TIME_WORD[kind]}, а не «{unit}»",
                )
            )
        elif not (unit or "").strip():
            issues.append(
                ContractIssue(
                    kind=IssueKind.UNIT_TIME,
                    unit=TIME_UNIT[kind],
                    message=f"единица по столбцу «{column}»: {TIME_WORD[kind]}",
                )
            )
    clean = [v for v in values if v is not None]
    stated_unit = (unit or "").strip().lower()
    if (
        clean
        and agg in ("ratio", "share", "sum_share")
        and stated_unit == "%"
        and all(0 <= v <= 1 for v in clean)
    ):
        issues.append(
            ContractIssue(
                kind=IssueKind.SHARE_VS_PERCENT,
                unit="доля",
                message="единица исправлена: значения — доли от 0 до 1, показаны в процентах",
            )
        )
    elif clean and agg == "mean" and stated_unit in _SHARE_UNITS and all(1 < v <= 100 for v in clean):
        issues.append(
            ContractIssue(
                kind=IssueKind.SHARE_VS_PERCENT,
                unit="%",
                message="единица исправлена: в столбце уже проценты, а не доли от 0 до 1",
            )
        )
    if churn_label_on_retention(label, agg, column):
        issues.append(
            ContractIssue(
                kind=IssueKind.CHURN_LABEL,
                label=relabel_retention(column, denominator_column),
                message="подпись исправлена: считается доля оставшихся, а не отток; отток = 1 − значение",
            )
        )
    if agg == "share" and aggregated_table:
        issues.append(
            ContractIssue(
                kind=IssueKind.ROW_SHARE_AGGREGATED,
                message="доля строк агрегированной таблицы: строка — группа, а не человек; в отчёт не выводится",
            )
        )
    return issues


# ----------------------------------------------------------------------------- период расчёта

_PERIOD_COLUMN = re.compile(
    r"month|date|period|quarter|year|week|cohort|месяц|дата|период|квартал|год|недел", re.I
)


def period_of(filters: list[tuple[str, str, object]], table_period: str | None) -> str | None:
    """Период расчёта: фильтр «столбец периода == значение» точнее периода всей таблицы из паспорта."""
    for column, op, value in filters:
        if op == "==" and _PERIOD_COLUMN.search(column or ""):
            return f"{column} = {value}"
    return table_period
