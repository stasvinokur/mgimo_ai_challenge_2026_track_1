"""Порты хранения: кейс (версии), реестр (история строк), материалы (неизменяемые, с хешами)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from strategist.domain.entities import DecisionCase


class CaseSummary(BaseModel):
    id: str
    title: str
    stage: str
    version: int
    updated_at: datetime


class CaseVersion(BaseModel):
    version: int
    at: datetime
    message: str


class CaseRepository(Protocol):
    def exists(self, case_id: str) -> bool: ...

    def create(self, case: DecisionCase) -> None: ...

    def load(self, case_id: str, version: int | None = None) -> DecisionCase: ...

    def save(self, case: DecisionCase, message: str) -> int: ...

    def versions(self, case_id: str) -> list[CaseVersion]: ...

    def list_cases(self) -> list[CaseSummary]: ...


class LedgerEntry(BaseModel):
    case_id: str
    claim_id: str
    action: str  # created | verified | revised | retracted | superseded | suspect
    at: datetime
    actor: str = ""
    snapshot: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class LedgerRepository(Protocol):
    def append(self, entries: list[LedgerEntry]) -> None: ...

    def history(self, case_id: str, claim_id: str | None = None) -> list[LedgerEntry]: ...


class MaterialFile(BaseModel):
    """Файл материалов кейса. Путь — относительно каталога материалов."""

    path: str
    sha256: str
    size: int
    title: str
    kind: str = "document"
    synthetic: bool = False
    source_url: str | None = None
    publisher: str | None = None
    published: date | None = None
    retrieved: date | None = None
    note: str = ""
    added_at: datetime
    meta: dict[str, Any] = Field(default_factory=dict)


class MaterialStore(Protocol):
    def add_file(self, case_id: str, src_path: str, *, subdir: str = "") -> MaterialFile: ...

    def add_bytes(self, case_id: str, name: str, data: bytes, meta: dict[str, Any]) -> MaterialFile: ...

    def list(self, case_id: str) -> list[MaterialFile]: ...

    def verify(self, case_id: str) -> list[str]: ...

    def absolute_path(self, case_id: str, rel_path: str) -> str: ...
