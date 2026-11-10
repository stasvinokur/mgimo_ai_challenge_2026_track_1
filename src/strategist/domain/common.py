"""Общие элементы домена: базовая модель и выдача идентификаторов."""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

_ID_RE = re.compile(r"^(?P<prefix>[A-Z]+)-(?P<num>\d+)$")


class Entity(BaseModel):
    """Базовая доменная модель: лишние поля запрещены, присваивание проверяется."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class LintIssue(Entity):
    """Нарушение правила: ошибка блокирует вывод в отчёт, предупреждение — нет."""

    code: str
    severity: Severity = Severity.ERROR
    entity_id: str
    message: str


def next_id(prefix: str, existing: Iterable[str], width: int = 3) -> str:
    """Следующий номер с префиксом без повторов: CL-001, CL-002, … (удалённые номера не переиспользуются)."""
    top = 0
    for value in existing:
        match = _ID_RE.match(value)
        if match and match.group("prefix") == prefix:
            top = max(top, int(match.group("num")))
    return f"{prefix}-{top + 1:0{width}d}"


SENTENCE_END = (".", "!", "?", "…", "»", ")", '"')


def clip_words(text: str, limit: int) -> str:
    """Текст не длиннее limit: обрезка по границе слова с «…», а не посреди слова или кода [CL-…]."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


ID_PREFIXES = {
    "source": "SRC",
    "evidence": "EV",
    "claim": "CL",
    "unknown": "UNK",
    "option": "OPT",
    "objection": "OBJ",
    "segment": "SEG",
    "feature": "FEAT",
    "promise": "PRM",
    "experiment": "EXP",
    "forecast": "FC",
    "trigger": "TRG",
    "commitment": "CMT",
    "decision": "DEC",
    "uncertainty": "U",
    "question": "RQ",
    "revision": "REV",
    "panel": "PNL",
}
