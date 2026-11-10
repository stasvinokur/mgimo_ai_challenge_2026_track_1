"""Панель модельных респондентов на карантине: плацебо, смена формулировки, разногласия, вопросы для интервью."""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_brain import FakeBrain
from fakes import FixedClock, InMemoryCostLedger, SimpleTableEngine, TextParser, make_context
from strategist.application.errors import SyntheticQuarantineViolation
from strategist.application.use_cases.panel import run_synthetic_panel
from strategist.application.use_cases.report import render_markdown
from strategist.application.use_cases.start_case import start_case
from strategist.domain.entities import PLACEBO_ID, ClaimType, OptionKind
from strategist.infrastructure.orchestration.langgraph.graph import AutoGate
from strategist.infrastructure.orchestration.langgraph.runner import LangGraphRunner
from strategist.infrastructure.persistence.db import CaseDatabase
from strategist.infrastructure.persistence.materials import FileMaterialStore
from strategist.infrastructure.persistence.repositories import (
    SqliteCaseRepository,
    SqliteJournal,
    SqliteLedger,
)


class NoEmbeddings:
    def embed(self, texts):
        raise RuntimeError("эмбеддинги недоступны")


def _finished_case(tmp_path: Path, brain: FakeBrain, embeddings=None):
    """Кейс доводится до конца на фейковых эмбеддингах (они нужны отсеву клонов); embeddings — для панели."""
    cases_dir = tmp_path / "cases"
    db = CaseDatabase(cases_dir)
    clock = FixedClock()
    ctx = make_context(
        llm=brain,
        cases=SqliteCaseRepository(db),
        ledger=SqliteLedger(db),
        journal=SqliteJournal(db),
        materials=FileMaterialStore(cases_dir, clock),
        parsers=[TextParser()],
        tables=SimpleTableEngine(),
        costs=InMemoryCostLedger(),
        gate=AutoGate(),
        clock=clock,
        settings={"research": {"max_steps": 1}},
    )
    src = tmp_path / "m.md"
    src.write_text(
        "Удержание подписочных сервисов в 2026 году снизилось на 4 процентных пункта.\n", encoding="utf-8"
    )
    start_case(
        ctx, case_id="panel", title="Панель", brief="Почему падает удержание?", material_paths=[str(src)]
    )
    assert LangGraphRunner(ctx, cases_dir).start("panel").finished
    if embeddings is not None:
        ctx.embeddings = embeddings
    return ctx, ctx.cases.load("panel")


def test_panel_is_quarantined_and_reports_disagreements_not_averages(tmp_path):
    ctx, case = _finished_case(tmp_path, FakeBrain(), NoEmbeddings())
    claims_before = len(case.claims)
    run = run_synthetic_panel(ctx, case, personas=3)

    concepts = [o.id for o in case.options if o.kind not in (OptionKind.STATUS_QUO, OptionKind.KILL)]
    assert set(run.concepts) == {*concepts, PLACEBO_ID}  # «оставить» и «закрыть» персонам не показываются
    assert len(run.personas) == 3 and all(p.label.startswith("СИНТЕТИКА") for p in run.personas)
    assert all(a.label.startswith("СИНТЕТИКА") and a.score_method == "model" for a in run.answers)
    assert run.trust == "normal" and not run.placebo_liked
    assert (
        run.disagreements
        and run.disagreements[0].supporters == ["P1"]
        and "P2" in run.disagreements[0].opponents
    )
    bad = next(q for q in run.interview_questions if q.question.startswith("Купили бы"))
    assert "hypothetical" in bad.issues and bad.suggestion.startswith("Расскажите")
    assert len(case.claims) == claims_before  # ответы персон не становятся строками реестра

    text = render_markdown(ctx, case)
    section = text.split("## Модельные респонденты")[1].split("\n## ")[0]
    assert "СИНТЕТИКА — гипотеза, не доказательство" in section
    assert "Средние оценки не показываются" in section and "Вопросы для реальных интервью" in section


def test_liked_placebo_lowers_trust(tmp_path):
    ctx, case = _finished_case(tmp_path, FakeBrain(placebo_love=True), NoEmbeddings())
    run = run_synthetic_panel(ctx, case, personas=3)
    assert run.placebo_liked and run.trust == "low"
    assert "плацебо-концепт понравился" in render_markdown(ctx, case)


def test_ssr_scores_from_reaction_text_when_embeddings_available(tmp_path):
    ctx, case = _finished_case(tmp_path, FakeBrain())  # HashEmbeddings из фейков
    run = run_synthetic_panel(ctx, case, personas=3)
    assert {a.score_method for a in run.answers} == {"ssr"}
    assert all(1.0 <= a.score <= 5.0 for a in run.answers)


def test_synthetic_leaking_into_facts_stops_the_run(tmp_path):
    from factories import add_claim, add_evidence, add_source

    ctx, case = _finished_case(tmp_path, FakeBrain(), NoEmbeddings())
    src = add_source(case, synthetic=True)  # модельные респонденты
    add_claim(case, "Родители выбирают семейный тариф", ClaimType.FACT, [add_evidence(case, src)])
    with pytest.raises(SyntheticQuarantineViolation, match="модельные респонденты"):
        run_synthetic_panel(ctx, case, personas=3)
