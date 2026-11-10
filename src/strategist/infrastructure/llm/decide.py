"""Адаптеры DecisionPort: System One (Clef в Ollama Cloud, Jev у TypeSafe) и запасной вариант на LLM.

Протокол System One одинаков для Clef и Jev: POST {base}/systemone с телом
{"model", "state", "questions": {id: {"type": "choice"|"noul"|"score", "instructions", "criteria"}}};
ответ: answers[id] = {choice, confidence, probabilities} | {noul} | {score, confidence, probabilities}.
Ниже порога уверенности ответ эскалируется на сильную LLM, а событие пишется как «неверный маршрут».
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from pydantic import BaseModel, Field

from strategist.application.ports import (
    Clock,
    CostLedger,
    CostRecord,
    DecisionKind,
    DecisionQuestion,
    DecisionResult,
    Event,
    EventBus,
    LLMMessage,
    LLMPort,
    LLMRequest,
)


class SystemOneError(Exception):
    pass


NOUL_CRITERIA = {"true": "да, верно", "false": "нет, неверно"}


def to_systemone(question: DecisionQuestion) -> dict[str, Any]:
    q: dict[str, Any] = {"type": question.kind.value, "instructions": question.instructions}
    if question.kind == DecisionKind.CHOICE:
        q["criteria"] = question.options
    elif question.kind == DecisionKind.SCORE:
        q["criteria"] = question.levels
    else:
        q["criteria"] = question.options or NOUL_CRITERIA  # пояснения {"true": …, "false": …}
    return q


def parse_answer(question: DecisionQuestion, answer: dict[str, Any]) -> DecisionResult:
    result = DecisionResult(question_id=question.id, kind=question.kind)
    if question.kind == DecisionKind.NOUL:
        p = float(answer.get("noul", answer.get("probability", 0.5)))
        result.probability = p
        result.probs = {"да": p, "нет": 1 - p}
        result.confidence = max(p, 1 - p)
    elif question.kind == DecisionKind.CHOICE:
        result.choice = answer.get("choice")
        result.probs = {k: float(v) for k, v in (answer.get("probabilities") or {}).items()}
        result.confidence = result.probs.get(result.choice or "", float(answer.get("confidence") or 0.0))
    else:
        result.score = float(answer.get("score", 0.0))
        result.probs = {k: float(v) for k, v in (answer.get("probabilities") or {}).items()}
        # Поле confidence у System One — концентрация распределения, а не вероятность правоты.
        if result.probs:
            result.confidence = max(result.probs.values())
        else:
            result.confidence = float(answer.get("confidence") or 0.0)
    return result


class SystemOneDecision:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        costs: CostLedger | None,
        events: EventBus,
        clock: Clock,
        backend_name: str = "systemone",
        threshold: float = 0.7,
        fallback: Any | None = None,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        max_workers: int = 6,
        consistency_tasks: tuple[str, ...] = (),
        score_threshold: float = 0.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )
        self.model = model
        self.costs = costs
        self.events = events
        self.clock = clock
        self.backend_name = backend_name
        self.threshold = threshold
        self.fallback = fallback
        self.max_workers = max_workers
        self.consistency_tasks = set(consistency_tasks)
        self.score_threshold = score_threshold

    @staticmethod
    def _variant(question: DecisionQuestion) -> dict[str, Any] | None:
        """Проверочный вариант того же вопроса в том же запросе: отрицание (noul) или перестановка (choice)."""
        if question.kind == DecisionKind.NOUL:
            # Отрицание через перестановку смысла критериев (TASK-3: согласовано 24 из 24 у Clef).
            criteria = question.options or NOUL_CRITERIA
            return {
                "type": "noul",
                "instructions": question.instructions,
                "criteria": {"true": criteria["false"], "false": criteria["true"]},
            }
        if question.kind == DecisionKind.CHOICE and len(question.options) > 1:
            return {
                "type": "choice",
                "instructions": question.instructions,
                "criteria": dict(reversed(list(question.options.items()))),
            }
        return None

    def _call(self, question: DecisionQuestion) -> tuple[DecisionResult, int, int]:
        questions: dict[str, Any] = {"q": to_systemone(question)}
        variant = self._variant(question) if question.task in self.consistency_tasks else None
        if variant is not None:
            questions["v"] = variant
        body = {"model": self.model, "state": question.state, "questions": questions}
        started = time.perf_counter()
        last: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.post("/systemone", json=body)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise SystemOneError(f"HTTP {response.status_code}")
                response.raise_for_status()
                data = response.json()
                break
            except (httpx.HTTPError, SystemOneError, ValueError) as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
        else:
            raise SystemOneError(f"{self.model}: {last}")
        latency = int((time.perf_counter() - started) * 1000)
        answers = data.get("answers") or {}
        result = parse_answer(question, answers.get("q") or {})
        if variant is not None and "v" in answers:
            check = parse_answer(question, answers["v"])
            if question.kind == DecisionKind.NOUL:
                consistent = ((result.probability or 0) >= 0.5 and (check.probability or 0) < 0.5) or (
                    (result.probability or 0) < 0.5 and (check.probability or 0) >= 0.5
                )
            else:
                consistent = check.choice == result.choice
            if not consistent:
                result.confidence = 0.0  # расхождение вариантов — сигнал для эскалации
        usage = data.get("usage") or {}
        return result, int(usage.get("input_tokens") or 0), latency

    def decide(self, question: DecisionQuestion) -> DecisionResult:
        try:
            result, tokens, latency = self._call(question)
        except SystemOneError as exc:
            self._emit("route.fallback", question, error=str(exc)[:200])
            if self.fallback is None:
                raise
            return self.fallback.decide(question)
        result.backend = self.backend_name
        result.model = self.model
        result.input_tokens = tokens
        result.latency_ms = latency
        if self.costs is not None and question.case_id:
            self.costs.record(
                CostRecord(
                    case_id=question.case_id,
                    run_id=question.run_id or "",
                    step=question.step or question.task,
                    task=question.task,
                    backend=self.backend_name,
                    model=self.model,
                    input_tokens=tokens,
                    output_tokens=0,
                    latency_ms=latency,
                    at=self.clock.now(),
                    attributes={
                        "gen_ai.operation.name": "evaluate",
                        "strategist.question_kind": question.kind.value,
                    },
                )
            )
        threshold = self.score_threshold if question.kind == DecisionKind.SCORE else self.threshold
        if result.confidence < threshold and self.fallback is not None:
            self._emit("route.misrouted", question, confidence=result.confidence, backend=self.backend_name)
            escalated = self.fallback.decide(question)
            escalated.escalated = True
            return escalated
        return result

    def decide_many(self, questions: list[DecisionQuestion]) -> list[DecisionResult]:
        if len(questions) <= 1:
            return [self.decide(q) for q in questions]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(self.decide, questions))

    def _emit(self, event_type: str, question: DecisionQuestion, **data: Any) -> None:
        self.events.publish(
            Event(
                type=event_type,
                case_id=question.case_id or "",
                at=self.clock.now(),
                data={"question": question.id, "task": question.task, **data},
            )
        )


class _ChoiceAnswer(BaseModel):
    choice: str
    confidence: float = Field(ge=0, le=1)


class _NoulAnswer(BaseModel):
    probability_yes: float = Field(ge=0, le=1)


class _ScoreAnswer(BaseModel):
    level: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)


class LLMDecision:
    """Запасной decide(): LLM с выводом из перечня и самооценкой уверенности (облако не отдаёт logprobs)."""

    def __init__(self, llm: LLMPort, *, role: str = "workhorse") -> None:
        self.llm = llm
        self.role = role

    def decide(self, question: DecisionQuestion) -> DecisionResult:
        if question.kind == DecisionKind.CHOICE:
            names = list(question.options)
            schema = {
                "type": "object",
                "properties": {"choice": {"type": "string", "enum": names}, "confidence": {"type": "number"}},
                "required": ["choice", "confidence"],
            }
            listing = "\n".join(f"- {k}: {v}" for k, v in question.options.items())
            ask = f"{question.instructions}\n\nВарианты:\n{listing}\n\nВыбери один вариант (поле choice — имя варианта)."
        elif question.kind == DecisionKind.NOUL:
            schema = _NoulAnswer.model_json_schema()
            ask = f"{question.instructions}\n\nОтветь вероятностью «да» от 0 до 1 (probability_yes)."
        else:
            schema = _ScoreAnswer.model_json_schema()
            listing = "\n".join(f"{i}: {lv}" for i, lv in enumerate(question.levels))
            ask = f"{question.instructions}\n\nШкала:\n{listing}\n\nВыбери номер уровня (level)."
        request = LLMRequest(
            task=question.task,
            system="Ты принимаешь узкое решение по заданным признакам. Отвечай только JSON по схеме.",
            messages=[LLMMessage(role="user", content=f"Контекст:\n{question.state}\n\n{ask}")],
            json_schema=schema,
            schema_name=f"decide_{question.kind.value}",
            temperature=0.0,
            role_override=self.role,
            case_id=question.case_id,
            run_id=question.run_id,
            step=question.step,
        )
        response = self.llm.complete(request)
        data = response.data or {}
        result = DecisionResult(
            question_id=question.id,
            kind=question.kind,
            backend="llm",
            model=response.usage.model,
            input_tokens=response.usage.input_tokens,
            latency_ms=response.usage.latency_ms,
        )
        try:
            if question.kind == DecisionKind.CHOICE:
                ans = _ChoiceAnswer.model_validate(data)
                result.choice = ans.choice if ans.choice in question.options else next(iter(question.options))
                result.confidence = ans.confidence if ans.choice in question.options else 0.0
                result.probs = {result.choice: result.confidence}
            elif question.kind == DecisionKind.NOUL:
                ans_n = _NoulAnswer.model_validate(data)
                result.probability = ans_n.probability_yes
                result.confidence = max(ans_n.probability_yes, 1 - ans_n.probability_yes)
            else:
                ans_s = _ScoreAnswer.model_validate(data)
                result.score = float(min(ans_s.level, max(0, len(question.levels) - 1)))
                result.confidence = ans_s.confidence
        except Exception:
            result.confidence = 0.0
            if question.kind == DecisionKind.CHOICE:
                result.choice = next(iter(question.options), None)
            elif question.kind == DecisionKind.NOUL:
                result.probability = 0.5
            else:
                result.score = 0.0
        return result

    def decide_many(self, questions: list[DecisionQuestion]) -> list[DecisionResult]:
        return [self.decide(q) for q in questions]
