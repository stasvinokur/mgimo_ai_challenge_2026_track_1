"""Панель модельных респондентов — на карантине: генератор гипотез и вопросов для реальных интервью.

Ни один ответ персоны не становится строкой реестра: панель хранится отдельно, с меткой СИНТЕТИКА, и в отчёт
попадают разногласия и вопросы для интервью, а не средние оценки.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from strategist.domain.common import Entity

SYNTHETIC_LABEL = "СИНТЕТИКА — гипотеза, не доказательство"
PLACEBO_ID = "PLACEBO"
PLACEBO_CONCEPT = (
    "Тот же сервис, что у вас уже есть, но с новым названием и обновлённым цветом интерфейса. "
    "Функции, цена и условия не меняются."
)


class Persona(Entity):
    id: str
    name: str  # вымышленное, без персональных данных
    profile: str
    segment: str = ""
    tech_attitude: str = ""  # low | medium | high
    current_alternative: str = ""
    label: str = SYNTHETIC_LABEL


class PersonaAnswer(Entity):
    persona_id: str
    concept_id: str
    reaction: str
    concern: str = ""
    model_score: float | None = None  # оценка, которую назвала модель (для сравнения, не для вывода)
    score: float  # SSR по тексту реакции, если доступны эмбеддинги, иначе оценка модели
    score_method: str = "ssr"
    label: str = SYNTHETIC_LABEL


class FramingAnswer(Entity):
    """Согласие персоны с позитивной и негативной формулировкой одного утверждения о концепте (1–5)."""

    persona_id: str
    concept_id: str
    positive: float
    negative: float


class Disagreement(Entity):
    concept_id: str
    spread: float
    supporters: list[str] = Field(default_factory=list)
    opponents: list[str] = Field(default_factory=list)
    reasons_for: list[str] = Field(default_factory=list)
    reasons_against: list[str] = Field(default_factory=list)


class InterviewQuestion(Entity):
    question: str
    concept_id: str | None = None
    checks: str = ""
    issues: list[str] = Field(default_factory=list)
    suggestion: str = ""


class PanelRun(Entity):
    id: str
    at: datetime
    personas: list[Persona] = Field(default_factory=list)
    concepts: dict[str, str] = Field(default_factory=dict)  # ID варианта → название; PLACEBO — плацебо
    answers: list[PersonaAnswer] = Field(default_factory=list)
    framing: list[FramingAnswer] = Field(default_factory=list)
    trust: str = "normal"  # normal | low
    placebo_liked: bool = False
    framing_sensitive: list[str] = Field(default_factory=list)
    spread_warning: str = ""
    disagreements: list[Disagreement] = Field(default_factory=list)
    interview_questions: list[InterviewQuestion] = Field(default_factory=list)
    label: str = SYNTHETIC_LABEL
