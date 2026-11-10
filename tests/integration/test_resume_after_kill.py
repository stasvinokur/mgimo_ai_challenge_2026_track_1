"""kill -9 посреди прогона: повторный запуск продолжает с прерванного шага и не дублирует вызовы модели."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(
    """
    import os, signal, sys
    from pathlib import Path

    from fakes import make_context
    from strategist.application.steps import run_step, stable_hash
    from strategist.infrastructure.persistence.db import CaseDatabase
    from strategist.infrastructure.persistence.repositories import SqliteJournal, SqliteResponseCache
    from strategist.infrastructure.runtime import SystemClock

    workdir = Path(sys.argv[1])
    kill_at = os.environ.get("KILL_AT", "")
    db = CaseDatabase(workdir / "cases")
    ctx = make_context(journal=SqliteJournal(db), clock=SystemClock())
    cache = SqliteResponseCache(db)
    calls_log = workdir / "model_calls.log"

    def call_model(prompt: str) -> str:
        key = stable_hash(prompt)
        cached = cache.get("c1", key)
        if cached is not None:
            return cached["text"]
        with calls_log.open("a") as fh:          # «настоящий» вызов модели
            fh.write(prompt + "\\n")
        answer = prompt.upper()
        cache.put("c1", key, "fake", {"text": answer}, ctx.clock.now())
        return answer

    def make_step(name: str):
        def work():
            answer = call_model(f"вопрос для {name}")
            if name == kill_at:
                os.kill(os.getpid(), signal.SIGKILL)   # обрыв после ответа модели, до done
            return {"answer": answer}
        return work

    for step in ["frame", "plan", "collect", "claims"]:
        run_step(ctx, "c1", step, {"step": step}, make_step(step))
    print("finished")
    """
)


def _run(workdir: Path, kill_at: str = "") -> subprocess.CompletedProcess[str]:
    script = workdir / "pipeline.py"
    script.write_text(SCRIPT, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": f"{TESTS_DIR}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}
    if kill_at:
        env["KILL_AT"] = kill_at
    else:
        env.pop("KILL_AT", None)
    return subprocess.run(
        [sys.executable, str(script), str(workdir)], env=env, capture_output=True, text=True
    )


def test_resume_after_sigkill_without_duplicate_model_calls(tmp_path):
    first = _run(tmp_path, kill_at="collect")
    assert first.returncode == -signal.SIGKILL, first.stderr
    calls = (tmp_path / "model_calls.log").read_text().splitlines()
    assert calls == ["вопрос для frame", "вопрос для plan", "вопрос для collect"]

    second = _run(tmp_path)
    assert second.returncode == 0, second.stderr
    assert "finished" in second.stdout
    calls = (tmp_path / "model_calls.log").read_text().splitlines()
    # Шаги frame и plan пропущены по журналу; collect повторён, но ответ модели взят из кэша.
    assert calls == ["вопрос для frame", "вопрос для plan", "вопрос для collect", "вопрос для claims"]

    from strategist.infrastructure.persistence.db import CaseDatabase
    from strategist.infrastructure.persistence.repositories import SqliteJournal

    journal = SqliteJournal(CaseDatabase(tmp_path / "cases"))
    entries = {e.step: e for e in journal.entries("c1")}
    assert all(e.status.value == "done" for e in entries.values())
    assert entries["collect"].attempt == 2
    assert entries["frame"].attempt == 1
