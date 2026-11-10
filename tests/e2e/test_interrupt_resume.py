"""Ctrl+C или исчерпанный лимит облака посреди прогона: работа останавливается, завершённые шаги не теряются,
продолжение — из нового процесса."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(
    """
    import json, os, signal, sys
    from pathlib import Path
    from fake_brain import FakeBrain
    from strategist.application.errors import StrategistError, UsageLimitReached
    from fakes import FixedClock, InMemoryCostLedger, SimpleTableEngine, TextParser, make_context
    from strategist.application.use_cases.start_case import start_case
    from strategist.infrastructure.orchestration.langgraph.graph import AutoGate
    from strategist.infrastructure.orchestration.langgraph.runner import LangGraphRunner
    from strategist.infrastructure.persistence.db import CaseDatabase
    from strategist.infrastructure.persistence.materials import FileMaterialStore
    from strategist.infrastructure.persistence.repositories import SqliteCaseRepository, SqliteJournal, SqliteLedger

    work = Path(sys.argv[1])
    interrupt_at = os.environ.get("INTERRUPT_AT", "")
    quota_at = os.environ.get("QUOTA_AT", "")

    class Brain(FakeBrain):
        def complete(self, request):
            if request.schema_name == interrupt_at:
                os.kill(os.getpid(), signal.SIGINT)  # как Ctrl+C в терминале
            if request.schema_name == quota_at:
                raise UsageLimitReached("Кредиты или лимит Ollama Cloud исчерпаны")
            return super().complete(request)

    cases = work / "cases"
    db = CaseDatabase(cases)
    clock = FixedClock()
    brain = Brain()
    ctx = make_context(llm=brain, cases=SqliteCaseRepository(db), ledger=SqliteLedger(db), journal=SqliteJournal(db),
                       materials=FileMaterialStore(cases, clock), parsers=[TextParser()], tables=SimpleTableEngine(),
                       costs=InMemoryCostLedger(), gate=AutoGate(), clock=clock, settings={"research": {"max_steps": 1}})
    if not ctx.cases.exists("ci"):
        m = work / "m.md"
        m.write_text("Удержание подписочных сервисов в 2026 году снизилось на 4 процентных пункта.", encoding="utf-8")
        start_case(ctx, case_id="ci", title="Ctrl+C", brief="Почему падает удержание?", material_paths=[str(m)])
    runner = LangGraphRunner(ctx, cases)
    try:
        status = runner.resume("ci")
    except KeyboardInterrupt:
        print(json.dumps({"interrupted": True, "schemas": [c.schema_name for c in brain.calls]}))
        sys.exit(130)
    except StrategistError as exc:
        print(json.dumps({"interrupted": False, "error": type(exc).__name__, "schemas": [c.schema_name for c in brain.calls]}))
        sys.exit(1)
    print(json.dumps({"interrupted": False, "finished": status.finished, "schemas": [c.schema_name for c in brain.calls]}))
    """
)


def _run(work: Path, interrupt_at: str = "", quota_at: str = "") -> tuple[int, dict]:
    script = work / "run.py"
    script.write_text(SCRIPT, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": f"{TESTS}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "INTERRUPT_AT": interrupt_at,
        "QUOTA_AT": quota_at,
    }
    proc = subprocess.run([sys.executable, str(script), str(work)], env=env, capture_output=True, text=True)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    return proc.returncode, (json.loads(lines[-1]) if lines else {"stderr": proc.stderr[-2000:]})


def test_ctrl_c_then_resume_in_new_process(tmp_path):
    code, first = _run(tmp_path, interrupt_at="OptionsDTO")
    assert code == 130 and first["interrupted"], first
    assert "FrameDTO" in first["schemas"] and "PremiseDTO" in first["schemas"]
    assert "OptionsDTO" not in first["schemas"], "Ctrl+C пришёл в момент вызова модели на шаге вариантов"

    code, second = _run(tmp_path)
    assert code == 0 and second["finished"], second
    assert "FrameDTO" not in second["schemas"], "постановка не пересчитывается после остановки"
    assert "ResearchPlanDTO" not in second["schemas"]
    assert "OptionsDTO" in second["schemas"], "прерванный шаг выполняется заново"

    from strategist.infrastructure.persistence.db import CaseDatabase
    from strategist.infrastructure.persistence.repositories import SqliteJournal

    entries = {e.step: e for e in SqliteJournal(CaseDatabase(tmp_path / "cases")).entries("ci")}
    assert entries["intake"].attempt == 1 and entries["claims"].attempt == 1
    assert entries["options"].attempt == 2 and entries["options"].status.value == "done"


def test_usage_limit_stops_run_and_resume_continues(tmp_path):
    """Кредиты облака кончились на шаге вариантов: понятная ошибка, а после пополнения — продолжение без повторов."""
    code, first = _run(tmp_path, quota_at="OptionsDTO")
    assert code == 1 and first["error"] == "UsageLimitReached", first
    assert "FrameDTO" in first["schemas"]

    code, second = _run(tmp_path)
    assert code == 0 and second["finished"], second
    assert "FrameDTO" not in second["schemas"] and "ResearchPlanDTO" not in second["schemas"]
    assert "OptionsDTO" in second["schemas"]
