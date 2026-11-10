"""Порт базы знаний: поиск по формулировке, смыслу и связям; память с происхождением записей."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from strategist.domain.entities import Origin


class KnowledgeDoc(BaseModel):
    id: str
    text: str
    title: str = ""
    kind: str = "material"  # methodology | material | memory
    case_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SearchHit(BaseModel):
    doc_id: str
    title: str = ""
    kind: str = ""
    snippet: str = ""
    score: float = 0.0
    ranks: dict[str, int] = Field(default_factory=dict)


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    CORRECTED = "corrected"
    RETRACTED = "retracted"


class MemoryRecord(BaseModel):
    """Запись памяти между сеансами. origin=agent_inference — не доказательство."""

    id: str
    case_id: str | None = None
    text: str
    origin: Origin
    epistemic_status: str = "unverified"  # verified | unverified | disputed
    valid_from: date | None = None
    valid_until: date | None = None
    recorded_at: datetime
    status: MemoryStatus = MemoryStatus.ACTIVE
    superseded_by: str | None = None
    approved_by_user: bool = False
    approved_at: datetime | None = None
    reason: str = ""

    @property
    def can_be_evidence(self) -> bool:
        if self.status != MemoryStatus.ACTIVE:
            return False
        if self.origin == Origin.AGENT_INFERENCE:
            return self.approved_by_user
        return True


class KnowledgeIndex(Protocol):
    def upsert(self, docs: list[KnowledgeDoc]) -> None: ...

    def search(
        self, query: str, k: int = 8, kinds: list[str] | None = None, case_id: str | None = None
    ) -> list[SearchHit]: ...

    def link(self, src_id: str, dst_id: str, relation: str) -> None: ...

    def neighbors(self, doc_id: str, relation: str | None = None) -> list[str]: ...

    def remember(self, record: MemoryRecord) -> None: ...

    def memories(self, case_id: str | None = None, include_inactive: bool = False) -> list[MemoryRecord]: ...

    def retract(self, memory_id: str, reason: str, at: datetime) -> None: ...

    def correct(self, memory_id: str, new_record: MemoryRecord) -> None: ...

    def approve(self, memory_id: str, at: datetime) -> None: ...
