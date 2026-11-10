"""Порт языковой модели. Маршрутизация, эскалация и учёт стоимости — в адаптере."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class LLMMessage(BaseModel):
    role: str  # system | user | assistant
    content: str


class UntrustedBlock(BaseModel):
    """Недоверенный текст (материалы, сайты). Адаптер обязан маркировать его (spotlighting)."""

    source_id: str
    text: str
    label: str = ""


class LLMRequest(BaseModel):
    task: str
    system: str
    messages: list[LLMMessage]
    untrusted: list[UntrustedBlock] = Field(default_factory=list)
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    escalation: int = 0
    role_override: str | None = None
    case_id: str | None = None
    run_id: str | None = None
    step: str | None = None


class LLMUsage(BaseModel):
    model: str
    role: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False
    latency_ms: int = 0
    escalated: bool = False


class LLMResponse(BaseModel):
    text: str
    data: Any | None = None
    usage: LLMUsage


class LLMPort(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...
