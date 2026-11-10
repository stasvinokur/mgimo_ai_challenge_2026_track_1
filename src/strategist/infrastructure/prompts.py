"""Промпты узлов (agent/instructions/*.md) и навыки (.agents/skills/*/SKILL.md) из файлов.

Формат промпта: YAML front matter (name, version, skills) + секции «## system» и «## user».
skills — навыки из .agents/skills/<имя>/SKILL.md (формат Agent Skills): их текст дописывается в системную часть
как методика узла. Версия = номер из front matter + короткий хеш промпта и навыков: в журнале видно, какой
текст инструкций и методики работал.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from strategist.application.ports import PromptTemplate

_SECTION = re.compile(r"^##\s+(system|user)\s*$", re.MULTILINE)


def split_front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            meta = yaml.safe_load(text[3:end]) or {}
            return (meta if isinstance(meta, dict) else {}), text[end + 4 :].lstrip("\n")
    return {}, text


class FilePromptRepository:
    def __init__(self, instructions_dir: Path, skills_dir: Path) -> None:
        self.instructions_dir = Path(instructions_dir)
        self.skills_dir = Path(skills_dir)
        self._cache: dict[str, PromptTemplate] = {}

    def get(self, name: str) -> PromptTemplate:
        if name in self._cache:
            return self._cache[name]
        path = self.instructions_dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Нет промпта {path}")
        raw = path.read_text(encoding="utf-8")
        meta, body = split_front_matter(raw)
        parts = _SECTION.split(body)
        sections: dict[str, str] = {}
        for i in range(1, len(parts) - 1, 2):
            sections[parts[i]] = parts[i + 1].strip()
        system = sections.get("system", body.strip())
        hashed = raw
        for skill_name in meta.get("skills") or []:
            text = self.skill(skill_name)
            if not text:
                raise FileNotFoundError(f"Промпт {name}: нет навыка {self._skill_path(skill_name)}")
            system += f"\n\nМетодика (навык {skill_name}):\n{text}"
            hashed += text
        digest = hashlib.sha256(hashed.encode("utf-8")).hexdigest()[:8]
        template = PromptTemplate(
            name=name,
            version=f"{meta.get('version', 1)}-{digest}",
            system=system,
            user=sections.get("user", ""),
        )
        self._cache[name] = template
        return template

    def _skill_path(self, name: str) -> Path:
        return self.skills_dir / name.replace("_", "-") / "SKILL.md"

    def skill(self, name: str) -> str:
        path = self._skill_path(name)
        if not path.exists():
            return ""
        _, body = split_front_matter(path.read_text(encoding="utf-8"))
        return body.strip()

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.instructions_dir.glob("*.md"))

    def versions(self) -> dict[str, str]:
        return {name: self.get(name).version for name in self.names()}
