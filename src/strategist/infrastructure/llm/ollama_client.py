"""Тонкий клиент Ollama: один интерфейс для локального демона и Ollama Cloud.

Облачные модели (*-cloud) идут через локальный демон после `ollama signin`; прямой доступ к облаку —
host=https://ollama.com + OLLAMA_API_KEY. Повторы с паузой при 429/5xx и обрывах соединения.

Тариф Ollama Cloud (ollama.com/pricing, ноябрь 2026): кредиты на месяц (Pro — $60, Max — $300) сбрасываются в день
начала подписки; одновременно 3 запроса (Max — 10), лишние ждут в очереди, при полной очереди запрос отклоняется.
Отказ из-за полной очереди или частоты — повтор с паузой. Исчерпанные кредиты или лимит — UsageLimitReached сразу,
без повторов: маршрутизатор переходит на локальные модели или останавливает работу с подсказкой.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import ollama

from strategist.application.errors import ModelServiceError, UsageLimitReached

# Признаки исчерпанных кредитов или лимита тарифа (а не временной перегрузки или полной очереди)
_USAGE_LIMIT_RE = re.compile(
    r"credit|usage[ _]limit|quota|insufficient|balance|billing|payment required|weekly limit|session limit"
    r"|exceeded your|(?<!rate )limit (?:reached|exceeded)",
    re.I,
)


class ModelUnavailable(Exception):
    """Модель не найдена или недоступна — маршрутизатор переходит на запасную."""


class TransientModelError(ModelServiceError):
    """Временная ошибка (лимит, 5xx, обрыв) после исчерпания повторов."""


@dataclass
class ChatResult:
    model: str
    content: str
    thinking: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    done_reason: str = ""
    logprobs: list[Any] = field(default_factory=list)


def think_param(model: str, level: str | bool | None) -> str | bool | None:
    """gpt-oss принимает уровни low/medium/high; другие thinking-модели — только True/False."""
    if level is None:
        return None
    if model.startswith("gpt-oss"):
        return level if isinstance(level, str) else ("medium" if level else "low")
    if isinstance(level, str):
        return level in ("medium", "high")
    return bool(level)


class OllamaClient:
    def __init__(
        self,
        host: str,
        api_key: str | None = None,
        timeout: float = 600.0,
        max_retries: int = 3,
        backoff_seconds: float = 4.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self.host = host
        self.client = ollama.Client(host=host, headers=headers, timeout=timeout)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    def _with_retries(self, fn: Any, model: str) -> Any:
        delay = self.backoff_seconds
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return fn()
            except ollama.ResponseError as exc:
                status = getattr(exc, "status_code", 0) or 0
                text = str(exc).lower()
                if (
                    status in (404, 410)
                    or "not found" in text
                    or "does not exist" in text
                    or "retired" in text
                ):
                    raise ModelUnavailable(f"{model}: {exc}") from exc
                if status == 402 or (status in (403, 429) and _USAGE_LIMIT_RE.search(text)):
                    raise UsageLimitReached(f"{model}: {exc}") from exc
                if status in (401, 403):
                    raise ModelUnavailable(
                        f"{model}: нет доступа ({status}) — проверьте `ollama signin`"
                    ) from exc
                if status == 429 or status >= 500:
                    last = exc
                else:
                    raise
            except (httpx.TransportError, ConnectionError) as exc:
                last = exc
            if attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2
        raise TransientModelError(f"{model}: {last}") from last

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
        think: str | bool | None = None,
        temperature: float | None = None,
        num_ctx: int | None = None,
        max_tokens: int | None = None,
        logprobs: bool = False,
        top_logprobs: int | None = None,
    ) -> ChatResult:
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if num_ctx:
            options["num_ctx"] = num_ctx
        if max_tokens:
            options["num_predict"] = max_tokens
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "options": options or None}
        if json_schema is not None:
            kwargs["format"] = json_schema
        think_value = think_param(model, think)
        if think_value is not None:
            kwargs["think"] = think_value
        if logprobs:
            kwargs["logprobs"] = True
            if top_logprobs:
                kwargs["top_logprobs"] = top_logprobs

        started = time.perf_counter()
        response = self._with_retries(lambda: self.client.chat(**kwargs), model)
        latency = int((time.perf_counter() - started) * 1000)
        message = response.message
        return ChatResult(
            model=model,
            content=message.content or "",
            thinking=getattr(message, "thinking", "") or "",
            input_tokens=response.prompt_eval_count or 0,
            output_tokens=response.eval_count or 0,
            latency_ms=latency,
            done_reason=response.done_reason or "",
            logprobs=list(getattr(response, "logprobs", None) or []),
        )

    def embed(self, model: str, texts: list[str]) -> tuple[list[list[float]], int]:
        response = self._with_retries(lambda: self.client.embed(model=model, input=texts), model)
        return [list(v) for v in response.embeddings], int(getattr(response, "prompt_eval_count", 0) or 0)

    def list_models(self) -> set[str]:
        response = self._with_retries(lambda: self.client.list(), "list")
        return {m.model for m in response.models}
