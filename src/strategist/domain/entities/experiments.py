"""План проверки: эксперименты и прогнозы с датами разрешения."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from strategist.domain.common import Entity
from strategist.domain.entities.options import KillCriterion


class Guardrail(Entity):
    metric: str
    threshold: str
    owner: str = ""


class Experiment(Entity):
    id: str
    unknown_id: str | None = None
    assumption_claim_ids: list[str] = Field(default_factory=list)
    hypothesis: str
    metric: str
    baseline: float | None = None
    baseline_source: str | None = None
    baseline_step: str | None = None
    expected_change: str = ""
    mde: str = ""
    comparison: str = ""
    observation_period_days: int | None = None
    behavior_cycle_days: int | None = None
    guardrails: list[Guardrail] = Field(default_factory=list)
    kill_criterion: KillCriterion | None = None
    decision_rule: str = ""
    owner: str = ""
    priority: int = 0
    undesired_effects: list[str] = Field(default_factory=list)


class Forecast(Entity):
    id: str
    option_id: str | None = None
    statement: str
    probability: float = Field(ge=0, le=1)
    samples: list[float] = Field(default_factory=list)
    resolution_date: date
    resolution_criterion: str
    method: str = "median of independent runs"
    created_at: datetime
    outcome: bool | None = None
