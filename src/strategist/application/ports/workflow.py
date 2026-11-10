"""Порт исполнителя рабочего процесса — его вызывают интерфейсы (CLI, MCP, UI)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from strategist.application.ports.runtime import CostSummary, GateRequest, GateResponse


class RunStatus(BaseModel):
    case_id: str
    stage: str
    stage_label: str = ""
    waiting_gate: GateRequest | None = None
    finished: bool = False
    error: str = ""
    completed_steps: list[str] = Field(default_factory=list)
    cost: CostSummary | None = None


class WorkflowRunner(Protocol):
    def start(self, case_id: str) -> RunStatus: ...

    def resume(self, case_id: str, response: GateResponse | None = None) -> RunStatus: ...

    def status(self, case_id: str) -> RunStatus: ...

    def revise(self, case_id: str, new_material_paths: list[str]) -> RunStatus: ...

    def contest(self, case_id: str, claim_ids: list[str], reason: str) -> RunStatus: ...
