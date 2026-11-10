"""Линтер плана проверки: без baseline-шага, guardrail или даты kill-критерия эксперимент не принимается."""

from __future__ import annotations

from strategist.domain.common import LintIssue, Severity
from strategist.domain.entities.experiments import Experiment


def lint_experiment(exp: Experiment) -> list[LintIssue]:
    issues: list[LintIssue] = []

    def add(code: str, message: str, severity: Severity = Severity.ERROR) -> None:
        issues.append(LintIssue(code=code, severity=severity, entity_id=exp.id, message=message))

    if exp.baseline is None and not (exp.baseline_step or "").strip():
        add("no_baseline", "Нет baseline и нет шага его получения")
    if not exp.guardrails:
        add("no_guardrail", "Нет guardrail-метрик (нежелательные последствия не отслеживаются)")
    if exp.kill_criterion is None:
        add("no_kill_criterion", "Нет kill-критерия")
    elif exp.kill_criterion.deadline is None:
        add("kill_without_deadline", "Kill-критерий без даты")
    if not exp.metric.strip():
        add("no_metric", "Нет основной метрики")
    if not exp.decision_rule.strip():
        add("no_decision_rule", "Правило решения не зафиксировано до запуска")
    if (
        exp.observation_period_days is not None
        and exp.behavior_cycle_days is not None
        and exp.observation_period_days < exp.behavior_cycle_days
    ):
        add(
            "period_shorter_than_cycle",
            "Период наблюдения короче поведенческого цикла",
            Severity.WARNING,
        )
    if exp.observation_period_days is None:
        add("no_period", "Не задан срок наблюдения")
    if not exp.hypothesis.lower().startswith("мы считаем"):
        add("hypothesis_format", "Гипотеза не в формате «Мы считаем, что…»", Severity.WARNING)
    return issues


def experiment_ok(exp: Experiment) -> bool:
    return not any(i.severity == Severity.ERROR for i in lint_experiment(exp))
