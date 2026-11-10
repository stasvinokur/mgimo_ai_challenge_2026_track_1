"""Порты вспомогательных моделей: эмбеддинги и проверка «цитата → утверждение» (NLI)."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel


class EmbeddingPort(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class NLILabel(StrEnum):
    ENTAILMENT = "entailment"
    NEUTRAL = "neutral"
    CONTRADICTION = "contradiction"


class NLIResult(BaseModel):
    label: NLILabel
    score: float
    model: str = ""
    rationale: str = ""


class NLIPort(Protocol):
    def check(self, premise: str, hypothesis: str, *, case_id: str | None = None) -> NLIResult: ...
