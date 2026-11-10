"""Ловушки агрегирования: парадокс Симпсона и среднее долей по строкам разного размера."""

from __future__ import annotations

import re

# Перенесено в контракт расчёта (TASK-60); имя оставлено здесь для прежних импортов
from strategist.domain.services.metric_contract import churn_label_on_retention  # noqa: F401


def leader(values: dict[str, float]) -> str | None:
    clean = {k: v for k, v in values.items() if v is not None}
    if len(clean) < 2:
        return None
    return max(clean, key=lambda k: clean[k])


def simpson_reversal(overall: dict[str, float], by_segment: dict[str, dict[str, float]]) -> str | None:
    """Если в большинстве сегментов лидирует не та группа, что в целом, — возвращает описание."""
    total_leader = leader(overall)
    if total_leader is None:
        return None
    segment_leaders = {seg: leader(vals) for seg, vals in by_segment.items()}
    decided = {seg: lead for seg, lead in segment_leaders.items() if lead is not None}
    if len(decided) < 2:
        return None
    against = [seg for seg, lead in decided.items() if lead != total_leader]
    if len(against) > len(decided) / 2:
        others = sorted({decided[s] for s in against})
        return (
            f"В целом лидирует «{total_leader}», но внутри {len(against)} из {len(decided)} сегментов "
            f"лидирует {', '.join(f'«{o}»' for o in others)} — возможен парадокс Симпсона (смена состава)."
        )
    return None


_RATE_COLUMN = re.compile(
    r"pct|percent|rate|share|ratio|conversion|churn|retention|доля|процент|конверси|отток|удержан", re.I
)


def is_rate_column(column: str | None, unit: str | None = None) -> bool:
    """Столбец с долей или процентом — по имени или единице измерения."""
    return bool(column) and (bool(_RATE_COLUMN.search(column or "")) or (unit or "").strip() in ("%", "п.п."))


def mean_of_rates_warning(
    agg: str, column: str | None, unit: str | None, rows_used: int, groups: int, binary: bool
) -> str | None:
    """Среднее столбца с долями по нескольким строкам: строка в 35 тыс. пользователей весит как строка в 1,7 тыс.

    Не срабатывает на столбце 0/1 (одна строка — один объект: среднее и есть доля) и на группах из одной строки.
    """
    if agg != "mean" or binary or not is_rate_column(column, unit):
        return None
    if rows_used <= max(groups, 1):
        return None
    return (
        f"среднее столбца с долями «{column}» по нескольким строкам разного размера — каждая строка весит "
        "одинаково, поэтому итог может вводить в заблуждение. Для доли нужно отношение счётчиков (ratio) или "
        "разбивка, при которой в группе одна строка"
    )
