"""Агрегат DecisionCase — версионируемый объект решения."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import Field

from strategist.domain.common import ID_PREFIXES, Entity, next_id
from strategist.domain.entities.claims import Claim, ClaimStatus, Unknown
from strategist.domain.entities.decision import Decision, DecisionStatus
from strategist.domain.entities.experiments import Experiment, Forecast
from strategist.domain.entities.options import (
    DecisionMatrix,
    Objection,
    Option,
    OptionKind,
    RoleScore,
    StressResult,
)
from strategist.domain.entities.panel import PanelRun
from strategist.domain.entities.sources import Evidence, Source
from strategist.domain.entities.strategy import ActionStep, CommsStrategy, ProductStrategy


class Stage(StrEnum):
    INTAKE = "intake"
    RESEARCH_PLAN = "research_plan"
    COLLECT = "collect"
    CLAIMS = "claims"
    SITUATION = "situation"
    OPTIONS = "options"
    STRESS = "stress"
    OPTION_CHOICE = "option_choice"
    PRODUCT = "product"
    COMMS = "comms"
    PROMISE_LINT = "promise_lint"
    EXPERIMENTS = "experiments"
    COMMIT = "commit"
    REPORT = "report"
    DONE = "done"

    @property
    def label_ru(self) -> str:
        return STAGE_LABELS[self]


STAGE_LABELS: dict[Stage, str] = {
    Stage.INTAKE: "Постановка задачи",
    Stage.RESEARCH_PLAN: "План исследования",
    Stage.COLLECT: "Сбор материалов",
    Stage.CLAIMS: "Реестр утверждений",
    Stage.SITUATION: "Картина ситуации",
    Stage.OPTIONS: "Варианты",
    Stage.STRESS: "Стресс-тест",
    Stage.OPTION_CHOICE: "Выбор варианта",
    Stage.PRODUCT: "Продуктовая стратегия",
    Stage.COMMS: "Коммуникационная стратегия",
    Stage.PROMISE_LINT: "Сверка обещаний",
    Stage.EXPERIMENTS: "План проверки",
    Stage.COMMIT: "Фиксация решения",
    Stage.REPORT: "Отчёт",
    Stage.DONE: "Готово",
}


class PremiseVerdict(StrEnum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    NOT_CONFIRMED = "not_confirmed"
    UNTESTED = "untested"

    @property
    def label_ru(self) -> str:
        return {
            "confirmed": "подтверждена",
            "partial": "подтверждена частично",
            "not_confirmed": "не подтверждена",
            "untested": "не проверена",
        }[self.value]


class ClarifyingQuestion(Entity):
    question: str
    why: str
    interpretations: list[str] = Field(default_factory=list)
    answer: str | None = None


class DecisionFrame(Entity):
    question: str
    neutral_question: str = ""
    goal: str = ""
    recipient: str = ""
    decision_owner: str = ""
    deadline: date | None = None
    constraints: list[str] = Field(default_factory=list)
    available_data: list[str] = Field(default_factory=list)
    premise: str = ""
    premise_verdict: PremiseVerdict = PremiseVerdict.UNTESTED
    premise_reasoning: str = ""
    premise_claim_ids: list[str] = Field(default_factory=list)
    competing_frames: list[str] = Field(default_factory=list)
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    playbook: str = "generic"


class ResearchQuestionStatus(StrEnum):
    PLANNED = "planned"
    DONE = "done"
    SKIPPED = "skipped"


class ResearchQuestion(Entity):
    id: str
    question: str
    uncertainty_id: str | None = None
    rationale: str = ""
    status: ResearchQuestionStatus = ResearchQuestionStatus.PLANNED
    skip_reason: str = ""
    saved_tokens_estimate: int = 0
    finding_claim_ids: list[str] = Field(default_factory=list)


class ResearchPlan(Entity):
    questions: list[ResearchQuestion] = Field(default_factory=list)
    matrix: DecisionMatrix = Field(default_factory=DecisionMatrix)
    budget_tokens: int = 0
    approved: bool = False
    human_edits: str = ""


class NodeVerdict(StrEnum):
    UNCHANGED = "unchanged"
    REVISED = "revised"
    RETRACTED = "retracted"


class NodeRevision(Entity):
    node_id: str
    node_kind: str
    verdict: NodeVerdict
    before: str = ""
    after: str = ""
    reason: str = ""


class RevisionRecord(Entity):
    id: str
    at: datetime
    new_source_ids: list[str] = Field(default_factory=list)
    new_claim_ids: list[str] = Field(default_factory=list)
    invalidated_claim_ids: list[str] = Field(default_factory=list)
    suspect_ids: list[str] = Field(default_factory=list)
    untouched_ids: list[str] = Field(default_factory=list)
    results: list[NodeRevision] = Field(default_factory=list)
    reopened_decision_id: str | None = None
    tokens_used: int = 0
    tokens_full_regeneration_estimate: int = 0
    summary: str = ""
    trigger: str = (
        "new_evidence"  # что запустило пересмотр: new_evidence (новые данные) или contest (оспорено)
    )
    contested_claim_ids: list[str] = Field(default_factory=list)  # строки, которые оспорил руководитель


class ChangeEntry(Entity):
    version: int
    at: datetime
    stage: Stage
    summary: str
    details: list[str] = Field(default_factory=list)


class ReasonBasis(Entity):
    """На чём держится довод выжимки: тип опоры, оговорка и прошёл ли довод сверку чисел (summary_repair)."""

    claim_ids: list[str] = Field(default_factory=list)
    kind_label: str = ""  # «расчёт по данным», «факт из материалов», «гипотеза», «допущение», «прогноз»…
    source: str = ""  # «SRC-001, период …» у расчёта или «цитата из SRC-003» у факта
    caveat: str = ""
    verified: bool = True  # числа довода сверены с реестром и входами
    replaced: bool = False  # после двух неудачных попыток довод собран из текста опорной строки


class ExecutiveSummary(Entity):
    """Выжимка для руководителя: прямой ответ, доводы с числами, шаги с датами, что нужно от руководителя."""

    answer: str
    reasons: list[str] = Field(default_factory=list)
    basis: list[ReasonBasis] = Field(default_factory=list)  # параллельно reasons; пусто у кейсов до v0.2.0
    unverified: list[str] = Field(default_factory=list)  # поля с несверенными числами: answer, revisit
    next_steps: list[ActionStep] = Field(default_factory=list)
    asks: list[str] = Field(default_factory=list)
    revisit: str = ""
    say_to_team: str = ""
    case_version: int = 0  # по какой версии кейса написана: после пересмотра выжимка пишется заново


class DecisionCase(Entity):
    id: str
    title: str
    brief: str
    created_at: datetime
    updated_at: datetime
    version: int = 0
    stage: Stage = Stage.INTAKE
    frame: DecisionFrame | None = None
    research_plan: ResearchPlan | None = None
    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    options: list[Option] = Field(default_factory=list)
    objections: list[Objection] = Field(default_factory=list)
    role_scores: list[RoleScore] = Field(default_factory=list)
    stress: StressResult | None = None
    selected_option_id: str | None = None
    product: ProductStrategy | None = None
    comms: CommsStrategy | None = None
    experiments: list[Experiment] = Field(default_factory=list)
    forecasts: list[Forecast] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    revisions: list[RevisionRecord] = Field(default_factory=list)
    panels: list[PanelRun] = Field(default_factory=list)  # модельные респонденты: карантин, не реестр
    executive: ExecutiveSummary | None = None
    changelog: list[ChangeEntry] = Field(default_factory=list)

    # --- поиск -------------------------------------------------------------
    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def evidence_item(self, evidence_id: str) -> Evidence | None:
        return next((e for e in self.evidence if e.id == evidence_id), None)

    def claim(self, claim_id: str) -> Claim | None:
        return next((c for c in self.claims if c.id == claim_id), None)

    def option(self, option_id: str) -> Option | None:
        return next((o for o in self.options if o.id == option_id), None)

    def unknown(self, unknown_id: str) -> Unknown | None:
        return next((u for u in self.unknowns if u.id == unknown_id), None)

    def options_of_kind(self, kind: OptionKind) -> list[Option]:
        return [o for o in self.options if o.kind == kind]

    def active_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.status in (ClaimStatus.ACTIVE, ClaimStatus.SUSPECT)]

    def objections_for(self, option_id: str) -> list[Objection]:
        return [o for o in self.objections if o.option_id == option_id]

    def active_decision(self) -> Decision | None:
        live = [d for d in self.decisions if d.status != DecisionStatus.SUPERSEDED]
        return live[-1] if live else None

    def all_ids(self) -> list[str]:
        ids: list[str] = []
        for collection in (
            self.sources,
            self.evidence,
            self.claims,
            self.unknowns,
            self.options,
            self.objections,
            self.experiments,
            self.forecasts,
            self.decisions,
            self.revisions,
            self.panels,
        ):
            ids.extend(item.id for item in collection)
        if self.product:
            ids.extend(f.id for f in self.product.features)
        if self.comms:
            ids.extend(p.id for p in self.comms.promises)
        for decision in self.decisions:
            ids.extend(t.id for t in decision.triggers)
            ids.extend(c.id for c in decision.commitments)
        if self.research_plan:
            ids.extend(q.id for q in self.research_plan.questions)
            ids.extend(u.id for u in self.research_plan.matrix.uncertainties)
        return ids

    # --- выдача идентификаторов -------------------------------------------
    def new_id(self, kind: str, *, reserved: list[str] | None = None) -> str:
        prefix = ID_PREFIXES[kind]
        width = 4 if kind == "decision" else 3
        return next_id(prefix, [*self.all_ids(), *(reserved or [])], width=width)

    def log(self, at: datetime, summary: str, details: list[str] | None = None) -> None:
        self.changelog.append(
            ChangeEntry(
                version=self.version + 1, at=at, stage=self.stage, summary=summary, details=details or []
            )
        )

    def deadline(self) -> date | None:
        return self.frame.deadline if self.frame else None
