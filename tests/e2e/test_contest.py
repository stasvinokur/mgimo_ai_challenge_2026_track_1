"""Оспорить основание → точечный пересмотр без новых файлов (граф пересмотра, фейковая модель)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mcp_server import _data, _fake_app

from strategist.application.ports import GateAction, GateResponse
from strategist.application.use_cases.challenge import audit_view
from strategist.application.use_cases.start_case import start_case
from strategist.domain.entities import ClaimStatus, DefeaterKind
from strategist.domain.services.dependency_graph import build_graph
from strategist.interfaces.mcp.server import create_server


def _finished(tmp_path):
    app = _fake_app(tmp_path)
    src = tmp_path / "m.md"
    src.write_text(
        "Удержание подписочных сервисов в 2026 году снизилось на 4 процентных пункта.\n", encoding="utf-8"
    )
    start_case(
        app.ctx, case_id="c", title="Оспорить", brief="Почему падает удержание?", material_paths=[str(src)]
    )
    status = app.runner.start("c")
    for _ in range(6):  # шлюзы: план, выбор варианта, фиксация — руководитель соглашается с предложенным
        if status.finished:
            break
        gate = status.waiting_gate
        assert gate is not None
        status = app.runner.resume("c", GateResponse(action=GateAction.APPROVE, choice=gate.default_choice))
    assert status.finished
    return app


def test_contest_marks_the_claim_and_revises_only_its_dependents(tmp_path):
    app = _finished(tmp_path)
    loaded = [r for r in audit_view(app.ctx, "c") if r.dependents]
    target = loaded[0].claim_id
    before = app.ctx.cases.load("c")
    dependents = set(build_graph(before).downstream([target]))
    calls = len(app.ctx.llm.calls)

    app.runner.contest("c", [target], "в опросе только текущие абоненты, выборка смещена")

    case = app.ctx.cases.load("c")
    record = case.revisions[-1]
    claim = case.claim(target)
    assert record.trigger == "contest" and record.contested_claim_ids == [target]
    assert record.new_source_ids == [] and record.new_claim_ids == []
    assert claim.status == ClaimStatus.SUSPECT
    assert claim.defeaters[-1].kind == DefeaterKind.UNDERCUTTING
    assert (
        claim.defeaters[-1].description
        == "Оспорено руководителем: в опросе только текущие абоненты, выборка смещена"
    )
    assert set(record.suspect_ids) <= dependents  # пересматриваются только зависимые узлы
    assert record.summary.startswith(f"Оспорено руководителем: {target} → 1 оснований под вопросом")
    revise_calls = [c for c in app.ctx.llm.calls[calls:] if c.task == "revise"]
    assert revise_calls and all(
        c.step != "revision:claims" for c in revise_calls
    )  # новых файлов нет — без разбора
    if record.suspect_ids:
        prompt = "\n".join(m.content for m in revise_calls[0].messages)
        assert "Руководитель оспорил" in prompt and "выборка смещена" in prompt


def test_contest_via_mcp_returns_what_changed(tmp_path):
    holder: dict = {}

    def factory():
        if "app" not in holder:
            holder["app"] = _finished(tmp_path)
        return holder["app"]

    server = create_server(factory)
    target = next(r.claim_id for r in audit_view(factory().ctx, "c") if r.dependents)

    async def scenario():
        return _data(
            await server.call_tool(
                "contest_claim", {"case_id": "c", "claim_ids": [target], "reason": "данные устарели"}
            )
        )

    result = asyncio.run(scenario())
    assert result["what_changed"].startswith(f"Оспорено руководителем: {target}")
    assert holder["app"].ctx.cases.load("c").revisions[-1].trigger == "contest"


def test_contest_of_unknown_rows_fails_before_any_model_call(tmp_path):
    import pytest

    from strategist.application.use_cases.revise import revise_on_contest

    app = _finished(tmp_path)
    calls = len(app.ctx.llm.calls)
    with pytest.raises(ValueError, match="Нет активных строк"):
        revise_on_contest(app.ctx, app.ctx.cases.load("c"), ["CL-999"], "—")
    assert len(app.ctx.llm.calls) == calls
