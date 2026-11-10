"""Проверка оснований: несущие утверждения и пробное исключение опоры.

Только чтение: кейс загружается, но не сохраняется, реестр и журнал не меняются, модели не вызываются.
"""

from __future__ import annotations

from strategist.application.context import AppContext
from strategist.domain.services.challenge import AuditRow, ChallengePreview, audit_rows, preview_removal


def audit_view(ctx: AppContext, case_id: str, limit: int = 12) -> list[AuditRow]:
    return audit_rows(ctx.cases.load(case_id), limit=limit)


def preview_challenge(ctx: AppContext, case_id: str, node_ids: list[str]) -> ChallengePreview:
    return preview_removal(ctx.cases.load(case_id), node_ids)
