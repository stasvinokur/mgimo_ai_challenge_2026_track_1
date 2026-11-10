"""Веб-панель: API поверх use cases на фейковых моделях (без сети) — те же сценарии, что были у панели на Streamlit,
плюс фоновые прогоны («занято» при втором), раздача событий и представления для экрана."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from fake_brain import FakeBrain
from fakes import FixedClock, InMemoryCostLedger, SimpleTableEngine, TextParser, make_context
from strategist.application.ports import GateKind
from strategist.application.use_cases.start_case import start_case
from strategist.infrastructure.artifacts import FileArtifactWriter, MarkdownRenderer
from strategist.infrastructure.orchestration.langgraph.graph import AutoGate, LangGraphGate
from strategist.infrastructure.orchestration.langgraph.runner import LangGraphRunner
from strategist.infrastructure.persistence.db import CaseDatabase
from strategist.infrastructure.persistence.materials import FileMaterialStore
from strategist.infrastructure.persistence.repositories import (
    SqliteCaseRepository,
    SqliteJournal,
    SqliteLedger,
)
from strategist.interfaces.web.api import create_app
from strategist.interfaces.web.jobs import EventHub

TEXT = "Удержание подписочных сервисов в 2026 году снизилось на 4 процентных пункта.\n"


class _App:
    """То, что панель берёт из bootstrap: контекст, исполнитель и подписка на события."""

    def __init__(self, ctx, runner) -> None:
        self.ctx, self.runner = ctx, runner

    def subscribe(self, handler, event_type: str = "*") -> None:
        self.ctx.events.subscribe(event_type, handler)


def _setup(tmp_path: Path, gate, case_id: str = "web") -> tuple[_App, TestClient]:
    cases_dir = tmp_path / "cases"
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
        gate=gate,
        clock=clock,
        artifacts=FileArtifactWriter(cases_dir),
        renderer=MarkdownRenderer(),
        settings={"research": {"max_steps": 1}},
    )
    src = tmp_path / "m.md"
    src.write_text(TEXT, encoding="utf-8")
    start_case(
        ctx,
        case_id=case_id,
        title="Кейс для панели",
        brief="Почему падает удержание?",
        material_paths=[str(src)],
    )
    app = _App(ctx, LangGraphRunner(ctx, cases_dir))
    return app, TestClient(create_app(app))


def _wait(client: TestClient) -> dict:
    client.app.state.jobs.wait(120)
    job = client.get("/api/job").json()
    assert job and not job["running"] and not job["error"], job
    return job


def test_manager_passes_gates_through_the_api(tmp_path):
    """Руководитель правит план, выбирает вариант, отклоняет фиксацию и затем фиксирует решение — всё в фоне."""
    app, client = _setup(tmp_path, LangGraphGate())
    view = client.get("/api/cases/web").json()
    assert view["gate"] is None and not view["finished"]

    client.post("/api/cases/web/run")
    _wait(client)
    view = client.get("/api/cases/web").json()
    assert view["gate"]["kind"] == GateKind.RESEARCH_PLAN.value and view["gate"]["plan"]

    client.post("/api/cases/web/gate", json={"action": "edit", "edits": {"add": ["Сколько стоит аналог?"]}})
    _wait(client)
    plan = app.ctx.cases.load("web").research_plan
    assert any(q.question == "Сколько стоит аналог?" for q in plan.questions)
    gate = client.get("/api/cases/web").json()["gate"]
    assert gate["kind"] == "option_choice"
    titles = {o.id: o.title for o in app.ctx.cases.load("web").options}
    assert gate["choices"] and all(c["title"] == titles[c["id"]] and c["kind"] for c in gate["choices"])

    client.post("/api/cases/web/gate", json={"choice": gate["default_choice"]})
    _wait(client)
    assert client.get("/api/cases/web").json()["gate"]["kind"] == "decision_commit"
    client.post("/api/cases/web/gate", json={"action": "reject", "comment": "отклонено в панели"})
    _wait(client)
    assert client.get("/api/cases/web").json()["gate"]["kind"] == "option_choice"
    client.post("/api/cases/web/gate", json={})
    _wait(client)
    client.post("/api/cases/web/gate", json={"action": "approve", "approver": "руководитель продукта"})
    _wait(client)

    view = client.get("/api/cases/web").json()
    assert view["finished"] and view["gate"] is None
    assert view["executive"]["answer"] and view["steps"]
    assert app.ctx.cases.load("web").decisions[-1].daci.approver == "руководитель продукта"
    assert client.get("/api/cases/web/report").text.startswith("#")
    # шлюза больше нет — ответ на него отклоняется
    assert client.post("/api/cases/web/gate", json={}).status_code == 409


def test_new_case_is_created_from_the_form_and_listed(tmp_path):
    _app, client = _setup(tmp_path, AutoGate())
    files = [("files", ("notes.md", TEXT.encode(), "text/markdown"))]
    data = {"case_id": "fresh", "title": "Новый кейс из панели", "brief": "Почему падает удержание?"}
    assert client.post("/api/cases", data=data, files=files).json() == {"id": "fresh"}
    assert "fresh" in [c["id"] for c in client.get("/api/cases").json()]
    view = client.get("/api/cases/fresh").json()
    assert view["title"] == "Новый кейс из панели" and view["metrics"]["sources"] == 1
    assert client.post("/api/cases", data=data).status_code == 409  # такой кейс уже есть
    assert client.get("/api/cases/nope").status_code == 404
    assert client.post("/api/cases/demo").status_code == 404  # панель запущена без --demo


def test_facts_and_challenge_preview_do_not_change_the_case(tmp_path):
    app, client = _setup(tmp_path, AutoGate())
    assert app.runner.start("web").finished
    version, calls = app.ctx.cases.load("web").version, len(app.ctx.llm.calls)
    facts = client.get("/api/cases/web/facts").json()
    assert facts and facts[0]["claim_id"].startswith("CL-")
    assert facts[0]["support"] in {"подтверждено", "частично", "нет опоры", "опровергнуто"}
    preview = client.post("/api/cases/web/challenge", json={"claim_ids": [facts[0]["claim_id"]]}).json()
    assert preview["removed"] == [facts[0]["claim_id"]]
    assert (app.ctx.cases.load("web").version, len(app.ctx.llm.calls)) == (version, calls)
    assert client.get("/api/cases/web").json()["revision"] is None


def test_revision_view_shows_why_each_conclusion_changed(tmp_path):
    app, client = _setup(tmp_path, AutoGate())
    assert app.runner.start("web").finished
    new = tmp_path / "new.md"
    new.write_text("Свежая когорта апреля показала удержание на уровне 39 процентов.\n", encoding="utf-8")
    assert client.post("/api/cases/web/evidence").status_code == 400  # без файла пересматривать нечего
    client.post("/api/cases/web/evidence", files=[("files", ("new.md", new.read_bytes(), "text/markdown"))])
    _wait(client)
    from strategist.domain.entities import NodeVerdict

    case = app.ctx.cases.load("web")  # фейковая модель ничего не меняет — один узел помечаем изменённым явно
    first = case.revisions[-1].results[0]
    case.revisions[-1].results[0] = first.model_copy(
        update={"verdict": NodeVerdict.REVISED, "before": "было", "after": "стало", "reason": "новые данные"}
    )
    app.ctx.cases.save(case, "тест: узел пересмотра изменён")
    rev = client.get("/api/cases/web").json()["revision"]
    assert rev["summary"] == case.revisions[-1].summary
    assert {"before": "было", "after": "стало", "reason": "новые данные"}.items() <= rev["revised"][0].items()


def test_second_run_while_one_is_going_gets_busy(tmp_path):
    app, client = _setup(tmp_path, AutoGate())
    release = threading.Event()
    app.runner.resume = lambda case_id, response=None: release.wait(10)  # прогон «идёт», пока его не отпустят
    assert client.post("/api/cases/web/run").json()["running"]
    busy = client.post("/api/cases/web/run")
    assert busy.status_code == 409 and "идёт прогон" in busy.json()["detail"]
    assert client.get("/api/cases/web").json()["job"]["running"]
    release.set()
    _wait(client)
    assert client.post("/api/cases/web/run").status_code == 200  # освободилось — снова можно


def test_event_hub_delivers_events_from_worker_threads():
    hub = EventHub()

    async def main() -> list[str]:
        stream = hub.stream()
        first = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)  # поток SSE подписался
        threading.Thread(
            target=hub.publish, args=({"type": "step.completed", "data": {"step": "intake"}},)
        ).start()
        line = await asyncio.wait_for(first, 5)
        await stream.aclose()
        return [line]

    lines = asyncio.run(main())
    assert '"step.completed"' in lines[0] and '"intake"' in lines[0]


def test_reason_clipped_mid_word_by_an_old_run_ends_with_an_ellipsis():
    from strategist.interfaces.web.views import _ended

    old = ("слово " * 60)[:296] + "возр"  # прогон до v0.3.0: ровно 300 символов, обрыв посреди слова
    assert len(old) == 300 and _ended(old).endswith("слово…")
    assert _ended("Короткая причина без точки") == "Короткая причина без точки"
    assert _ended("x " * 149 + "y.") == "x " * 149 + "y."
