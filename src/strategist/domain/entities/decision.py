"""Журнал решения: ADR (без повторов, superseded), MADR Confirmation, DACI, RAND ABP, kill-критерии."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import Field

from strategist.domain.common import Entity
from strategist.domain.entities.options import KillCriterion


class Daci(Entity):
    driver: str = ""
    approver: str = ""
    contributors: list[str] = Field(default_factory=list)
    informed: list[str] = Field(default_factory=list)


class CommitmentStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    DROPPED = "dropped"


class Commitment(Entity):
    id: str
    description: str
    owner: str = ""
    due: date | None = None
    status: CommitmentStatus = CommitmentStatus.OPEN


class TriggerKind(StrEnum):
    SIGNAL = "signal"
    KILL = "kill"
    DATE = "date"
    EVIDENCE = "evidence"


class Trigger(Entity):
    id: str
    kind: TriggerKind
    description: str
    condition: str = ""
    assumption_claim_ids: list[str] = Field(default_factory=list)
    due: date | None = None
    fired: bool = False
    fired_at: datetime | None = None
    fired_reason: str = ""


class DecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    REOPENED = "reopened"
    SUPERSEDED = "superseded"


class Decision(Entity):
    id: str
    selected_option_id: str
    rejected_option_ids: list[str] = Field(default_factory=list)
    rationale: str
    evidence_snapshot: dict[str, str] = Field(default_factory=dict)
    load_bearing_claim_ids: list[str] = Field(default_factory=list)
    vulnerable_claim_ids: list[str] = Field(default_factory=list)
    daci: Daci = Field(default_factory=Daci)
    decision_deadline: date | None = None
    revisit_date: date | None = None
    metrics: list[str] = Field(default_factory=list)
    confirmation: str = ""
    triggers: list[Trigger] = Field(default_factory=list)
    kill_criteria: list[KillCriterion] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    shaping_actions: list[str] = Field(default_factory=list)
    hedging_actions: list[str] = Field(default_factory=list)
    status: DecisionStatus = DecisionStatus.ACCEPTED
    supersedes: str | None = None
    superseded_by: str | None = None
    created_at: datetime
