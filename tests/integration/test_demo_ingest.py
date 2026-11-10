"""Интеграция на материалах демо-кейса: настоящие парсеры и двойной движок таблиц, фейковая модель."""

from __future__ import annotations

from pathlib import Path

from fake_brain import FakeBrain
from fakes import FixedClock, make_context
from strategist.application.use_cases.collect import ingest_source, merge_ingest
from strategist.application.use_cases.start_case import start_case
from strategist.domain.entities import ClaimType, SourceKind
from strategist.infrastructure.persistence.db import CaseDatabase
from strategist.infrastructure.persistence.materials import FileMaterialStore
from strategist.infrastructure.persistence.repositories import SqliteCaseRepository
from strategist.infrastructure.security.guard import RegexContentGuard
from strategist.infrastructure.tools.parsers import default_parsers
from strategist.infrastructure.tools.tables import PandasDuckdbTableEngine

DEMO = Path(__file__).resolve().parents[2] / "cases" / "demo-retention" / "materials"


class DemoBrain(FakeBrain):
    def _MetricPlanDTO(self, text: str, _: str) -> dict:
        if "paid_m3" in text:
            return {
                "metrics": [
                    {
                        "name": "m3_rate",
                        "description": "Доля оплативших 3-й месяц",
                        "agg": "ratio",
                        "column": "paid_m3",
                        "denominator_column": "connected",
                        "group_by": "cohort_month",
                        "segment_column": "channel",
                        "unit": "доля",
                    }
                ]
            }
        if "main_reason" in text:
            return {
                "metrics": [
                    {
                        "name": "reasons",
                        "description": "Распределение причин отмены",
                        "agg": "share",
                        "group_by": "main_reason",
                        "unit": "доля",
                    }
                ]
            }
        return {"metrics": []}


def test_demo_materials_ingest_with_real_adapters(tmp_path):
    cases_dir = tmp_path / "cases"
    clock = FixedClock()
    ctx = make_context(
        llm=DemoBrain(),
        cases=SqliteCaseRepository(CaseDatabase(cases_dir)),
        materials=FileMaterialStore(cases_dir, clock),
        parsers=default_parsers(),
        tables=PandasDuckdbTableEngine(),
        guard=RegexContentGuard(),
        clock=clock,
    )
    paths = [str(p) for p in sorted(DEMO.iterdir()) if p.is_file() and not p.name.endswith(".meta.yaml")]
    case = start_case(
        ctx, case_id="demo-int", title="Демо", brief="Почему падает удержание?", material_paths=paths
    )
    results = [ingest_source(ctx, case, s.id) for s in case.sources]
    assert not [r.error for r in results if r.error], [r.error for r in results if r.error]
    merge_ingest(ctx, case, results)

    kinds = {s.kind for s in case.sources}
    assert SourceKind.TABLE in kinds and SourceKind.WEB in kinds
    docx = next(s for s in case.sources if (s.path or "").endswith(".docx"))
    assert any(e.source_id == docx.id for e in case.evidence), "DOCX разобран на абзацы с цитатами"
    for ev in case.evidence:  # каждая цитата — дословная подстрока разобранного исходника
        src = case.source(ev.source_id)
        parser = ctx.parser_for(ctx.materials.absolute_path(case.id, src.path))
        full = parser.parse(ctx.materials.absolute_path(case.id, src.path)).full_text
        assert ev.quote in full

    metrics = [c for c in case.claims if c.type == ClaimType.DERIVED_METRIC]
    assert metrics and all(c.computation.consistent for c in metrics), "pandas и DuckDB совпали"
    reasons = next(c for c in metrics if c.computation.metric == "reasons")
    assert "Распределение причин" in reasons.text
    bait = next(s for s in case.sources if "bait" in (s.path or ""))
    assert bait.instructions_detected, "приманка поймана, команда не стала цитатой"
    assert all("игнорируй" not in e.quote.lower() for e in case.evidence)
    assert all(s.ingested for s in case.sources)
