"""Источники и доказательства: откуда агент знает то, что знает."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import Field

from strategist.domain.common import Entity


class SourceKind(StrEnum):
    DOCUMENT = "document"
    TABLE = "table"
    WEB = "web"
    INTERVIEW = "interview"
    NOTE = "note"
    USER_INPUT = "user_input"


class Origin(StrEnum):
    """Происхождение сведения. Прежний вывод агента — не факт."""

    USER_STATED = "user_stated"
    SOURCE_DOCUMENT = "source_document"
    EXTERNAL_DATA = "external_data"
    AGENT_INFERENCE = "agent_inference"
    AGENT_OUTPUT_ACCEPTED_BY_USER = "agent_output_accepted_by_user"


class Reliability(StrEnum):
    """Надёжность по мотивам GRADE: собственные данные с пересчётом выше пресс-релиза,
    вывод модели без источника — ниже всего."""

    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    VERY_LOW = "very_low"

    @property
    def weight(self) -> float:
        return {"high": 1.0, "moderate": 0.75, "low": 0.5, "very_low": 0.25}[self.value]

    @property
    def label_ru(self) -> str:
        return {"high": "высокая", "moderate": "умеренная", "low": "низкая", "very_low": "очень низкая"}[
            self.value
        ]


class Source(Entity):
    id: str
    title: str
    kind: SourceKind
    path: str | None = None
    url: str | None = None
    publisher: str | None = None
    published: date | None = None
    retrieved: date | None = None
    sha256: str | None = None
    synthetic: bool = False
    # simulated_data — смоделированные внутренние данные (демо/стенд); model_respondents — ИИ-персоны.
    synthetic_kind: str | None = None
    reliability: Reliability = Reliability.LOW
    added_at: datetime
    superseded_by: str | None = None
    retracted: bool = False
    retraction_reason: str | None = None
    instructions_detected: list[str] = Field(default_factory=list)
    pii_masked: int = 0
    ingested: bool = False
    note: str = ""

    @property
    def is_valid(self) -> bool:
        return not self.retracted and self.superseded_by is None

    @property
    def is_model_respondents(self) -> bool:
        """Ответы модельных респондентов: гипотезы, никогда не доказательство спроса."""
        return self.synthetic and self.synthetic_kind == "model_respondents"

    @property
    def address(self) -> str:
        return self.url or self.path or self.id


class Locator(Entity):
    """Точный адрес фрагмента: файл+страница/абзац/строка/ячейка или URL."""

    page: int | None = None
    paragraph: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    sheet: str | None = None
    cell: str | None = None
    url: str | None = None
    char_start: int | None = None
    char_end: int | None = None

    def render(self) -> str:
        parts: list[str] = []
        if self.page is not None:
            parts.append(f"стр. {self.page}")
        if self.paragraph is not None:
            parts.append(f"абз. {self.paragraph}")
        if self.line_start is not None:
            end = f"–{self.line_end}" if self.line_end and self.line_end != self.line_start else ""
            parts.append(f"стр. текста {self.line_start}{end}")
        if self.sheet:
            parts.append(f"лист «{self.sheet}»")
        if self.cell:
            parts.append(f"ячейка {self.cell}")
        if self.url:
            parts.append(self.url)
        return ", ".join(parts) or "весь документ"


class Evidence(Entity):
    """Дословная цитата из источника. Утверждения пишутся из цитат, а не наоборот."""

    id: str
    source_id: str
    quote: str = Field(min_length=1)
    locator: Locator = Field(default_factory=Locator)
    extracted_at: datetime
    origin: Origin = Origin.SOURCE_DOCUMENT
    verified_in_source: bool = True
    topic: str | None = None
