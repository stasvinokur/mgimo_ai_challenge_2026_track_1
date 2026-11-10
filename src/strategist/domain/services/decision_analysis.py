"""Ценность информации и стресс-тест решения на аддитивной модели полезности.

Таблица «варианты × 3–6 неопределённостей × уровни»: для каждого фактора лучший вариант пересчитывается
на каждом уровне; исследуются только факторы, меняющие победителя (по мотивам DeLLMa и UoT).
Стресс-тест перебирает сценарии и показывает, в скольких из них базовый победитель остаётся лучшим,
и границу решения — при каких уровнях факторов выигрывает другой вариант.
"""

from __future__ import annotations

import itertools

from strategist.domain.common import Entity
from strategist.domain.entities.options import (
    Boundary,
    DecisionMatrix,
    ScenarioFlip,
    StressResult,
    Uncertainty,
)

Scenario = dict[str, str]


def option_ids(matrix: DecisionMatrix) -> list[str]:
    return list(matrix.base_utility)


def recenter_effects(
    effects: dict[str, dict[str, dict[str, float]]], new_base: dict[str, str]
) -> dict[str, dict[str, dict[str, float]]]:
    """Эффекты уровня отсчитываются от базового уровня (на нём эффект 0). Если базовый уровень неопределённости
    сменился, эффекты всех вариантов сдвигаются так, чтобы на новом базовом уровне эффект снова был 0."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    for option, by_unc in effects.items():
        out[option] = {}
        for unc, levels in by_unc.items():
            if unc in new_base:
                shift = levels.get(new_base[unc], 0.0)
                out[option][unc] = {lvl: v - shift for lvl, v in levels.items()}
            else:
                out[option][unc] = dict(levels)
    return out


def base_scenario(matrix: DecisionMatrix) -> Scenario:
    return {u.id: u.base_level for u in matrix.uncertainties}


def utility(matrix: DecisionMatrix, option_id: str, scenario: Scenario) -> float:
    total = matrix.base_utility.get(option_id, 0.0)
    effects = matrix.effects.get(option_id, {})
    for unc_id, level in scenario.items():
        total += effects.get(unc_id, {}).get(level, 0.0)
    return total


def winner(matrix: DecisionMatrix, scenario: Scenario) -> tuple[str, float]:
    """Победитель и отрыв от второго места. Ничья решается порядком вариантов в матрице."""
    scored = sorted(
        ((utility(matrix, o, scenario), -i, o) for i, o in enumerate(option_ids(matrix))), reverse=True
    )
    if not scored:
        raise ValueError("в матрице нет вариантов")
    margin = scored[0][0] - scored[1][0] if len(scored) > 1 else float("inf")
    return scored[0][2], margin


def _probabilities(unc: Uncertainty) -> dict[str, float]:
    total = sum(level.probability for level in unc.levels) or 1.0
    return {level.name: level.probability / total for level in unc.levels}


class FactorAssessment(Entity):
    uncertainty_id: str
    name: str
    flips_winner: bool
    evpi: float
    winners_by_level: dict[str, str]


def assess_factors(matrix: DecisionMatrix) -> list[FactorAssessment]:
    """Для каждого фактора: меняет ли он победителя и ожидаемая ценность совершенной информации (EVPI)."""
    base = base_scenario(matrix)
    base_win, _ = winner(matrix, base)
    result: list[FactorAssessment] = []
    for unc in matrix.uncertainties:
        probs = _probabilities(unc)
        winners: dict[str, str] = {}
        value_with_info = 0.0
        expected_by_option = dict.fromkeys(option_ids(matrix), 0.0)
        for level in unc.levels:
            scenario = {**base, unc.id: level.name}
            win, _ = winner(matrix, scenario)
            winners[level.name] = win
            value_with_info += probs[level.name] * utility(matrix, win, scenario)
            for opt in expected_by_option:
                expected_by_option[opt] += probs[level.name] * utility(matrix, opt, scenario)
        value_without_info = max(expected_by_option.values()) if expected_by_option else 0.0
        result.append(
            FactorAssessment(
                uncertainty_id=unc.id,
                name=unc.name,
                flips_winner=any(w != base_win for w in winners.values()),
                evpi=round(max(0.0, value_with_info - value_without_info), 6),
                winners_by_level=winners,
            )
        )
    return sorted(result, key=lambda f: (-int(f.flips_winner), -f.evpi))


def stress_test(matrix: DecisionMatrix, max_factors: int = 4) -> StressResult:
    if not matrix.uncertainties:
        win, _ = winner(matrix, {})
        return StressResult(
            base_winner=win,
            scenarios_total=1,
            scenarios_survived=1,
            summary=f"{win}: неопределённости не заданы — стресс-тест не проводился",
        )
    base = base_scenario(matrix)
    base_win, _ = winner(matrix, base)
    factors = assess_factors(matrix)
    chosen_ids = [f.uncertainty_id for f in factors[:max_factors]]
    chosen = [u for u in matrix.uncertainties if u.id in chosen_ids]
    probs = {u.id: _probabilities(u) for u in chosen}

    total = survived = 0
    survival_probability = 0.0
    flips: list[ScenarioFlip] = []
    for combo in itertools.product(*[[lvl.name for lvl in u.levels] for u in chosen]):
        scenario = {**base, **dict(zip(chosen_ids, combo, strict=True))}
        p = 1.0
        for unc_id, level in zip(chosen_ids, combo, strict=True):
            p *= probs[unc_id][level]
        win, margin = winner(matrix, scenario)
        total += 1
        if win == base_win:
            survived += 1
            survival_probability += p
        else:
            flips.append(
                ScenarioFlip(
                    scenario=dict(zip(chosen_ids, combo, strict=True)), winner=win, margin=round(margin, 4)
                )
            )

    boundaries: list[Boundary] = []
    names = {u.id: u.name for u in matrix.uncertainties}
    for factor in factors:
        if not factor.flips_winner:
            continue
        flipped = {lvl: w for lvl, w in factor.winners_by_level.items() if w != base_win}
        parts = [f"на уровне «{lvl}» выигрывает {w}" for lvl, w in flipped.items()]
        boundaries.append(
            Boundary(
                uncertainty_id=factor.uncertainty_id,
                statement=f"{base_win} предпочтителен, пока «{names[factor.uncertainty_id]}» не уходит: "
                + "; ".join(parts),
                winners_by_level=factor.winners_by_level,
            )
        )
    critical = [f.uncertainty_id for f in factors if f.flips_winner]
    summary = (
        f"{base_win} выигрывает в базовом сценарии и остаётся лучшим в {survived} из {total} сценариев "
        f"(вероятность сохранения ≈ {survival_probability:.0%}). "
        + (
            "Решение меняют факторы: " + ", ".join(names[c] for c in critical) + "."
            if critical
            else "Ни один фактор по отдельности не меняет победителя."
        )
    )
    return StressResult(
        base_winner=base_win,
        scenarios_total=total,
        scenarios_survived=survived,
        survival_probability=round(survival_probability, 4),
        flips=sorted(flips, key=lambda f: f.margin, reverse=True)[:12],
        boundaries=boundaries,
        critical_uncertainty_ids=critical,
        summary=summary,
    )
