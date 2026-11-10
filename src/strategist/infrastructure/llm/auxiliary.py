"""Вспомогательные модели: эмбеддинги Ollama и NLI-проверяющий на дешёвой модели."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from strategist.application.ports import LLMMessage, LLMPort, LLMRequest, NLILabel, NLIResult
from strategist.infrastructure.llm.ollama_client import OllamaClient


class OllamaEmbeddings:
    """Эмбеддинги с дисковым кэшем по хешу текста (повторный индекс не тратит вызовы)."""

    def __init__(self, client: OllamaClient, model: str, cache_path: Path | None = None) -> None:
        self.client = client
        self.model = model
        self.cache_path = cache_path
        self._cache: dict[str, list[float]] = {}
        if cache_path and cache_path.exists():
            self._cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}:{text}".encode()).hexdigest()[:24]

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in texts if self._key(t) not in self._cache]
        for start in range(0, len(missing), 32):
            batch = missing[start : start + 32]
            vectors, _ = self.client.embed(self.model, batch)
            for text, vec in zip(batch, vectors, strict=True):
                self._cache[self._key(text)] = vec
        if missing and self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache), encoding="utf-8")
        return [self._cache[self._key(t)] for t in texts]


class _NLIAnswer(BaseModel):
    label: str = Field(description="entailment | neutral | contradiction")
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(default="", description="одно короткое предложение")


NLI_SYSTEM = (
    "Ты проверяешь, подтверждает ли цитата утверждение. entailment — цитата прямо подтверждает "
    "утверждение целиком (числа, период, объект совпадают); contradiction — цитата прямо противоречит; "
    "neutral — не подтверждает и не опровергает или подтверждает лишь часть. Не используй внешние знания. "
    "Ответь JSON."
)


class LLMNLI:
    """Дешёвая проверка пары «цитата → утверждение» (вместо отдельной NLI-модели на русском)."""

    def __init__(self, llm: LLMPort, task: str = "verify_claim") -> None:
        self.llm = llm
        self.task = task

    def check(self, premise: str, hypothesis: str, *, case_id: str | None = None) -> NLIResult:
        request = LLMRequest(
            task=self.task,
            system=NLI_SYSTEM,
            messages=[
                LLMMessage(
                    role="user",
                    content=f"ЦИТАТА:\n{premise}\n\nУТВЕРЖДЕНИЕ:\n{hypothesis}\n\nПодтверждает ли цитата утверждение?",
                )
            ],
            json_schema=_NLIAnswer.model_json_schema(),
            schema_name="_NLIAnswer",
            temperature=0.0,
            case_id=case_id,
            step="verify_claim",
        )
        response = self.llm.complete(request)
        data: Any = response.data or {}
        try:
            answer = _NLIAnswer.model_validate(data)
            label = (
                NLILabel(answer.label) if answer.label in NLILabel._value2member_map_ else NLILabel.NEUTRAL
            )
            return NLIResult(
                label=label, score=answer.confidence, model=response.usage.model, rationale=answer.rationale
            )
        except Exception:
            return NLIResult(
                label=NLILabel.NEUTRAL, score=0.0, model=response.usage.model, rationale="невалидный ответ"
            )
