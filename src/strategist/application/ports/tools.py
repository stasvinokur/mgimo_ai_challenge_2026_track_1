"""Порты инструментов сбора: документы, таблицы, сайты."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from strategist.domain.entities import Locator


class Fragment(BaseModel):
    text: str
    locator: Locator


class ParsedDocument(BaseModel):
    path: str
    title: str = ""
    kind: str = "document"
    fragments: list[Fragment] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return "\n".join(f.text for f in self.fragments)


class DocumentParser(Protocol):
    def supports(self, path: str) -> bool: ...

    def parse(self, path: str) -> ParsedDocument: ...


class TablePassport(BaseModel):
    """Паспорт таблицы: единицы, период и доля пропусков фиксируются до расчётов."""

    path: str
    sheet: str | None = None
    rows: int
    columns: list[str]
    dtypes: dict[str, str] = Field(default_factory=dict)
    units: dict[str, str] = Field(default_factory=dict)
    period: str | None = None
    missing_share: dict[str, float] = Field(default_factory=dict)
    preview: str = ""


class MetricFilter(BaseModel):
    column: str
    op: Literal["==", "!=", ">", ">=", "<", "<=", "in"]
    value: Any


class MetricSpec(BaseModel):
    """Метрика в ограниченном языке — чтобы два движка (pandas и DuckDB) считали её независимо."""

    name: str
    agg: Literal["mean", "sum", "count", "median", "min", "max", "ratio", "share", "sum_share"]
    column: str | None = None
    denominator_column: str | None = None  # для ratio: sum(column)/sum(denominator)
    filters: list[MetricFilter] = Field(default_factory=list)
    group_by: str | None = None
    unit: str | None = None
    description: str = ""


class MetricResult(BaseModel):
    spec: MetricSpec
    value: float | None = None
    by_group: dict[str, float] = Field(default_factory=dict)
    check_value: float | None = None
    check_by_group: dict[str, float] = Field(default_factory=dict)
    primary_engine: str = "pandas"
    check_engine: str = "duckdb"
    consistent: bool = False
    rows_used: int = 0
    cells: list[str] = Field(default_factory=list)


class TableEngine(Protocol):
    def supports(self, path: str) -> bool: ...

    def passport(self, path: str, sheet: str | None = None) -> TablePassport: ...

    def compute(self, path: str, spec: MetricSpec, sheet: str | None = None) -> MetricResult: ...

    def distinct_values(
        self, path: str, column: str, sheet: str | None = None, limit: int = 20
    ) -> list[str]: ...


class WebSnapshot(BaseModel):
    url: str
    final_url: str
    status: int
    fetched_at: datetime
    title: str = ""
    text: str = ""
    sha256: str = ""


class WebFetcher(Protocol):
    def allowed(self, url: str) -> bool: ...

    def fetch(self, url: str) -> WebSnapshot: ...


class ScreenResult(BaseModel):
    """Результат проверки недоверенного текста до передачи модели."""

    text: str
    pii_found: int = 0
    pii_kinds: list[str] = Field(default_factory=list)
    injections: list[str] = Field(default_factory=list)


class ContentGuard(Protocol):
    def screen(self, text: str) -> ScreenResult: ...
