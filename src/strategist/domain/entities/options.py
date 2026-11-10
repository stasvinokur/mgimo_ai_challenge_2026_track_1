"""Варианты решения, возражения, панель ролей и модель решения для стресс-теста."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import Field

from strategist.domain.common import Entity


class OptionKind(StrEnum):
    STATUS_QUO = "status_quo"
    KILL = "kill"
    PIVOT = "pivot"
    INVEST = "invest"
    FOCUS = "focus"
    EXPAND = "expand"
    OTHER = "other"

    @property
    def label_ru(self) -> str:
        return {
            "status_quo": "оставить как есть",
            "kill": "закрыть / заморозить направление",
            "pivot": "сменить направление",
            "invest": "инвестировать и улучшить",
            "focus": "сфокусироваться на сегменте",
            "expand": "расширить",
            "other": "другое",
        }[self.value]


MANDATORY_OPTION_KINDS = (OptionKind.STATUS_QUO, OptionKind.KILL)


class OptionStatus(StrEnum):
    CANDIDATE = "candidate"
    SELECTED = "selected"
    REJECTED = "rejected"
    REOPENED = "reopened"
    CLONE = "clone"


class KillCriterion(Entity):
    """Kill-критерий по Дьюк: заранее заданный сигнал плюс дата."""

    signal: str
    threshold: str | None = None
    deadline: date | None = None


class Option(Entity):
    id: str
    kind: OptionKind
    title: str
    summary: str
    rationale: str = ""
    supporting_claim_ids: list[str] = Field(default_factory=list)
    assumption_claim_ids: list[str] = Field(default_factory=list)
    conscious_refusals: list[str] = Field(default_factory=list)
    kill_criterion: KillCriterion | None = None
    costs: str = ""
    revenue: str = ""
    status: OptionStatus = OptionStatus.CANDIDATE
    rejected_reason: str | None = None
    revisit_when: str | None = None
    clone_of: str | None = None


class ObjectionKind(StrEnum):
    PREMORTEM = "premortem"
    STEELMAN = "steelman"
    ROLE = "role"
    MUTATION = "mutation"

    @property
    def label_ru(self) -> str:
        return {
            "premortem": "пре-мортем",
            "steelman": "сильнейший довод против",
            "role": "роль",
            "mutation": "мутация",
        }[self.value]


class Objection(Entity):
    """Возражение обязано ссылаться на claim_id — иначе это явно помеченная гипотеза."""

    id: str
    option_id: str
    kind: ObjectionKind
    text: str
    claim_ids: list[str] = Field(default_factory=list)
    is_hypothesis: bool = False
    strength: int | None = Field(default=None, ge=1, le=5)
    author_role: str | None = None


class ScoreOrder(StrEnum):
    FORWARD = "forward"
    REVERSE = "reverse"


class RoleScore(Entity):
    role: str
    option_id: str
    score: float = Field(ge=0, le=10)
    order: ScoreOrder
    rationale: str = ""


class UncertaintyLevel(Entity):
    name: str
    description: str = ""
    probability: float = Field(ge=0, le=1)


class Uncertainty(Entity):
    """Ключевая неопределённость с 2–3 уровнями и вербальными вероятностями."""

    id: str
    name: str
    description: str = ""
    levels: list[UncertaintyLevel]
    base_level: str
    linked_claim_ids: list[str] = Field(default_factory=list)
    unknown_id: str | None = None


class DecisionMatrix(Entity):
    """Аддитивная модель полезности: u(вариант, сценарий) = база + Σ эффект(неопределённость, уровень).

    Грубая и прозрачная: её цель — показать, от каких переменных зависит выбор, а не точность.
    """

    uncertainties: list[Uncertainty] = Field(default_factory=list)
    base_utility: dict[str, float] = Field(default_factory=dict)
    effects: dict[str, dict[str, dict[str, float]]] = Field(default_factory=dict)
    note: str = ""


class ScenarioFlip(Entity):
    scenario: dict[str, str]
    winner: str
    margin: float


class Boundary(Entity):
    uncertainty_id: str
    statement: str
    winners_by_level: dict[str, str]


class StressResult(Entity):
    base_winner: str
    scenarios_total: int
    scenarios_survived: int
    survival_probability: float | None = None
    flips: list[ScenarioFlip] = Field(default_factory=list)
    boundaries: list[Boundary] = Field(default_factory=list)
    critical_uncertainty_ids: list[str] = Field(default_factory=list)
    summary: str = ""
