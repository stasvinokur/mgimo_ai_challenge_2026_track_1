"""MCP-сервер: кейс ведётся инструментами (создать → шлюзы → отчёт → новые данные) и по настоящему stdio."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fake_brain import FakeBrain
from fakes import FixedClock, InMemoryCostLedger, SimpleTableEngine, TextParser, make_context
from strategist.infrastructure.artifacts import FileArtifactWriter, MarkdownRenderer
from strategist.infrastructure.orchestration.langgraph.graph import LangGraphGate
from strategist.infrastructure.orchestration.langgraph.runner import LangGraphRunner
from strategist.infrastructure.persistence.db import CaseDatabase
from strategist.infrastructure.persistence.materials import FileMaterialStore
from strategist.infrastructure.persistence.repositories import (
    SqliteCaseRepository,
    SqliteJournal,
    SqliteLedger,
)
from strategist.interfaces.mcp.server import create_server

ROOT = Path(__file__).resolve().parents[2]


def _fake_app(tmp: Path) -> SimpleNamespace:
    cases_dir = tmp / "cases"
    db = CaseDatabase(cases_dir)
    clock = FixedClock()
    ctx = make_context(
        llm=FakeBrain(),
        cases=SqliteCaseRepository(db),
        ledger=SqliteLedger(db),
        journal=SqliteJournal(db),
        materials=FileMaterialStore(cases_dir, clock),
        parsers=[TextParser()],
        tables=SimpleTableEngine(),
        costs=InMemoryCostLedger(),
        gate=LangGraphGate(),
        clock=clock,
        artifacts=FileArtifactWriter(cases_dir),
        renderer=MarkdownRenderer(),
        settings={"research": {"max_steps": 1}},
    )
    return SimpleNamespace(ctx=ctx, runner=LangGraphRunner(ctx, cases_dir))


def _data(result) -> object:
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    content = getattr(result, "content", None) or []
    text = content[0].text if content and hasattr(content[0], "text") else ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def test_case_driven_through_mcp_tools(tmp_path):
    material = tmp_path / "m.md"
    material.write_text(
        "Удержание подписочных сервисов в 2026 году снизилось на 4 процентных пункта.\n", encoding="utf-8"
    )
    holder: dict = {}
    server = create_server(lambda: holder.setdefault("app", _fake_app(tmp_path)))

    async def scenario():
        call = server.call_tool
        await call(
            "create_case",
            {
                "case_id": "mcp",
                "title": "MCP",
                "brief": "Почему падает удержание?",
                "material_paths": [str(material)],
            },
        )
        status = _data(await call("run_case", {"case_id": "mcp"}))
        gates = []
        while not status["finished"]:
            gate = status["waiting_gate"]
            gates.append(gate["gate"])
            status = _data(
                await call(
                    "answer_gate",
                    {"case_id": "mcp", "action": "approve", "choice": gate.get("default_choice")},
                )
            )
        ledger = _data(await call("ledger_query", {"case_id": "mcp", "claim_type": "fact"}))
        report = _data(await call("report", {"case_id": "mcp"}))
        app = holder["app"]
        version, calls = app.ctx.cases.load("mcp").version, len(app.ctx.llm.calls)
        audit = _data(await call("audit_claims", {"case_id": "mcp", "limit": 5}))
        claim = audit[0]["claim_id"]
        preview = _data(await call("challenge_preview", {"case_id": "mcp", "claim_ids": [claim, "CL-999"]}))
        untouched = (app.ctx.cases.load("mcp").version, len(app.ctx.llm.calls)) == (version, calls)
        new = tmp_path / "new.md"
        new.write_text("Свежая когорта апреля показала удержание на уровне 39 процентов.\n", encoding="utf-8")
        changed = _data(await call("add_material", {"case_id": "mcp", "paths": [str(new)]}))
        return gates, ledger, report, changed, audit, preview, untouched

    gates, ledger, report, changed, audit, preview, untouched = asyncio.run(scenario())
    assert audit and audit[0]["why_first"] and audit[0]["sources"]  # несущие строки с источником
    assert preview["removed"] == [audit[0]["claim_id"]] and preview["unknown"] == ["CL-999"]
    assert untouched  # проверка оснований не меняет кейс и не зовёт модели
    assert gates == ["research_plan", "option_choice", "decision_commit"]
    assert ledger and all(row["type"] == "fact" for row in ledger)
    assert "Решение на одной странице" in report["text"]
    assert changed["what_changed"]


def test_stdio_server_lists_tools():
    from mcp import StdioServerParameters
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    async def scenario():
        params = StdioServerParameters(
            command="uv", args=["run", "--quiet", "strategist", "mcp"], cwd=str(ROOT), env={**os.environ}
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return {t.name for t in tools.tools}

    names = asyncio.run(asyncio.wait_for(scenario(), timeout=90))
    assert {"create_case", "run_case", "answer_gate", "ledger_query", "add_material", "what_changed"} <= names
    assert {"audit_claims", "challenge_preview"} <= names


def test_started_in_cli_continued_via_mcp_without_repeats(tmp_path):
    """Переносимость: начали в одной среде (исполнитель CLI), продолжили в другой (MCP) из той же папки."""
    from strategist.application.ports import JournalStatus
    from strategist.application.use_cases.start_case import start_case

    material = tmp_path / "m.md"
    material.write_text(
        "Удержание подписочных сервисов в 2026 году снизилось на 4 процентных пункта.\n", encoding="utf-8"
    )
    cli_app = _fake_app(tmp_path)
    start_case(
        cli_app.ctx,
        case_id="port",
        title="Перенос",
        brief="Почему падает удержание?",
        material_paths=[str(material)],
    )
    first = cli_app.runner.start("port")
    assert first.waiting_gate is not None  # CLI остановился на шлюзе плана
    done_before = {e.step for e in cli_app.ctx.journal.entries("port") if e.status == JournalStatus.DONE}
    llm_calls_before = len(cli_app.ctx.llm.calls)

    ide_app = _fake_app(tmp_path)  # «другая среда»: новый процесс поверх тех же файлов кейса
    server = create_server(lambda: ide_app)

    async def scenario():
        status = _data(await server.call_tool("case_status", {"case_id": "port"}))
        while not status["finished"]:
            gate = status["waiting_gate"]
            status = _data(
                await server.call_tool(
                    "answer_gate", {"case_id": "port", "choice": gate.get("default_choice")}
                )
            )
        return status

    assert asyncio.run(scenario())["finished"]
    entries = ide_app.ctx.journal.entries("port")
    assert all(e.attempt == 1 for e in entries if e.step in done_before), "выполненные шаги не повторялись"
    repeated = [
        c.schema_name for c in ide_app.ctx.llm.calls if c.schema_name in ("FrameDTO", "ResearchPlanDTO")
    ]
    assert not repeated, "постановка и план не пересчитывались в новой среде"
    assert llm_calls_before > 0
