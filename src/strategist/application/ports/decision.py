"""Порт decide(): «умный if» для узких частых решений (Choice / Noul / Score в терминах Jev)."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class DecisionKind(StrEnum):
    CHOICE = "choice"  # выбрать один вариант из перечня
    NOUL = "noul"  # вероятность ответа «да»
    SCORE = "score"  # уровень по упорядоченной шкале


class DecisionQuestion(BaseModel):
    id: str
    kind: DecisionKind
    instructions: str
    state: str = ""
    options: dict[str, str] = Field(default_factory=dict)  # имя → описание (choice)
    levels: list[str] = Field(default_factory=list)  # по возрастанию (score)
    task: str = "decide"
    case_id: str | None = None
    run_id: str | None = None
    step: str | None = None


class DecisionResult(BaseModel):
    question_id: str
    kind: DecisionKind
    choice: str | None = None
    probability: float | None = None  # P(да) для noul
    score: float | None = None  # дробный уровень для score (0 = первый уровень)
    probs: dict[str, float] = Field(default_factory=dict)
    confidence: float = 0.0
    backend: str = ""
    model: str = ""
    escalated: bool = False
    input_tokens: int = 0
    latency_ms: int = 0

    @property
    def yes(self) -> bool:
        return (self.probability or 0.0) >= 0.5


class DecisionPort(Protocol):
    def decide(self, question: DecisionQuestion) -> DecisionResult: ...

    def decide_many(self, questions: list[DecisionQuestion]) -> list[DecisionResult]: ...
