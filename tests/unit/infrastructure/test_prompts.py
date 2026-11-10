"""Промпты узлов и навыки: подключение навыка, версия, заполнение обеих частей, формат Agent Skills."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from strategist.infrastructure.prompts import FilePromptRepository, split_front_matter

ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _repo(tmp_path: Path, skill_text: str = "Шаг 1. Проверить посылку.") -> FilePromptRepository:
    (tmp_path / "instr").mkdir()
    (tmp_path / "skills" / "frame").mkdir(parents=True)
    (tmp_path / "instr" / "frame.md").write_text(
        "---\nname: frame\nversion: 2\nskills: [frame]\n---\n## system\nТы стратег. Не больше {{n}} вопросов.\n"
        "## user\n{{brief}}\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "frame" / "SKILL.md").write_text(
        f"---\nname: frame\ndescription: Постановка задачи\n---\n{skill_text}\n", encoding="utf-8"
    )
    return FilePromptRepository(tmp_path / "instr", tmp_path / "skills")


def test_skill_is_appended_to_system_and_changes_version(tmp_path):
    template = _repo(tmp_path).get("frame")
    assert "Методика (навык frame):\nШаг 1. Проверить посылку." in template.system
    assert template.placeholders() == {"n", "brief"}
    assert template.render_system(n=3, brief="—").startswith("Ты стратег. Не больше 3 вопросов.")
    assert template.render_user(n=3, brief="Почему падает удержание?") == "Почему падает удержание?"

    other = tmp_path / "other"
    other.mkdir()
    changed = _repo(other, skill_text="Шаг 1. Проверить посылку по данным.").get("frame")
    assert changed.version != template.version and changed.version.startswith("2-")


def test_declared_but_missing_skill_fails_loudly(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "skills" / "frame" / "SKILL.md").unlink()
    with pytest.raises(FileNotFoundError, match="нет навыка"):
        repo.get("frame")


def test_repository_prompts_load_with_their_skills():
    repo = FilePromptRepository(ROOT / "agent" / "instructions", ROOT / ".agents" / "skills")
    with_skills = {}
    for name in repo.names():
        meta, _ = split_front_matter(
            (ROOT / "agent" / "instructions" / f"{name}.md").read_text(encoding="utf-8")
        )
        template = repo.get(name)  # объявленный навык обязан существовать
        if meta.get("skills"):
            with_skills[name] = meta["skills"]
            assert "Методика (навык" in template.system
    assert {"frame", "premortem", "comms_strategy", "experiments"} <= set(with_skills)


def test_executive_summary_v2_keeps_v1_slots_adds_feedback_and_the_rules_on_numbers():
    """v1 остаётся для пути без флага summary_repair (поведение v0.1.1), v2 — для пути с проверкой чисел."""
    repo = FilePromptRepository(ROOT / "agent" / "instructions", ROOT / ".agents" / "skills")
    v1, v2 = repo.get("executive_summary"), repo.get("executive_summary_v2")
    assert v1.version.startswith("1-") and v2.version.startswith("2-")
    assert v2.placeholders() == v1.placeholders() | {"feedback"}
    assert "из той строки реестра, на которую довод ссылается" in v2.system
    assert "«гипотеза: …»" in v2.system and "стресс-тест — расчёт модели" in v2.system
    assert v2.user.endswith("Замечания проверки чисел к прошлому ответу: {{feedback}}")


def test_skills_follow_agent_skills_format():
    skills = sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md"))
    assert {p.parent.name for p in skills} >= {"frame", "premortem", "promise-lint", "interview-guide"}
    for path in skills:
        meta, body = split_front_matter(path.read_text(encoding="utf-8"))
        assert meta.get("name") == path.parent.name, path
        assert SKILL_NAME.match(meta["name"]) and len(meta["name"]) <= 64, path
        assert 0 < len(meta.get("description", "")) <= 1024, path
        assert body.strip(), path


def test_every_playbook_has_a_skill():
    from strategist.application.use_cases.frame import PLAYBOOKS

    repo = FilePromptRepository(ROOT / "agent" / "instructions", ROOT / ".agents" / "skills")
    for playbook in PLAYBOOKS:
        assert repo.skill(f"playbook-{playbook}"), playbook
