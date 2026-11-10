"""Структурированные вызовы модели: схема из DTO, повтор с ошибкой валидации, затем эскалация."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from strategist.application.context import AppContext
from strategist.application.errors import SchemaError
from strategist.application.ports import LLMMessage, LLMRequest, LLMUsage, UntrustedBlock

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if match:
            return json.loads(match.group(0))
        raise


def ask_structured[T: BaseModel](
    ctx: AppContext,
    *,
    task: str,
    prompt: str,
    values: dict[str, Any],
    schema: type[T],
    case_id: str | None = None,
    step: str | None = None,
    untrusted: list[UntrustedBlock] | None = None,
    max_attempts: int = 3,
    temperature: float | None = None,
    escalation: int = 0,
    role_override: str | None = None,
) -> tuple[T, LLMUsage]:
    """Вызов модели с JSON-схемой. Попытки: 1 — обычная, 2 — с текстом ошибки, 3 — эскалация роли."""
    template = ctx.prompts.get(prompt)
    missing = template.placeholders() - set(values)
    if missing:
        raise ValueError(f"Промпт {prompt}: не переданы значения {sorted(missing)}")
    system = template.render_system(**values)
    user = template.render_user(**values)
    messages = [LLMMessage(role="user", content=user)]
    json_schema = schema.model_json_schema()
    last_error = ""
    total_in = total_out = 0
    for attempt in range(max_attempts):
        level = escalation + (1 if attempt >= 2 else 0)
        request = LLMRequest(
            task=task,
            system=system,
            messages=messages,
            untrusted=untrusted or [],
            json_schema=json_schema,
            schema_name=schema.__name__,
            temperature=temperature,
            escalation=level,
            role_override=role_override,
            case_id=case_id,
            run_id=ctx.run_id,
            step=step or task,
        )
        response = ctx.llm.complete(request)
        ctx.emit(
            "llm.called",
            case_id or "",
            task=task,
            step=step or task,
            model=response.usage.model,
            role=response.usage.role,
            attempt=attempt + 1,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=response.usage.latency_ms,
        )
        total_in += response.usage.input_tokens
        total_out += response.usage.output_tokens
        try:
            data = response.data if response.data is not None else _extract_json(response.text)
            obj = schema.model_validate(data)
            usage = response.usage.model_copy(
                update={"input_tokens": total_in, "output_tokens": total_out, "escalated": level > 0}
            )
            return obj, usage
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
            last_error = str(exc)[:1500]
            ctx.emit(
                "llm.schema_error",
                case_id or "",
                task=task,
                attempt=attempt + 1,
                model=response.usage.model,
                error=last_error[:300],
            )
            messages = [
                *messages,
                LLMMessage(role="assistant", content=response.text[:4000]),
                LLMMessage(
                    role="user",
                    content=(
                        "Ответ не прошёл проверку схемы. Исправь и верни только JSON по схеме. "
                        f"Ошибка: {last_error}"
                    ),
                ),
            ]
    raise SchemaError(f"{task}: модель не вернула валидный {schema.__name__}: {last_error}")
