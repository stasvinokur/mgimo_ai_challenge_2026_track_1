"""Проверка целостности материалов перед продолжением работы."""

from __future__ import annotations

from strategist.application.context import AppContext
from strategist.application.errors import IntegrityError


def ensure_integrity(ctx: AppContext, case_id: str) -> None:
    problems = ctx.materials.verify(case_id)
    if problems:
        ctx.emit("integrity.failed", case_id, problems=problems)
        listed = "\n".join(f"  - {p}" for p in problems)
        raise IntegrityError(
            "Материалы кейса изменились после загрузки — выводы опирались на другие версии файлов:\n"
            f"{listed}\nВерните исходные файлы или добавьте новую версию как новый материал "
            "(`strategist add`), чтобы агент пересмотрел затронутые выводы."
        )
