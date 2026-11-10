"""Оркестрация на LangGraph: граф стратегии и граф пересмотра.

LangGraph живёт только здесь (decision-2). Узлы — тонкие обёртки над use cases:
загрузить кейс ПО ВЕРСИИ ИЗ СОСТОЯНИЯ ГРАФА → выполнить сценарий → сохранить новую версию.
Поэтому повтор прерванного узла начинается с той же исходной версии и не применяет изменения дважды,
а ответы моделей берутся из кэша — без повторных вызовов. Шлюзы человека — interrupt().
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from strategist.application.context import AppContext
from strategist.application.ports import GateAction, GateRequest, GateResponse
from strategist.application.steps import register_suspension, run_step
from strategist.application.use_cases import (
    claims as claims_uc,
)
from strategist.application.use_cases import (
    collect,
    decision,
    executive,
    experiments,
    frame,
    options,
    report,
    research,
    revise,
    strategy,
)
from strategist.application.use_cases.collect import IngestResult
from strategist.application.use_cases.integrity import ensure_integrity
from strategist.domain.entities import DecisionCase, Stage

# interrupt() в узле — пауза до ответа человека, а не сбой шага
register_suspension(GraphBubbleUp)


def accumulate(old: list[dict[str, Any]] | None, new: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Редьюсер результатов читателей: дописывает; пустой список — очистка после слияния."""
    if new is None:
        return old or []
    if not new:
        return []
    return [*(old or []), *new]


class GraphState(TypedDict, total=False):
    case_id: str
    version: int
    stage: str
    promise_loops: int
    research_rejects: int
    reader_source_id: str
    ingest_results: Annotated[list[dict[str, Any]], accumulate]
    new_paths: list[str]
    contest: dict[str, Any]  # {"claim_ids": [...], "reason": "..."} — пересмотр по оспоренным основаниям


class LangGraphGate:
    """HumanGate через interrupt(): граф сохраняет состояние и ждёт ответа (в т. ч. из нового процесса)."""

    def ask(self, request: GateRequest) -> GateResponse:
        value = interrupt(request.model_dump(mode="json"))
        if isinstance(value, GateResponse):
            return value
        return GateResponse.model_validate(value or {})


class AutoGate:
    """Скриптовые прогоны (стенд сравнения, тесты): принимает вариант по умолчанию."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.requests: list[GateRequest] = []

    def ask(self, request: GateRequest) -> GateResponse:
        self.requests.append(request)
        preset = self.answers.get(request.gate.value)
        if isinstance(preset, GateResponse):
            return preset
        if isinstance(preset, dict):
            return GateResponse.model_validate(preset)
        return GateResponse(action=GateAction.APPROVE, choice=request.default_choice, approver="auto")


def _work(
    ctx: AppContext,
    state: GraphState,
    step: str,
    fn: Callable[[DecisionCase], str],
    next_stage: Stage,
) -> GraphState:
    """Журналируемый шаг: base-версия → сценарий → новая версия кейса."""
    case_id = state["case_id"]
    base = state["version"]

    def body() -> dict[str, Any]:
        case = ctx.cases.load(case_id, version=base)
        summary = fn(case)
        case.stage = next_stage
        case.updated_at = ctx.clock.now()
        case.log(ctx.clock.now(), summary)
        version = ctx.cases.save(case, f"{step}: {summary}"[:200])
        return {"version": version, "summary": summary}

    payload = run_step(ctx, case_id, step, {"base": base, "step": step}, body)
    return {"version": int(payload["version"]), "stage": next_stage.value}


def build_strategy_graph(ctx: AppContext) -> StateGraph:
    g: StateGraph = StateGraph(GraphState)

    def intake(state: GraphState) -> GraphState:
        ensure_integrity(ctx, state["case_id"])

        def fn(case: DecisionCase) -> str:
            f = frame.frame_decision(ctx, case)
            return (
                f"рамка: {f.question[:80]}; уточнений: {len(f.clarifying_questions)}; плейбук: {f.playbook}"
            )

        return _work(ctx, state, "intake", fn, Stage.INTAKE)

    def clarify(state: GraphState) -> GraphState:
        case = ctx.cases.load(state["case_id"], version=state["version"])
        if case.frame is None or not case.frame.clarifying_questions:
            return {}

        def fn(c: DecisionCase) -> str:
            answered = frame.ask_clarifications(ctx, c)
            return f"получено ответов: {answered}"

        return _work(ctx, state, "clarify", fn, Stage.RESEARCH_PLAN)

    def plan(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            comment = (
                case.research_plan.human_edits
                if case.research_plan and not case.research_plan.approved
                else ""
            )
            case.unknowns = []  # план пересобирается целиком вместе с реестром неизвестного
            p = research.plan_research(ctx, case, comment=comment)
            planned = sum(q.status.value == "planned" for q in p.questions)
            return f"вопросов исследовать: {planned}, пропустить: {len(p.questions) - planned}"

        return _work(ctx, state, f"research_plan:{state.get('research_rejects', 0)}", fn, Stage.RESEARCH_PLAN)

    def research_gate(state: GraphState) -> GraphState:
        case = ctx.cases.load(state["case_id"], version=state["version"])
        response = ctx.ask_gate(research.research_gate_request(case))
        approved = {"value": False}

        def fn(c: DecisionCase) -> str:
            approved["value"] = research.apply_research_gate(ctx, c, response)
            return "план принят" if approved["value"] else f"план отклонён: {response.comment[:80]}"

        update = _work(ctx, state, f"research_gate:{state.get('research_rejects', 0)}", fn, Stage.COLLECT)
        if not approved["value"]:
            update["research_rejects"] = state.get("research_rejects", 0) + 1
            update["stage"] = Stage.RESEARCH_PLAN.value
        return update

    def after_research_gate(state: GraphState) -> str:
        return (
            "plan"
            if state.get("stage") == Stage.RESEARCH_PLAN.value and state.get("research_rejects", 0) < 3
            else "dispatch"
        )

    def dispatch(state: GraphState) -> GraphState:
        return {}

    def to_readers(state: GraphState) -> list[Send] | str:
        case = ctx.cases.load(state["case_id"], version=state["version"])
        pending = [s.id for s in case.sources if s.is_valid and not s.ingested]
        if not pending:
            return "merge"
        return [Send("read", {**state, "reader_source_id": sid, "ingest_results": []}) for sid in pending]

    def read(state: GraphState) -> GraphState:
        case = ctx.cases.load(state["case_id"], version=state["version"])
        sid = state["reader_source_id"]
        payload = run_step(
            ctx,
            case.id,
            f"read:{sid}",
            {"base": state["version"], "source": sid},
            lambda: collect.ingest_source(ctx, case, sid).model_dump(mode="json"),
        )
        return {"ingest_results": [payload]}

    def merge(state: GraphState) -> GraphState:
        results = [IngestResult.model_validate(r) for r in state.get("ingest_results", [])]

        def fn(case: DecisionCase) -> str:
            counts = collect.merge_ingest(ctx, case, results)
            return f"цитат: {counts['evidence']}, метрик: {counts['claims']}, сбоев: {counts['failed']}"

        update = _work(ctx, state, "merge", fn, Stage.COLLECT)
        update["ingest_results"] = []
        return update

    def research_node(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            counters = research.research_loop(ctx, case)
            fresh = [s.id for s in case.sources if s.is_valid and not s.ingested]
            results = [collect.ingest_source(ctx, case, sid) for sid in fresh]
            if results:
                collect.merge_ingest(ctx, case, results)
            return f"шагов исследования: {counters['steps']}, метрик: {counters['metrics']}, страниц: {counters['pages']}"

        return _work(ctx, state, "research_loop", fn, Stage.CLAIMS)

    def claims_node(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            new = claims_uc.extract_claims(ctx, case)
            counters = claims_uc.verify_claims(ctx, case)
            return f"утверждений: {len(new)}, проверено: {counters['checked']}, эскалаций: {counters['escalated']}"

        return _work(ctx, state, "claims", fn, Stage.SITUATION)

    def situation(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            return "самоаудит: " + report.build_situation(ctx, case).headline()

        return _work(ctx, state, "situation", fn, Stage.OPTIONS)

    def options_node(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            opts = options.generate_options(ctx, case)
            return "варианты: " + ", ".join(o.kind.value for o in opts)

        return _work(ctx, state, "options", fn, Stage.STRESS)

    def stress(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            options.collect_objections(ctx, case)
            panel = options.role_panel(ctx, case)
            result = options.run_stress_test(ctx, case)
            return f"{result.summary} Панель: {panel.note}"

        return _work(ctx, state, "stress", fn, Stage.OPTION_CHOICE)

    def option_gate(state: GraphState) -> GraphState:
        case = ctx.cases.load(state["case_id"], version=state["version"])
        default = options.recommended_option(case)
        response = ctx.ask_gate(decision.option_choice_request(case, default))

        def fn(c: DecisionCase) -> str:
            chosen = decision.apply_option_choice(ctx, c, response, default)
            return f"выбран вариант {chosen}"

        return _work(ctx, state, "option_gate", fn, Stage.PRODUCT)

    def product(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            p = strategy.compile_product(ctx, case)
            return f"продукт: функций {len(p.features)}, сегментов {len(p.segment_ids)}"

        return _work(ctx, state, "product", fn, Stage.COMMS)

    def comms(state: GraphState) -> GraphState:
        loop = state.get("promise_loops", 0)

        def fn(case: DecisionCase) -> str:
            c = strategy.compile_comms(ctx, case, feedback=strategy.promise_feedback(case) if loop else "")
            return f"коммуникация: обещаний {len(c.promises)}, каналов {len(c.channels)}"

        return _work(ctx, state, f"comms:{loop}", fn, Stage.PROMISE_LINT)

    def promise_lint(state: GraphState) -> GraphState:
        loop = state.get("promise_loops", 0)
        blocked = {"n": 0}

        def fn(case: DecisionCase) -> str:
            blocked["n"] = strategy.lint_promises_step(ctx, case)
            if blocked["n"] and loop >= 1:
                removed = strategy.drop_blocked_promises(case)
                return f"сверка: заблокировано {blocked['n']}, снято {len(removed)}"
            return f"сверка: заблокировано {blocked['n']}"

        update = _work(ctx, state, f"promise_lint:{loop}", fn, Stage.EXPERIMENTS)
        if blocked["n"] and loop < 1:
            update["promise_loops"] = loop + 1
            update["stage"] = Stage.COMMS.value
        return update

    def after_lint(state: GraphState) -> str:
        return "comms" if state.get("stage") == Stage.COMMS.value else "experiments"

    def experiments_node(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            exps = experiments.plan_experiments(ctx, case)
            fcs = experiments.make_forecasts(ctx, case)
            return f"экспериментов: {len(exps)}, прогнозов: {len(fcs)}"

        return _work(ctx, state, "experiments", fn, Stage.COMMIT)

    def commit_gate(state: GraphState) -> GraphState:
        case = ctx.cases.load(state["case_id"], version=state["version"])
        response = ctx.ask_gate(decision.commit_request(case))
        if response.action == GateAction.REJECT:

            def back(c: DecisionCase) -> str:
                c.selected_option_id = None
                return f"фиксация отклонена: {response.comment[:80]}"

            update = _work(ctx, state, "commit_gate:reject", back, Stage.OPTION_CHOICE)
            return update

        def fn(c: DecisionCase) -> str:
            d = decision.commit_decision(ctx, c, response)
            return f"решение {d.id} зафиксировано, сигналов пересмотра: {len(d.triggers)}"

        return _work(ctx, state, "commit", fn, Stage.REPORT)

    def after_commit(state: GraphState) -> str:
        return "option_gate" if state.get("stage") == Stage.OPTION_CHOICE.value else "summary"

    def summary_node(state: GraphState) -> GraphState:
        return _summary(ctx, state)

    def report_node(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            paths = report.build_report(ctx, case)
            return "отчёт: " + paths.get("markdown", "в памяти")

        return _work(ctx, state, "report", fn, Stage.DONE)

    def after_intake(state: GraphState) -> str:
        case = ctx.cases.load(state["case_id"], version=state["version"])
        return "clarify" if case.frame and case.frame.clarifying_questions else "plan"

    g.add_node("intake", intake)
    g.add_node("clarify", clarify)
    g.add_node("plan", plan)
    g.add_node("research_gate", research_gate)
    g.add_node("dispatch", dispatch)
    g.add_node("read", read)
    g.add_node("merge", merge)
    g.add_node("research", research_node)
    g.add_node("claims", claims_node)
    g.add_node("situation", situation)
    g.add_node("options", options_node)
    g.add_node("stress", stress)
    g.add_node("option_gate", option_gate)
    g.add_node("product", product)
    g.add_node("comms", comms)
    g.add_node("promise_lint", promise_lint)
    g.add_node("experiments", experiments_node)
    g.add_node("commit_gate", commit_gate)
    g.add_node("summary", summary_node)
    g.add_node("report", report_node)

    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", after_intake, ["clarify", "plan"])
    g.add_edge("clarify", "plan")
    g.add_edge("plan", "research_gate")
    g.add_conditional_edges("research_gate", after_research_gate, ["plan", "dispatch"])
    g.add_conditional_edges("dispatch", to_readers, ["read", "merge"])
    g.add_edge("read", "merge")
    g.add_edge("merge", "research")
    g.add_edge("research", "claims")
    g.add_edge("claims", "situation")
    g.add_edge("situation", "options")
    g.add_edge("options", "stress")
    g.add_edge("stress", "option_gate")
    g.add_edge("option_gate", "product")
    g.add_edge("product", "comms")
    g.add_edge("comms", "promise_lint")
    g.add_conditional_edges("promise_lint", after_lint, ["comms", "experiments"])
    g.add_edge("experiments", "commit_gate")
    g.add_conditional_edges("commit_gate", after_commit, ["option_gate", "summary"])
    g.add_edge("summary", "report")
    g.add_edge("report", END)
    return g


def _summary(ctx: AppContext, state: GraphState, step: str = "summary") -> GraphState:
    """Выжимка для руководителя — только если есть решение; без него шаг пропускается."""

    def fn(case: DecisionCase) -> str:
        if case.active_decision() is None:
            return "решения нет — выжимка не нужна"
        s = executive.write_executive_summary(ctx, case)
        return f"выжимка: доводов {len(s.reasons)}, шагов {len(s.next_steps)}"

    return _work(ctx, state, step, fn, Stage.REPORT)


def build_revision_graph(ctx: AppContext) -> StateGraph:
    g: StateGraph = StateGraph(GraphState)

    def revise_node(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            contest = state.get("contest")
            if contest:
                record = revise.revise_on_contest(ctx, case, contest["claim_ids"], contest.get("reason", ""))
            else:
                record = revise.revise_on_new_evidence(ctx, case, state.get("new_paths", []))
            return record.summary

        return _work(ctx, state, "revise", fn, Stage.REPORT)

    def report_node(state: GraphState) -> GraphState:
        def fn(case: DecisionCase) -> str:
            paths = report.build_report(ctx, case)
            return "отчёт обновлён: " + paths.get("markdown", "")

        return _work(ctx, state, "report", fn, Stage.DONE)

    def summary_node(state: GraphState) -> GraphState:
        return _summary(ctx, state, step="summary:revision")

    g.add_node("revise", revise_node)
    g.add_node("summary", summary_node)
    g.add_node("report", report_node)
    g.add_edge(START, "revise")
    g.add_edge("revise", "summary")
    g.add_edge("summary", "report")
    g.add_edge("report", END)
    return g


STRATEGY_NODES = [
    "intake",
    "clarify",
    "plan",
    "research_gate",
    "dispatch",
    "read",
    "merge",
    "research",
    "claims",
    "situation",
    "options",
    "stress",
    "option_gate",
    "product",
    "comms",
    "promise_lint",
    "experiments",
    "commit_gate",
    "summary",
    "report",
]
