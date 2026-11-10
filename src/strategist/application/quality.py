"""Выключатели улучшений качества v0.2.0 (`config/settings.yaml → quality`).

Каждое улучшение включается своим флагом. Выключенный флаг — поведение v0.1.1: так меряется вклад каждого
улучшения (абляция), а прогоны стенда без улучшений воспроизводимы. Флага нет в настройках — он выключен, поэтому
старые конфиги и тесты на фейках работают как раньше.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FLAGS = (
    "metric_contract",  # TASK-60: единица времени, «отток» на удержании, доля строк, период и n расчёта
    "numbers_v2",  # TASK-61: сверка чисел с единицами и выведенными числами
    "model_labels",  # TASK-61: расчёты и оценки модели агента подписаны как таковые
    "summary_repair",  # TASK-62: повтор выжимки по замечаниям, замена довода, статусы и оговорки
    "ru_labels",  # TASK-71: статусы, виды и вердикты в отчёте — по-русски, а не значениями перечислений
)


def enabled(settings: Mapping[str, Any], flag: str) -> bool:
    if flag not in FLAGS:
        raise KeyError(f"Неизвестный флаг качества {flag!r}; известны: {', '.join(FLAGS)}")
    return bool((settings.get("quality") or {}).get(flag, False))


def switch_off(quality: Mapping[str, Any], off: str | None) -> dict[str, bool]:
    """Флаги после `STRATEGIST_QUALITY_OFF`: «all» или список через запятую. Опечатка — ошибка, а не тихий пропуск."""
    out = {flag: bool(quality.get(flag, False)) for flag in FLAGS}
    names = [x.strip() for x in (off or "").split(",") if x.strip()]
    if names == ["all"]:
        return dict.fromkeys(FLAGS, False)
    unknown = [x for x in names if x not in FLAGS]
    if unknown:
        raise ValueError(
            f"STRATEGIST_QUALITY_OFF: неизвестные флаги {unknown}; известны: all, {', '.join(FLAGS)}"
        )
    for name in names:
        out[name] = False
    return out
