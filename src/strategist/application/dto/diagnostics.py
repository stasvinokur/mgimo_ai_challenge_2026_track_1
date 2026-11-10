"""Результаты проверки окружения (strategist doctor)."""

from __future__ import annotations

from pydantic import BaseModel


class DiagnosticCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""
    required: bool = True
