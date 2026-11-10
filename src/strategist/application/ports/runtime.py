"""Порты среды выполнения: промпты, стоимость, журнал шагов, шлюзы человека, события, часы."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

# --- Промпты и навыки --------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


def _fill(text: str, values: dict[str, Any]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


class PromptTemplate(BaseModel):
    name: str
    version: str
    system: str
    user: str = ""

    def placeholders(self) -> set[str]:
        """Имена {{…}} в обеих частях шаблона (до подстановки: в значениях могут быть свои скобки)."""
        return set(_PLACEHOLDER.findall(self.system + "\n" + self.user))

    def render_system(self, **values: Any) -> str:
        return _fill(self.system, values)

    def render_user(self, **values: Any) -> str:
        return _fill(self.user, values)


class PromptRepository(Protocol):
    def get(self, name: str) -> PromptTemplate: ...

    def skill(self, name: str) -> str: ...

    def names(self) -> list[str]: ...


# --- Стоимость ---------------------------------------------------------------


class CostRecord(BaseModel):
    """Атрибуты по OpenTelemetry GenAI: gen_ai.request.model, gen_ai.usage.input_tokens и т. д."""

    case_id: str
    run_id: str = ""
    step: str = ""
    task: str = ""
    backend: str = "ollama"
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_estimated: bool = False
    usd: float = 0.0
    usd_estimated: bool = True
    rub: float = 0.0
    latency_ms: int = 0
    at: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


class HumanTimeRecord(BaseModel):
    case_id: str
    minutes: float
    note: str = ""
    at: datetime


class CostSummary(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_tokens_calls: int = 0
    usd: float = 0.0
    rub: float = 0.0
    usd_estimated: bool = True
    subscription_share_usd: float = 0.0
    human_minutes: float = 0.0
    human_rub: float = 0.0
    total_rub: float = 0.0
    model_latency_seconds: float = 0.0
    by_step: dict[str, dict[str, float]] = Field(default_factory=dict)
    by_model: dict[str, dict[str, float]] = Field(default_factory=dict)


class CostLedger(Protocol):
    def record(self, record: CostRecord) -> None: ...

    def record_human(self, record: HumanTimeRecord) -> None: ...

    def records(self, case_id: str) -> list[CostRecord]: ...

    def summary(self, case_id: str) -> CostSummary: ...


# --- Журнал шагов (идемпотентность) ------------------------------------------


class JournalStatus(StrEnum):
    INTENT = "intent"
    DONE = "done"
    FAILED = "failed"


class JournalEntry(BaseModel):
    case_id: str
    run_id: str
    step: str
    status: JournalStatus
    input_hash: str
    output_hash: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    side_effect: bool = False
    error: str = ""
    at: datetime
    attempt: int = 1


class JournalPort(Protocol):
    def get(self, case_id: str, run_id: str, step: str) -> JournalEntry | None: ...

    def intent(
        self, case_id: str, run_id: str, step: str, input_hash: str, *, side_effect: bool, at: datetime
    ) -> JournalEntry: ...

    def done(
        self, case_id: str, run_id: str, step: str, output_hash: str, payload: dict[str, Any], at: datetime
    ) -> JournalEntry: ...

    def fail(self, case_id: str, run_id: str, step: str, error: str, at: datetime) -> JournalEntry: ...

    def entries(self, case_id: str, run_id: str | None = None) -> list[JournalEntry]: ...


# --- Шлюзы человека ----------------------------------------------------------


class GateKind(StrEnum):
    CLARIFY = "clarify"
    RESEARCH_PLAN = "research_plan"
    OPTION_CHOICE = "option_choice"
    DECISION_COMMIT = "decision_commit"
    SIDE_EFFECT = "side_effect"


class GateAction(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


class GateRequest(BaseModel):
    gate: GateKind
    case_id: str
    title: str
    summary: str = ""
    choices: list[str] = Field(default_factory=list)
    default_choice: str | None = None
    questions: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class GateResponse(BaseModel):
    action: GateAction = GateAction.APPROVE
    choice: str | None = None
    answers: list[str] = Field(default_factory=list)
    edits: dict[str, Any] = Field(default_factory=dict)
    comment: str = ""
    approver: str = ""


class HumanGate(Protocol):
    def ask(self, request: GateRequest) -> GateResponse: ...


# --- События (хуки) ----------------------------------------------------------


class Event(BaseModel):
    type: str
    case_id: str = ""
    at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class EventBus(Protocol):
    def publish(self, event: Event) -> None: ...

    def subscribe(self, event_type: str, handler: Any) -> None: ...


# --- Артефакты (отчёты, выгрузки) ---------------------------------------------


class ArtifactWriter(Protocol):
    """Запись артефактов кейса: out/ (отчёты) и state/ (выгрузки вроде forecasts.jsonl)."""

    def write(self, case_id: str, rel_path: str, content: str) -> str: ...

    def read(self, case_id: str, rel_path: str) -> str | None: ...


class DocumentRenderer(Protocol):
    """Markdown → HTML (сама библиотека разметки — деталь инфраструктуры)."""

    def to_html(self, markdown_text: str, title: str) -> str: ...


# --- Часы --------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> datetime: ...

    def today(self) -> date: ...
