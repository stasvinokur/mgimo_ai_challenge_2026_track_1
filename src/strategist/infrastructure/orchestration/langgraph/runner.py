"""LangGraphRunner — адаптер WorkflowRunner: запуск, продолжение после перерыва, статус, пересмотр.

Состояние графа — в cases/<id>/state/checkpoints.sqlite (SqliteSaver). Продолжить можно из нового
процесса или нового чата: достаточно `strategist resume <case>` — граф продолжит с последнего шлюза или шага.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from strategist.application.context import AppContext
from strategist.application.ports import GateRequest, GateResponse, JournalStatus, RunStatus
from strategist.application.use_cases.integrity import ensure_integrity
from strategist.application.use_cases.start_case import register_materials
from strategist.infrastructure.orchestration.langgraph.graph import build_revision_graph, build_strategy_graph


class LangGraphRunner:
    def __init__(
        self, ctx: AppContext, cases_dir: Path, *, max_concurrency: int = 4, recursion_limit: int = 120
    ) -> None:
        self.ctx = ctx
        self.cases_dir = Path(cases_dir)
        self.max_concurrency = max_concurrency
        self.recursion_limit = recursion_limit

    @contextmanager
    def _compiled(self, case_id: str, kind: str = "main"):
        path = self.cases_dir / case_id / "state" / "checkpoints.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        try:
            saver = SqliteSaver(conn)
            builder = build_strategy_graph(self.ctx) if kind == "main" else build_revision_graph(self.ctx)
            yield builder.compile(checkpointer=saver)
        finally:
            conn.close()

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "max_concurrency": self.max_concurrency,
            "recursion_limit": self.recursion_limit,
        }

    @staticmethod
    def _pending_interrupt(snapshot: Any) -> GateRequest | None:
        interrupts = list(getattr(snapshot, "interrupts", ()) or ())
        if not interrupts:
            for task in getattr(snapshot, "tasks", ()) or ():
                interrupts += list(getattr(task, "interrupts", ()) or ())
        if not interrupts:
            return None
        value = interrupts[0].value
        return GateRequest.model_validate(value) if isinstance(value, dict) else None

    def _status(self, case_id: str, graph: Any, thread_id: str) -> RunStatus:
        snapshot = graph.get_state(self._config(thread_id))
        case = self.ctx.cases.load(case_id)
        done_steps = [e.step for e in self.ctx.journal.entries(case_id) if e.status == JournalStatus.DONE]
        gate = self._pending_interrupt(snapshot)
        finished = not snapshot.next and gate is None and bool(snapshot.values)
        return RunStatus(
            case_id=case_id,
            stage=case.stage.value,
            stage_label=case.stage.label_ru,
            waiting_gate=gate,
            finished=finished,
            completed_steps=done_steps,
            cost=self.ctx.costs.summary(case_id),
        )

    def _thread(self, case_id: str) -> str:
        return f"{case_id}:main"

    def start(self, case_id: str) -> RunStatus:
        ensure_integrity(self.ctx, case_id)
        self.ctx.run_id = "main"
        thread = self._thread(case_id)
        with self._compiled(case_id) as graph:
            snapshot = graph.get_state(self._config(thread))
            if snapshot.values:  # граф уже запускался — продолжаем, а не начинаем заново
                return self._continue(case_id, graph, thread, None)
            latest = self.ctx.cases.load(case_id).version
            graph.invoke({"case_id": case_id, "version": latest, "ingest_results": []}, self._config(thread))
            return self._announce(self._status(case_id, graph, thread))

    def _continue(self, case_id: str, graph: Any, thread: str, response: GateResponse | None) -> RunStatus:
        snapshot = graph.get_state(self._config(thread))
        gate = self._pending_interrupt(snapshot)
        if gate is not None:
            if response is None:
                return self._status(case_id, graph, thread)
            graph.invoke(Command(resume=response.model_dump(mode="json")), self._config(thread))
        elif snapshot.next:
            graph.invoke(None, self._config(thread))  # обрыв процесса посреди шага — продолжаем с него
        return self._announce(self._status(case_id, graph, thread))

    def _announce(self, status: RunStatus) -> RunStatus:
        """Граф остановился на шлюзе — событие gate.waiting (уведомить руководителя, показать в панели)."""
        gate = status.waiting_gate
        if gate is not None:
            self.ctx.emit("gate.waiting", gate.case_id, gate=gate.gate.value, title=gate.title)
        return status

    def resume(self, case_id: str, response: GateResponse | None = None) -> RunStatus:
        ensure_integrity(self.ctx, case_id)
        self.ctx.run_id = "main"
        thread = self._thread(case_id)
        with self._compiled(case_id) as graph:
            if not graph.get_state(self._config(thread)).values:
                return self.start(case_id)
            return self._continue(case_id, graph, thread, response)

    def status(self, case_id: str) -> RunStatus:
        thread = self._thread(case_id)
        with self._compiled(case_id) as graph:
            return self._status(case_id, graph, thread)

    def revise(self, case_id: str, new_material_paths: list[str]) -> RunStatus:
        _ = register_materials  # регистрация выполняется внутри сценария пересмотра
        return self._revision(case_id, {"new_paths": [str(p) for p in new_material_paths]})

    def contest(self, case_id: str, claim_ids: list[str], reason: str) -> RunStatus:
        """Пересмотр по оспоренным основаниям: без новых файлов, те же шаги графа пересмотра."""
        return self._revision(case_id, {"contest": {"claim_ids": list(claim_ids), "reason": reason}})

    def _revision(self, case_id: str, payload: dict) -> RunStatus:
        ensure_integrity(self.ctx, case_id)
        case = self.ctx.cases.load(case_id)
        n = len(case.revisions) + 1
        self.ctx.run_id = f"revision-{n}"
        thread = f"{case_id}:revision:{n}"
        with self._compiled(case_id, kind="revision") as graph:
            snapshot = graph.get_state(self._config(thread))
            if snapshot.values and snapshot.next:
                graph.invoke(None, self._config(thread))
            elif not snapshot.values:
                graph.invoke({"case_id": case_id, "version": case.version, **payload}, self._config(thread))
            status = self._status(case_id, graph, thread)
        self.ctx.run_id = "main"
        return status
