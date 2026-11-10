"""Мелкие помощники сценариев."""

from __future__ import annotations

from datetime import date, datetime


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def enum_or[E](enum_cls: type[E], value: str | None, default: E) -> E:
    try:
        return enum_cls(value)  # type: ignore[call-arg]
    except (ValueError, TypeError):
        return default


def known_ids(ids: list[str], valid: set[str]) -> list[str]:
    """Оставляет только существующие ID (модель может сослаться на несуществующую строку)."""
    seen: list[str] = []
    for i in ids:
        i = i.strip().strip("[]")
        if i in valid and i not in seen:
            seen.append(i)
    return seen
