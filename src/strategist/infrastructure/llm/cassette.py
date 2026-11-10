"""Кассеты: запись ответов моделей и воспроизведение прогона без доступа к моделям.

Режим record оборачивает реальный LLM и сохраняет каждый ответ в `<dir>/<task>-<key>.json`.
Режим replay отвечает только из кассет — так жюри может пройти демо без аккаунта Ollama.
Режим update — дозапись: есть ответ в кассете — он воспроизводится, нет — вызывается модель и ответ дописывается.
Так правка одного шага перезаписывает только его вызовы, а остальные шаги демо (и сцены видео) не меняются.
Ключ не зависит от case_id/run_id: одинаковые запросы дают одинаковые ответы.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from strategist.application.errors import StrategistError
from strategist.application.ports import (
    CostRecord,
    DecisionQuestion,
    DecisionResult,
    LLMPort,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)


class ReplayMiss(StrategistError):
    pass


MODES = ("record", "replay", "update")


def cassette_key(request: LLMRequest) -> str:
    payload = {
        "task": request.task,
        "system": request.system,
        "messages": [m.model_dump() for m in request.messages],
        "untrusted": [b.model_dump() for b in request.untrusted],
        "schema": request.schema_name,
        "escalation": request.escalation,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class CassetteLLM:
    def __init__(
        self, directory: Path, mode: str, inner: LLMPort | None = None, costs: Any = None, clock: Any = None
    ) -> None:
        if mode not in MODES:
            raise ValueError("mode: record | replay | update")
        if mode != "replay" and inner is None:
            raise ValueError("для записи нужен реальный LLM")
        self.directory = Path(directory)
        self.mode = mode
        self.inner = inner
        self.costs = costs
        self.clock = clock
        self.used: set[str] = set()  # имена файлов кассет, к которым обращался прогон
        self.recorded = 0  # сколько ответов получено от модели и записано
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, request: LLMRequest) -> Path:
        return self.directory / f"{request.task}-{cassette_key(request)}.json"

    def complete(self, request: LLMRequest) -> LLMResponse:
        path = self._path(request)
        self.used.add(path.name)
        if self.mode == "replay" or (self.mode == "update" and path.exists()):
            if not path.exists():
                raise ReplayMiss(
                    f"Нет записанного ответа для шага {request.task} ({path.name}). Запустите прогон с моделями "
                    "(`strategist demo --record`) или проверьте, что материалы кейса не менялись."
                )
            stored: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            recorded = LLMUsage.model_validate(stored["usage"])
            usage = recorded.model_copy(update={"latency_ms": 0})
            if self.costs is not None and self.clock is not None and request.case_id:
                # replay — бесплатное воспроизведение; update — прогон, который станет записанным демо:
                # ответ из кассеты учитывается как вызов модели (те же модель и токены), чтобы чек стоимости
                # показывал весь прогон, а не только дописанные вызовы
                live_like = self.mode == "update"
                self.costs.record(
                    CostRecord(
                        case_id=request.case_id,
                        run_id=request.run_id or "",
                        step=request.step or request.task,
                        task=request.task,
                        backend="cassette" if live_like else "replay",
                        model=recorded.model,
                        input_tokens=recorded.input_tokens,
                        output_tokens=recorded.output_tokens,
                        latency_ms=recorded.latency_ms if live_like else 0,
                        usd=0.0,
                        usd_estimated=False,
                        at=self.clock.now(),
                        attributes={"strategist.replay": True},
                    )
                )
                if live_like:
                    return LLMResponse(text=stored["text"], data=stored.get("data"), usage=recorded)
            return LLMResponse(text=stored["text"], data=stored.get("data"), usage=usage)
        assert self.inner is not None
        response = self.inner.complete(request)
        self.recorded += 1
        path.write_text(
            json.dumps(
                {
                    "task": request.task,
                    "schema": request.schema_name,
                    "text": response.text,
                    "data": response.data,
                    "usage": response.usage.model_dump(),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        return response


def _hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class CassetteDecision:
    """Запись и воспроизведение ответов decide() (System One или запасной LLM)."""

    def __init__(
        self, directory: Path, mode: str, inner: Any | None = None, costs: Any = None, clock: Any = None
    ) -> None:
        if mode not in MODES:
            raise ValueError("mode: record | replay | update")
        self.costs = costs
        self.clock = clock
        self.directory = Path(directory) / "decide"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.inner = inner
        self.used: set[str] = set()
        self.recorded = 0

    def _path(self, question: DecisionQuestion) -> Path:
        key = _hash(
            {
                "kind": question.kind.value,
                "instructions": question.instructions,
                "state": question.state,
                "options": question.options,
                "levels": question.levels,
            }
        )
        return self.directory / f"{question.task}-{key}.json"

    def decide(self, question: DecisionQuestion) -> DecisionResult:
        path = self._path(question)
        self.used.add(path.name)
        if self.mode == "replay" or (self.mode == "update" and path.exists()):
            if not path.exists():
                raise ReplayMiss(f"Нет записанного решения decide() для {question.task} ({path.name})")
            stored = DecisionResult.model_validate_json(path.read_text(encoding="utf-8"))
            if (
                self.mode == "update"
                and self.costs is not None
                and self.clock is not None
                and question.case_id
            ):
                self.costs.record(  # как у live-вызова decide(): та же модель и токены (см. CassetteLLM)
                    CostRecord(
                        case_id=question.case_id,
                        run_id=question.run_id or "",
                        step=question.step or question.task,
                        task=question.task,
                        backend=stored.backend or "cassette",
                        model=stored.model,
                        input_tokens=stored.input_tokens,
                        output_tokens=0,
                        latency_ms=stored.latency_ms,
                        at=self.clock.now(),
                        attributes={"gen_ai.operation.name": "evaluate", "strategist.replay": True},
                    )
                )
                return stored.model_copy(update={"question_id": question.id})
            return stored.model_copy(update={"question_id": question.id, "latency_ms": 0})
        assert self.inner is not None
        result = self.inner.decide(question)
        self.recorded += 1
        path.write_text(result.model_dump_json(indent=1), encoding="utf-8")
        return result

    def decide_many(self, questions: list[DecisionQuestion]) -> list[DecisionResult]:
        if self.mode == "replay" or self.inner is None:
            return [self.decide(q) for q in questions]
        if self.mode == "update" and all(self._path(q).exists() for q in questions):
            return [self.decide(q) for q in questions]
        # Пакет вызывается целиком (как при записи): порядок и состав пакета влияют на ответ модели
        results = self.inner.decide_many(questions)
        for q, r in zip(questions, results, strict=True):
            self.used.add(self._path(q).name)
            self._path(q).write_text(r.model_dump_json(indent=1), encoding="utf-8")
        self.recorded += len(questions)
        return results


class CassetteEmbeddings:
    """Эмбеддинги из записи: один JSON-файл {хеш текста: вектор}."""

    def __init__(self, directory: Path, mode: str, inner: Any | None = None) -> None:
        self.path = Path(directory) / "embeddings.json"
        self.mode = mode
        self.inner = inner
        self.store: dict[str, list[float]] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        keys = [_hash(t) for t in texts]
        missing = [t for t, k in zip(texts, keys, strict=True) if k not in self.store]
        if missing:
            if self.mode == "replay" or self.inner is None:
                raise ReplayMiss(f"Нет записанных эмбеддингов для {len(missing)} текстов")
            for text, vec in zip(missing, self.inner.embed(missing), strict=True):
                self.store[_hash(text)] = vec
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.store), encoding="utf-8")
        return [self.store[k] for k in keys]
