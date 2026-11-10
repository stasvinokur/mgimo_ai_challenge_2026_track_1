"""Продуктовая и коммуникационная стратегии и обещания."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from strategist.domain.common import Entity


class Segment(Entity):
    id: str
    name: str
    description: str = ""


class FeatureStatus(StrEnum):
    EXISTING = "existing"
    PLANNED = "planned"
    HYPOTHETICAL = "hypothetical"

    @property
    def label_ru(self) -> str:
        return {"existing": "есть в продукте", "planned": "в плане", "hypothetical": "гипотеза"}[self.value]


class Feature(Entity):
    id: str
    name: str
    description: str = ""
    status: FeatureStatus = FeatureStatus.EXISTING
    claim_ids: list[str] = Field(default_factory=list)


class ActionStep(Entity):
    """Шаг плана: когда, что, кто и как поймём, что получилось — то, что руководитель ставит команде."""

    when: str
    action: str
    owner: str = ""
    metric: str = ""


class SegmentMessage(Entity):
    segment_id: str | None = None
    message: str
    channel: str = ""
    timing: str = ""


class ProductStrategy(Entity):
    option_id: str
    audience: str
    segments: list[Segment] = Field(default_factory=list)
    segment_ids: list[str] = Field(default_factory=list)
    job_to_be_done: str
    alternatives: list[str] = Field(default_factory=list)
    direction: str
    conscious_refusals: list[str] = Field(default_factory=list)
    value_logic: str = ""
    costs: str = ""
    revenue: str = ""
    features: list[Feature] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    action_plan: list[ActionStep] = Field(default_factory=list)


class BarrierType(StrEnum):
    UNDERSTANDING = "understanding"
    CHOICE = "choice"
    TRUST = "trust"
    HABIT = "habit_switching_cost"

    @property
    def label_ru(self) -> str:
        return {
            "understanding": "понимание",
            "choice": "выбор",
            "trust": "доверие",
            "habit_switching_cost": "привычка и цена переключения",
        }[self.value]


class ChannelRoleKind(StrEnum):
    AWARENESS = "awareness"
    EXPLANATION = "explanation"
    CONVERSION = "conversion"
    RETENTION = "retention"

    @property
    def label_ru(self) -> str:
        return {
            "awareness": "узнавание",
            "explanation": "объяснение",
            "conversion": "конверсия",
            "retention": "удержание",
        }[self.value]


class PromiseLocation(StrEnum):
    KEY_MESSAGE = "key_message"
    PRESS_RELEASE = "press_release"
    CHANNEL = "channel"
    SEGMENT_MESSAGE = "segment_message"


class PromiseStatus(StrEnum):
    UNCHECKED = "unchecked"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    SEGMENT_MISMATCH = "segment_mismatch"
    SYNTHETIC_ONLY = "synthetic_only"

    @property
    def label_ru(self) -> str:
        return {
            "unchecked": "не проверено",
            "supported": "подтверждено продуктом",
            "unsupported": "продукт не обеспечивает",
            "segment_mismatch": "не тот сегмент",
            "synthetic_only": "опора только на синтетику",
        }[self.value]


class Promise(Entity):
    """Обещание аудитории. Должно опираться на функцию продукта и строку реестра."""

    id: str
    text: str
    location: PromiseLocation
    segment_id: str | None = None
    feature_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    status: PromiseStatus = PromiseStatus.UNCHECKED
    issue: str = ""


class ChannelRole(Entity):
    channel: str
    role: ChannelRoleKind
    message: str = ""
    promise_ids: list[str] = Field(default_factory=list)


class CommsStrategy(Entity):
    audience: str
    segment_id: str | None = None
    situation: str
    barrier: BarrierType
    barrier_explanation: str = ""
    key_message: str
    proof_claim_ids: list[str] = Field(default_factory=list)
    channels: list[ChannelRole] = Field(default_factory=list)
    press_release: str = ""
    promises: list[Promise] = Field(default_factory=list)
    revision_note: str = ""
    segment_messages: list[SegmentMessage] = Field(default_factory=list)
    timeline: list[ActionStep] = Field(default_factory=list)
