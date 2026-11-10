"""Реестр утверждений: тип, опора, надёжность, условия пересмотра."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import Field, field_validator

from strategist.domain.common import Entity
from strategist.domain.entities.sources import Origin, Reliability


class ClaimType(StrEnum):
    FACT = "fact"
    DERIVED_METRIC = "derived_metric"
    HYPOTHESIS = "hypothesis"
    FORECAST = "forecast"
    ASSUMPTION = "assumption"
    UNKNOWN = "unknown"
    SYNTHETIC = "synthetic"

    @property
    def label_ru(self) -> str:
        return {
            "fact": "факт",
            "derived_metric": "расчётная метрика",
            "hypothesis": "гипотеза",
            "forecast": "прогноз",
            "assumption": "допущение",
            "unknown": "неизвестно",
            "synthetic": "синтетика",
        }[self.value]


class SupportLabel(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"
    UNVERIFIED = "unverified"

    @property
    def label_ru(self) -> str:
        return {
            "supported": "подтверждено",
            "partial": "частично",
            "unsupported": "нет опоры",
            "contradicted": "опровергнуто",
            "unverified": "не проверено",
        }[self.value]


class ClaimStatus(StrEnum):
    ACTIVE = "active"
    SUSPECT = "suspect"
    RETRACTED = "retracted"
    SUPERSEDED = "superseded"


class Likelihood(StrEnum):
    """Шкала вероятностей ICD 203 (фиксированные словесные диапазоны)."""

    ALMOST_NO_CHANCE = "almost_no_chance"
    VERY_UNLIKELY = "very_unlikely"
    UNLIKELY = "unlikely"
    ROUGHLY_EVEN = "roughly_even"
    LIKELY = "likely"
    VERY_LIKELY = "very_likely"
    ALMOST_CERTAIN = "almost_certain"

    @property
    def label_ru(self) -> str:
        return LIKELIHOOD_LABELS[self]


LIKELIHOOD_BANDS: list[tuple[float, Likelihood]] = [
    (0.05, Likelihood.ALMOST_NO_CHANCE),
    (0.20, Likelihood.VERY_UNLIKELY),
    (0.45, Likelihood.UNLIKELY),
    (0.55, Likelihood.ROUGHLY_EVEN),
    (0.80, Likelihood.LIKELY),
    (0.95, Likelihood.VERY_LIKELY),
    (1.00, Likelihood.ALMOST_CERTAIN),
]

LIKELIHOOD_LABELS: dict[Likelihood, str] = {
    Likelihood.ALMOST_NO_CHANCE: "почти исключено (01–05%)",
    Likelihood.VERY_UNLIKELY: "очень маловероятно (05–20%)",
    Likelihood.UNLIKELY: "маловероятно (20–45%)",
    Likelihood.ROUGHLY_EVEN: "примерно равные шансы (45–55%)",
    Likelihood.LIKELY: "вероятно (55–80%)",
    Likelihood.VERY_LIKELY: "очень вероятно (80–95%)",
    Likelihood.ALMOST_CERTAIN: "почти наверняка (95–99%)",
}


def likelihood_from_probability(probability: float) -> Likelihood:
    if not 0.0 <= probability <= 1.0:
        raise ValueError("вероятность должна быть в [0, 1]")
    for upper, label in LIKELIHOOD_BANDS:
        if probability <= upper:
            return label
    return Likelihood.ALMOST_CERTAIN


class Computation(Entity):
    """Расчёт в коде: значение считается дважды разными движками."""

    metric: str
    description: str = ""
    spec: dict = Field(default_factory=dict)
    value: float
    unit: str | None = None
    period: str | None = None
    primary_engine: str = "pandas"
    check_engine: str | None = "duckdb"
    check_value: float | None = None
    tolerance: float = 1e-6
    source_id: str | None = None
    cells: list[str] = Field(default_factory=list)
    rows: int | None = (
        None  # строк таблицы в расчёте (n); показывается в отчёте, в текст утверждения не входит
    )
    # Сработавшие правила контракта расчёта (domain/services/metric_contract.IssueKind)
    contract: list[str] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """Расчёт по контракту не годится для отчёта (доля строк агрегированной таблицы) — только реестр."""
        return "row_share_aggregated" in self.contract

    @property
    def consistent(self) -> bool:
        if self.check_value is None:
            return False
        scale = max(1.0, abs(self.value))
        return abs(self.value - self.check_value) <= self.tolerance * scale


class DefeaterKind(StrEnum):
    REBUTTING = "rebutting"  # новые данные противоречат утверждению
    UNDERMINING = "undermining"  # источник ненадёжен
    UNDERCUTTING = "undercutting"  # перестало работать правило вывода


class Defeater(Entity):
    kind: DefeaterKind
    description: str
    evidence_id: str | None = None
    recorded_at: datetime


class Claim(Entity):
    id: str
    text: str = Field(min_length=1)
    type: ClaimType
    evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    support: SupportLabel = SupportLabel.UNVERIFIED
    verification_note: str = ""
    verified_by: str | None = None
    confidence: float | None = None
    probability: float | None = None
    horizon: date | None = None
    resolution_criterion: str | None = None
    falsification_test: str | None = None
    what_would_change: str | None = None
    computation: Computation | None = None
    origin: Origin = Origin.SOURCE_DOCUMENT
    reliability: Reliability = Reliability.LOW
    status: ClaimStatus = ClaimStatus.ACTIVE
    valid_from: date | None = None
    valid_until: date | None = None
    superseded_by: str | None = None
    defeaters: list[Defeater] = Field(default_factory=list)
    load_bearing: bool = False
    vulnerable: bool = False
    topic: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    @field_validator("confidence", "probability")
    @classmethod
    def _unit_interval(cls, value: float | None) -> float | None:
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError("значение должно быть в [0, 1]")
        return value

    @property
    def likelihood(self) -> Likelihood | None:
        return likelihood_from_probability(self.probability) if self.probability is not None else None

    @property
    def is_active(self) -> bool:
        return self.status in (ClaimStatus.ACTIVE, ClaimStatus.SUSPECT)

    @property
    def is_grounded(self) -> bool:
        """Опирается на проверенную опору (факт/метрика с подтверждённой поддержкой)."""
        return self.support in (SupportLabel.SUPPORTED, SupportLabel.PARTIAL) and self.type in (
            ClaimType.FACT,
            ClaimType.DERIVED_METRIC,
        )


class UnknownStatus(StrEnum):
    OPEN = "open"
    RESEARCHING = "researching"
    RESOLVED = "resolved"
    SKIPPED = "skipped"


class Unknown(Entity):
    """Известный пробел: «мы не знаем» — полноценный результат, а не дыра."""

    id: str
    question: str
    why_it_matters: str
    reason: str = ""
    linked_option_ids: list[str] = Field(default_factory=list)
    linked_claim_ids: list[str] = Field(default_factory=list)
    resolution_method: str = ""
    estimated_cost: str | None = None
    deadline: date | None = None
    status: UnknownStatus = UnknownStatus.OPEN
    flips_decision: bool = False
    voi: float | None = None
    saved_tokens_estimate: int | None = None
    resolved_by_claim_ids: list[str] = Field(default_factory=list)
    created_at: datetime
