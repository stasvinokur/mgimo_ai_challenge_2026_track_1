"""PlanExperiments: план проверки из неизвестных, ранжированных по влиянию на решение.

Каждый эксперимент — гипотеза «Мы считаем, что…», метрика с baseline (или шаг его получения), MDE,
период не короче поведенческого цикла, guardrails с порогами и ответственным, kill-критерий с датой,
правило решения до запуска. Прогнозы — медиана независимых прогонов в разных контекстах (ансамбль
LLM не хуже толпы, но отдельные модели завышают вероятности выше 50% — Schoenegger et al.).
"""

from __future__ import annotations

import json
from datetime import timedelta
from statistics import median

from strategist.application.context import AppContext
from strategist.application.digests import claims_digest, frame_digest, unknowns_digest
from strategist.application.dto.llm_outputs import ExperimentsDTO, ForecastDTO
from strategist.application.llm_calls import ask_structured
from strategist.application.utils import known_ids, parse_date
from strategist.domain.entities import (
    DecisionCase,
    Experiment,
    Forecast,
    Guardrail,
    KillCriterion,
    UnknownStatus,
)
from strategist.domain.services.experiment_linter import experiment_ok, lint_experiment


def _ranked_unknowns(case: DecisionCase) -> list[str]:
    open_unknowns = [u for u in case.unknowns if u.status in (UnknownStatus.OPEN, UnknownStatus.RESEARCHING)]
    open_unknowns.sort(key=lambda u: (not u.flips_decision, -(u.voi or 0)))
    return [u.id for u in open_unknowns]


def _experiment_from_dto(case: DecisionCase, dto, reserved: list[str]) -> Experiment:
    exp = Experiment(
        id=case.new_id("experiment", reserved=reserved),
        unknown_id=dto.unknown_id if case.unknown(dto.unknown_id or "") else None,
        assumption_claim_ids=known_ids(dto.assumption_claim_ids, {c.id for c in case.claims}),
        hypothesis=dto.hypothesis,
        metric=dto.metric,
        baseline=dto.baseline,
        baseline_source=dto.baseline_source,
        baseline_step=dto.baseline_step,
        expected_change=dto.expected_change,
        mde=dto.mde,
        comparison=dto.comparison,
        observation_period_days=dto.observation_period_days,
        behavior_cycle_days=dto.behavior_cycle_days,
        guardrails=[Guardrail(metric=g.metric, threshold=g.threshold, owner=g.owner) for g in dto.guardrails],
        kill_criterion=KillCriterion(
            signal=dto.kill_criterion.signal,
            threshold=dto.kill_criterion.threshold,
            deadline=parse_date(dto.kill_criterion.deadline),
        ),
        decision_rule=dto.decision_rule,
        owner=dto.owner,
        undesired_effects=dto.undesired_effects,
    )
    reserved.append(exp.id)
    return exp


def plan_experiments(ctx: AppContext, case: DecisionCase) -> list[Experiment]:
    option = case.option(case.selected_option_id or "")
    ranked = _ranked_unknowns(case)
    feedback = ""
    experiments: list[Experiment] = []
    for attempt in range(2):
        dto, _ = ask_structured(
            ctx,
            task="experiments",
            prompt="experiments",
            values={
                "frame": frame_digest(case),
                "option": f"{option.title}: {option.summary}" if option else "—",
                "unknowns": unknowns_digest(case),
                "priority": ", ".join(ranked) or "—",
                "assumptions": claims_digest(
                    case, [cid for cid in (option.assumption_claim_ids if option else [])]
                ),
                "product_metrics": "; ".join(case.product.metrics) if case.product else "—",
                "today": ctx.clock.today().isoformat(),
                "feedback": feedback or "—",
            },
            schema=ExperimentsDTO,
            case_id=case.id,
            step="experiments",
        )
        reserved: list[str] = []
        experiments = [_experiment_from_dto(case, e, reserved) for e in dto.experiments]
        bad = [e for e in experiments if not experiment_ok(e)]
        if not bad or attempt == 1:
            break
        feedback = "Линтер плана проверки отклонил эксперименты:\n" + "\n".join(
            f"- {e.hypothesis[:80]}: " + "; ".join(i.message for i in lint_experiment(e)) for e in bad
        )
    for priority, exp in enumerate(
        sorted(experiments, key=lambda e: ranked.index(e.unknown_id) if e.unknown_id in ranked else 99)
    ):
        exp.priority = priority + 1
    case.experiments = experiments
    rejected = [e.id for e in experiments if not experiment_ok(e)]
    ctx.emit("experiments.planned", case.id, count=len(experiments), rejected=rejected)
    return experiments


def make_forecasts(ctx: AppContext, case: DecisionCase, samples: int = 3) -> list[Forecast]:
    """Вероятность успеха варианта — в независимых контекстах; храним медиану и все выборки."""
    forecasts: list[Forecast] = []
    reserved: list[str] = []
    horizon_days = max((e.observation_period_days or 0) for e in case.experiments) if case.experiments else 90
    resolution = ctx.clock.today() + timedelta(days=max(30, horizon_days))
    targets = [o for o in case.options if o.status.value != "clone"]
    for option in targets:
        statement = (
            f"Через {max(30, horizon_days)} дней вариант «{option.title}» даст целевое изменение основной метрики"
            + (f" ({case.product.metrics[0]})" if case.product and case.product.metrics else "")
        )
        values = []
        for i in range(samples):
            dto, _ = ask_structured(
                ctx,
                task="judge" if i % 2 else "experiments",
                prompt="forecast",
                values={
                    "frame": frame_digest(case),
                    "statement": statement,
                    "option": f"{option.title}: {option.summary}",
                    "claims": claims_digest(case, limit=60),
                    "angle": ["базовая частота похожих инициатив", "сценарий провала", "сценарий успеха"][
                        i % 3
                    ],
                },
                schema=ForecastDTO,
                case_id=case.id,
                step=f"forecast:{option.id}:{i}",
                temperature=0.7,
            )
            values.append(dto.probability)
        fc = Forecast(
            id=case.new_id("forecast", reserved=reserved),
            option_id=option.id,
            statement=statement,
            probability=round(median(values), 3),
            samples=values,
            resolution_date=resolution,
            resolution_criterion="Сравнение основной метрики с baseline по правилу решения эксперимента",
            created_at=ctx.clock.now(),
        )
        reserved.append(fc.id)
        forecasts.append(fc)
    case.forecasts = forecasts
    export_forecasts(ctx, case)
    return forecasts


def export_forecasts(ctx: AppContext, case: DecisionCase) -> str | None:
    if ctx.artifacts is None:
        return None
    lines = [
        json.dumps(
            {
                "id": f.id,
                "case_id": case.id,
                "option_id": f.option_id,
                "statement": f.statement,
                "probability": f.probability,
                "samples": f.samples,
                "resolution_date": f.resolution_date.isoformat(),
                "resolution_criterion": f.resolution_criterion,
                "created_at": f.created_at.isoformat(),
                "outcome": f.outcome,
            },
            ensure_ascii=False,
        )
        for f in case.forecasts
    ]
    return ctx.artifacts.write(case.id, "state/forecasts.jsonl", "\n".join(lines) + ("\n" if lines else ""))
