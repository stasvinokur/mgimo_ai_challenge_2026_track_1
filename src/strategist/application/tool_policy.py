"""Фильтр инструментов: на шагах, где модель видит недоверенный текст, доступны только инструменты чтения.

В AgentDojo фильтр инструментов снизил успех целевых атак на GPT-4o с 47,7% до 7,5%. Здесь он
детерминированный: действие вне белого списка шага отклоняется и журналируется (tool.blocked).
"""

from __future__ import annotations

from strategist.application.context import AppContext

# Все инструменты агента только читают данные (fetch_url — чтение открытой страницы из allowlist).
READ_ONLY_TOOLS = frozenset({"compute_metric", "fetch_url", "search_knowledge", "finish"})

POLICY: dict[str, frozenset[str]] = {
    "extract_evidence": frozenset(),  # читатель на карантине: только схема ответа, без инструментов
    "research_step": READ_ONLY_TOOLS,
}


def allowed(step: str, tool: str) -> bool:
    return tool in POLICY.get(step, frozenset())


def check(ctx: AppContext, case_id: str, step: str, tool: str) -> bool:
    if allowed(step, tool):
        return True
    ctx.emit("tool.blocked", case_id, step=step, tool=tool)
    return False
