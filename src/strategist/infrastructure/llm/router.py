"""RoutedLLM — адаптер LLMPort: маршрутизация по задаче, эскалация, запасные модели, кэш и чек стоимости.

Сильная модель вызывается только там, где нужна: роль выбирается по типу задачи (config/models.yaml →
tasks). При невалидном выводе application просит эскалацию (request.escalation), и роль поднимается
по цепочке escalation. Если модель недоступна — переход на запасную модель роли с записью события.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from strategist.application.errors import UsageLimitReached
from strategist.application.ports import (
    Clock,
    CostLedger,
    CostRecord,
    Event,
    EventBus,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)
from strategist.infrastructure.llm.ollama_client import ChatResult, ModelUnavailable, TransientModelError
from strategist.infrastructure.security.spotlighting import GUARD_RULE, render_untrusted


class ChatBackend(Protocol):
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
    ) -> ChatResult: ...


class ResponseCache(Protocol):
    def get(self, case_id: str, key: str) -> dict[str, Any] | None: ...

    def put(self, case_id: str, key: str, model: str, response: dict[str, Any], at: Any) -> None: ...


def request_key(request: LLMRequest, role: str) -> str:
    payload = {
        "task": request.task,
        "role": role,
        "system": request.system,
        "messages": [m.model_dump() for m in request.messages],
        "untrusted": [b.model_dump() for b in request.untrusted],
        "schema": request.schema_name,
        "escalation": request.escalation,
        "temperature": request.temperature,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


SCHEMA_RULE = (
    "\n\nФОРМАТ ОТВЕТА: верни только один JSON-объект без пояснений и без markdown, строго по этой "
    "JSON Schema (облачные модели не ограничивают декодирование схемой, поэтому она приведена здесь):\n"
)


def compact_schema(schema: dict[str, Any]) -> str:
    """Схема без служебных заголовков — короче в промпте, смысл тот же."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return json.dumps(strip(schema), ensure_ascii=False, separators=(",", ":"))


def build_messages(request: LLMRequest) -> list[dict[str, str]]:
    system = request.system + (GUARD_RULE if request.untrusted else "")
    if request.json_schema is not None:
        system += SCHEMA_RULE + compact_schema(request.json_schema)
    messages = [{"role": "system", "content": system}]
    user_msgs = [m.model_dump() for m in request.messages]
    if request.untrusted:
        data = render_untrusted(request.untrusted)
        if user_msgs and user_msgs[0]["role"] == "user":
            user_msgs[0] = {"role": "user", "content": f"{data}\n\n{user_msgs[0]['content']}"}
        else:
            user_msgs.insert(0, {"role": "user", "content": data})
    return messages + user_msgs


def parse_json(text: str) -> Any | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.startswith("json") else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


USAGE_LIMIT_HINT = (
    "Кредиты или лимит Ollama Cloud исчерпаны ({detail}), а локальной модели для роли {role} нет. "
    "Завершённые шаги сохранены. Остаток кредитов: ollama.com/settings/usage; кредиты подписки "
    "сбрасываются раз в месяц, в день её начала, баланс можно пополнить. После этого: strategist resume <кейс>. "
    "Либо назначьте роли локальную модель в config/models.yaml."
)


def is_cloud_model(model: str) -> bool:
    """Облачные модели Ollama: суффикс :cloud или -cloud (gemma4:31b-cloud, deepseek-v4.1-flash:cloud)."""
    return model.endswith(":cloud") or model.endswith("-cloud")


class RoutedLLM:
    def __init__(
        self,
        backend: ChatBackend,
        models_cfg: dict[str, Any],
        *,
        costs: CostLedger | None,
        events: EventBus,
        clock: Clock,
        cache: ResponseCache | None = None,
        backend_name: str = "ollama",
    ) -> None:
        self.backend = backend
        self.cfg = models_cfg
        self.roles: dict[str, dict[str, Any]] = models_cfg.get("roles", {})
        self.tasks: dict[str, str] = models_cfg.get("tasks", {})
        self.escalation: list[str] = models_cfg.get("escalation", ["local_small", "workhorse", "reasoning"])
        self.costs = costs
        self.events = events
        self.clock = clock
        self.cache = cache
        self.backend_name = backend_name
        self.cloud_blocked = ""  # причина, если кредиты облака исчерпаны: до конца процесса облако не трогаем

    def role_for(self, request: LLMRequest) -> str:
        role = request.role_override or self.tasks.get(request.task, "workhorse")
        if request.escalation and role in self.escalation:
            idx = min(self.escalation.index(role) + request.escalation, len(self.escalation) - 1)
            role = self.escalation[idx]
        elif request.escalation:
            role = self.escalation[-1]
        return role

    def _emit(self, event_type: str, request: LLMRequest, **data: Any) -> None:
        self.events.publish(
            Event(type=event_type, case_id=request.case_id or "", at=self.clock.now(), data=data)
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        role = self.role_for(request)
        if request.escalation:
            self._emit("route.escalated", request, task=request.task, role=role, level=request.escalation)
        key = request_key(request, role)
        if self.cache is not None and request.case_id:
            cached = self.cache.get(request.case_id, key)
            if cached is not None:
                self._emit("llm.cache_hit", request, task=request.task, model=cached.get("model", ""))
                usage = LLMUsage(model=cached.get("model", ""), role=role, escalated=request.escalation > 0)
                return LLMResponse(text=cached["text"], data=cached.get("data"), usage=usage)

        spec = self.roles.get(role) or self.roles.get("workhorse") or {}
        candidates = [spec.get("model"), *spec.get("fallbacks", [])]
        messages = build_messages(request)
        errors: list[str] = []
        result: ChatResult | None = None
        for model in [c for c in candidates if c]:
            if self.cloud_blocked and is_cloud_model(model):
                errors.append(f"{model}: пропущена — лимит облака исчерпан")
                continue
            try:
                result = self.backend.chat(
                    model,
                    messages,
                    json_schema=request.json_schema,
                    think=spec.get("think"),
                    temperature=request.temperature
                    if request.temperature is not None
                    else spec.get("temperature"),
                    num_ctx=spec.get("num_ctx"),
                    max_tokens=request.max_tokens,
                )
                break
            except UsageLimitReached as exc:
                self.cloud_blocked = str(exc)[:300]
                errors.append(f"{model}: {exc}")
                self._emit(
                    "route.fallback",
                    request,
                    task=request.task,
                    model=model,
                    error=str(exc)[:300],
                    reason="usage_limit",
                )
            except (ModelUnavailable, TransientModelError) as exc:
                errors.append(f"{model}: {exc}")
                self._emit("route.fallback", request, task=request.task, model=model, error=str(exc)[:300])
        if result is None:
            if self.cloud_blocked:
                raise UsageLimitReached(USAGE_LIMIT_HINT.format(role=role, detail=self.cloud_blocked))
            raise TransientModelError(f"Все модели роли {role} недоступны: {errors}")

        data = parse_json(result.content) if request.json_schema is not None else None
        usage = LLMUsage(
            model=result.model,
            role=role,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            estimated=False,
            latency_ms=result.latency_ms,
            escalated=request.escalation > 0,
        )
        if self.costs is not None and request.case_id:
            self.costs.record(
                CostRecord(
                    case_id=request.case_id,
                    run_id=request.run_id or "",
                    step=request.step or request.task,
                    task=request.task,
                    backend=self.backend_name,
                    model=result.model,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    tokens_estimated=False,
                    latency_ms=result.latency_ms,
                    at=self.clock.now(),
                    attributes={"strategist.role": role, "strategist.escalation": request.escalation},
                )
            )
        self._emit(
            "llm.called",
            request,
            task=request.task,
            role=role,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
        )
        if self.cache is not None and request.case_id and (data is not None or request.json_schema is None):
            self.cache.put(
                request.case_id,
                key,
                result.model,
                {"text": result.content, "data": data, "model": result.model},
                self.clock.now(),
            )
        return LLMResponse(text=result.content, data=data, usage=usage)
