"""Веб-панель стратега: JSON API поверх тех же use cases, что у CLI и MCP, и собранная страница (web/ → static/).

Запуск: `uv run strategist ui` (или `--demo` — демо по записанным ответам моделей, без моделей).
Долгие операции (прогон до шлюза, ответ на шлюз, новые данные, оспаривание) идут в фоне по одной (jobs.py),
ход работы приходит в панель событиями (`GET /api/events`, SSE).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from strategist.application.ports import Event, GateAction, GateResponse
from strategist.application.use_cases.challenge import audit_view, preview_challenge
from strategist.application.use_cases.report import render_markdown
from strategist.application.use_cases.start_case import start_case
from strategist.interfaces.demo import DEMO_CASE_ID, demo_inputs
from strategist.interfaces.web import views
from strategist.interfaces.web.jobs import Busy, EventHub, Job, JobRunner

STATIC = Path(__file__).resolve().parent / "static"
# Шаги, которые панель показывает как ход работы; остальное (вызовы моделей) — в журнале
LIVE_EVENTS = {
    "step.started",
    "step.completed",
    "step.failed",
    "step.skipped",
    "gate.waiting",
    "gate.answered",
    "injection.detected",
    "revision.completed",
    "decision.committed",
    "report.built",
    "case.created",
}


class GateAnswer(BaseModel):
    action: str = "approve"
    choice: str | None = None
    answers: list[str] = Field(default_factory=list)
    comment: str = ""
    approver: str = ""
    edits: dict[str, list[str]] = Field(default_factory=dict)


class ClaimIds(BaseModel):
    claim_ids: list[str]


class Contest(ClaimIds):
    reason: str


def _event_json(event: Event) -> dict[str, Any]:
    data = {
        k: (v if isinstance(v, int | float | bool) else str(v)[:200]) for k, v in (event.data or {}).items()
    }
    return {"type": event.type, "case_id": event.case_id, "at": event.at.isoformat(), "data": data}


def create_app(application: Any, *, demo_source: Path | None = None) -> FastAPI:
    """application — то, что отдаёт `bootstrap.build_app`: ctx, runner, subscribe. demo_source — каталог демо-кейса:
    в режиме демо панель создаёт его кнопкой «Открыть демо», а ответы на шлюзы — как при записи (кассеты узнают
    запрос по тексту, любой другой ответ даёт промах)."""
    ctx, runner = application.ctx, application.runner
    hub = EventHub()

    def on_done(job: Job) -> None:
        hub.publish({"type": "job.finished", "case_id": job.case_id, "data": job.to_json()})

    jobs = JobRunner(on_done=on_done)
    application.subscribe(lambda e: hub.publish(_event_json(e)) if e.type in LIVE_EVENTS else None)

    api = FastAPI(title="Стратег Фабрики проектов", docs_url="/api/docs", openapi_url="/api/openapi.json")
    api.state.jobs = jobs  # тестам: дождаться фонового прогона

    def load(case_id: str) -> Any:
        if not ctx.cases.exists(case_id):
            raise HTTPException(404, f"нет кейса {case_id}")
        return ctx.cases.load(case_id)

    def submit(case_id: str, kind: str, fn: Any) -> dict[str, Any]:
        load(case_id)
        try:
            job = jobs.submit(case_id, kind, fn)
        except Busy as exc:
            raise HTTPException(409, str(exc)) from None
        hub.publish({"type": "job.started", "case_id": case_id, "data": job.to_json()})
        return job.to_json()

    @api.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {"demo": demo_source is not None, "demo_case_id": DEMO_CASE_ID}

    @api.get("/api/cases")
    def cases() -> list[dict[str, Any]]:
        return [views.case_list_item(c) for c in ctx.cases.list_cases()]

    @api.post("/api/cases")
    def create_case(
        case_id: str = Form(...),
        title: str = Form(""),
        brief: str = Form(...),
        files: list[UploadFile] = File(default_factory=list),
    ) -> dict[str, Any]:
        if ctx.cases.exists(case_id):
            raise HTTPException(409, f"кейс {case_id} уже есть")
        tmp = Path(tempfile.mkdtemp(prefix="strategist-upload-"))
        paths = []
        for f in files:
            target = tmp / Path(f.filename or "file").name
            target.write_bytes(f.file.read())
            paths.append(str(target))
        case = start_case(ctx, case_id=case_id, title=title or case_id, brief=brief, material_paths=paths)
        return {"id": case.id}

    @api.post("/api/cases/demo")
    def open_demo() -> dict[str, Any]:
        if demo_source is None:
            raise HTTPException(404, "панель запущена без --demo")
        if not ctx.cases.exists(DEMO_CASE_ID):
            d = demo_inputs(demo_source)
            start_case(ctx, case_id=DEMO_CASE_ID, title=d.title, brief=d.brief, material_paths=d.materials)
        return {"id": DEMO_CASE_ID}

    @api.get("/api/cases/{case_id}")
    def case(case_id: str) -> dict[str, Any]:
        c = load(case_id)
        view = views.case_view(c, runner.status(case_id), ctx.clock.today())
        job = jobs.current
        view["job"] = job.to_json() if job and job.case_id == case_id else None
        return view

    @api.post("/api/cases/{case_id}/run")
    def run(case_id: str) -> dict[str, Any]:
        return submit(case_id, "run", lambda: runner.resume(case_id))

    @api.post("/api/cases/{case_id}/gate")
    def gate(case_id: str, answer: GateAnswer) -> dict[str, Any]:
        waiting = runner.status(case_id).waiting_gate
        if waiting is None:
            raise HTTPException(409, "кейс не ждёт решения человека")
        if demo_source is not None:
            # демо воспроизводит запись: ответ как у записанного прогона (принять, вариант по умолчанию)
            response = GateResponse(action=GateAction.APPROVE, choice=waiting.default_choice, approver="auto")
        else:
            response = GateResponse(
                action=GateAction(answer.action),
                choice=answer.choice,
                answers=answer.answers,
                comment=answer.comment,
                approver=answer.approver,
                edits=answer.edits,
            )
        return submit(case_id, "gate", lambda: runner.resume(case_id, response))

    @api.post("/api/cases/{case_id}/evidence")
    def evidence(case_id: str, files: list[UploadFile] = File(default_factory=list)) -> dict[str, Any]:
        if demo_source is not None and case_id == DEMO_CASE_ID:
            # в демо — те же новые данные, что у записи (итоги A/B-теста): файл выбирать не нужно
            paths = demo_inputs(demo_source).new_evidence
        else:
            if not files:
                raise HTTPException(400, "нужен хотя бы один файл с новыми данными")
            tmp = Path(tempfile.mkdtemp(prefix="strategist-new-"))
            paths = []
            for f in files:
                target = tmp / Path(f.filename or "file").name
                target.write_bytes(f.file.read())
                paths.append(str(target))
        return submit(case_id, "revise", lambda: runner.revise(case_id, paths))

    @api.get("/api/cases/{case_id}/facts")
    def facts(case_id: str, limit: int = 30) -> list[dict[str, Any]]:
        load(case_id)
        rows = [r.model_dump(mode="json") for r in audit_view(ctx, case_id, max(1, min(limit, 60)))]
        for r in rows:  # опора — по-русски, как в отчёте
            r["support"] = views.SUPPORT_RU.get(r["support"], r["support"])
        return rows

    @api.post("/api/cases/{case_id}/challenge")
    def challenge(case_id: str, body: ClaimIds) -> dict[str, Any]:
        load(case_id)
        return preview_challenge(ctx, case_id, body.claim_ids).model_dump(mode="json")

    @api.post("/api/cases/{case_id}/contest")
    def contest(case_id: str, body: Contest) -> dict[str, Any]:
        return submit(case_id, "contest", lambda: runner.contest(case_id, body.claim_ids, body.reason))

    @api.get("/api/cases/{case_id}/report", response_class=PlainTextResponse)
    def report(case_id: str) -> str:
        return render_markdown(ctx, load(case_id))

    @api.get("/api/job")
    def job() -> dict[str, Any] | None:
        return jobs.current.to_json() if jobs.current else None

    @api.get("/api/events")
    async def events() -> EventSourceResponse:
        return EventSourceResponse({"data": line} async for line in hub.stream())

    if (STATIC / "index.html").exists():
        api.mount("/", StaticFiles(directory=STATIC, html=True), name="panel")
    else:

        @api.get("/", response_class=HTMLResponse)
        def missing() -> str:
            return "<p>Сборки панели нет: <code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code>.</p>"

    return api
