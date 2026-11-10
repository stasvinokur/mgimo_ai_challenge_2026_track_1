"""Входы демо-кейса — одни для CLI (`strategist demo`), веб-панели («Открыть демо») и скриптов ролика.

Записанные ответы моделей (кассеты) узнают запрос по его тексту, поэтому название, запрос и материалы демо-кейса
должны совпадать с записью до символа: они берутся только отсюда.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEMO_CASE_ID = "demo-retention-run"


def strip_front_matter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip()
    return text


def collect_paths(materials: list[Path] | None, materials_dir: Path | None) -> list[str]:
    paths = [str(p) for p in (materials or [])]
    if materials_dir:
        paths += [
            str(p)
            for p in sorted(materials_dir.rglob("*"))
            if p.is_file()
            and not p.name.endswith(".meta.yaml")
            and p.name != "manifest.json"
            and not p.name.startswith(".")
        ]
    return paths


@dataclass(frozen=True)
class DemoInputs:
    title: str
    brief: str
    materials: list[str]
    new_evidence: list[str]


def demo_inputs(source: Path, fallback_title: str = "demo") -> DemoInputs:
    """Запрос, материалы и новые данные демо-кейса; название — первая непустая строка запроса (как у записи)."""
    brief = strip_front_matter((source / "brief.md").read_text(encoding="utf-8"))
    materials = collect_paths(None, source / "materials")
    new_ev = collect_paths(None, source / "new_evidence") if (source / "new_evidence").exists() else []
    title = next((line.lstrip("# ").strip() for line in brief.splitlines() if line.strip()), fallback_title)
    return DemoInputs(title=title, brief=brief, materials=materials, new_evidence=new_ev)
