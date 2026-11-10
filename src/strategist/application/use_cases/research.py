"""PlanResearch: план от вопросов, которые могут изменить решение (ценность информации).

Таблица «варианты × 3–6 неопределённостей × уровни»: исследуем только факторы, меняющие победителя,
остальное — в журнал как «полезно знать, пропущено» с оценкой сэкономленных токенов. Это даёт
исследованию условие остановки и делает стоимость прозрачной.
"""

from __future__ import annotations

from strategist.application import tool_policy
from strategist.application.context import AppContext
from strategist.application.digests import frame_digest, materials_overview
from strategist.application.dto.llm_outputs import ResearchActionDTO, ResearchPlanDTO
from strategist.application.llm_calls import ask_structured
from strategist.application.ports import GateAction, GateKind, GateRequest, GateResponse
from strategist.application.use_cases.collect import compute_metric, research_questions_text
from strategist.domain.entities import (
    DecisionCase,
    DecisionMatrix,
    ResearchPlan,
    ResearchQuestion,
    ResearchQuestionStatus,
    SourceKind,
    Uncertainty,
    UncertaintyLevel,
    Unknown,
    UnknownStatus,
)
from strategist.domain.services.decision_analysis import assess_factors

DEFAULT_SKIP_TOKENS = 15000


def _matrix_from_dto(dto: ResearchPlanDTO) -> tuple[DecisionMatrix, dict[str, str]]:
    """Матрица с ключами-кандидатами вариантов; ключи неопределённостей → U-001…"""
    unc_ids = {u.key: f"U-{i:03d}" for i, u in enumerate(dto.uncertainties, start=1)}
    uncertainties = []
    for u in dto.uncertainties:
        levels = [
            UncertaintyLevel(name=lv.name, description=lv.description, probability=lv.probability)
            for lv in u.levels
        ]
        if not levels:
            continue
        names = [lv.name for lv in levels]
        uncertainties.append(
            Uncertainty(
                id=unc_ids[u.key],
                name=u.name,
                description=u.description,
                levels=levels,
                base_level=u.base_level if u.base_level in names else names[len(names) // 2],
            )
        )
    option_keys = {o.key for o in dto.candidate_options}
    base: dict[str, float] = {}
    effects: dict[str, dict[str, dict[str, float]]] = {}
    for row in dto.utilities:
        if row.option_key not in option_keys:
            continue
        base[row.option_key] = row.base_utility
        effects[row.option_key] = {unc_ids[k]: v for k, v in row.effects.items() if k in unc_ids}
    for key in option_keys - set(base):
        base[key] = 0.0
    return DecisionMatrix(uncertainties=uncertainties, base_utility=base, effects=effects), unc_ids


def plan_research(ctx: AppContext, case: DecisionCase, comment: str = "") -> ResearchPlan:
    max_unc = int(ctx.setting("max_uncertainties", 6))
    dto, usage = ask_structured(
        ctx,
        task="research_plan",
        prompt="research_plan",
        values={
            "frame": frame_digest(case),
            "materials": materials_overview(case),
            "max_uncertainties": max_unc,
            "comment": comment or "—",
            "methodology": ctx.prompts.skill(f"playbook-{case.frame.playbook if case.frame else 'generic'}"),
        },
        schema=ResearchPlanDTO,
        case_id=case.id,
        step="research_plan",
    )
    dto.uncertainties = dto.uncertainties[:max_unc]
    matrix, unc_ids = _matrix_from_dto(dto)
    factors = {f.uncertainty_id: f for f in assess_factors(matrix)} if matrix.base_utility else {}
    by_key = {u.key: u for u in dto.uncertainties}
    questions: list[ResearchQuestion] = []
    reserved: list[str] = []
    budget = 0
    for key, unc_id in unc_ids.items():
        u = by_key[key]
        factor = factors.get(unc_id)
        relevant = factor is None or factor.flips_winner or factor.evpi > 0
        q = ResearchQuestion(
            id=case.new_id("question", reserved=reserved),
            question=u.research_question,
            uncertainty_id=unc_id,
            rationale=(
                f"Меняет победителя (EVPI≈{factor.evpi:.1f})"
                if factor and factor.flips_winner
                else (f"EVPI≈{factor.evpi:.1f}" if factor else "оценка не построена")
            ),
            status=ResearchQuestionStatus.PLANNED if relevant else ResearchQuestionStatus.SKIPPED,
            skip_reason=""
            if relevant
            else "Не меняет выбор варианта ни на одном уровне — исследование не окупается",
            saved_tokens_estimate=0 if relevant else u.estimated_tokens,
        )
        reserved.append(q.id)
        questions.append(q)
        budget += u.estimated_tokens if relevant else 0
        case.unknowns.append(
            Unknown(
                id=case.new_id("unknown"),
                question=u.research_question,
                why_it_matters=u.description or u.name,
                reason="Нет данных в материалах на момент планирования",
                resolution_method=u.resolution_method,
                estimated_cost=f"≈{u.estimated_tokens:,} токенов исследования".replace(",", " "),
                deadline=case.frame.deadline if case.frame else None,
                status=UnknownStatus.OPEN if relevant else UnknownStatus.SKIPPED,
                flips_decision=bool(factor and factor.flips_winner),
                voi=factor.evpi if factor else None,
                saved_tokens_estimate=None if relevant else u.estimated_tokens,
                created_at=ctx.clock.now(),
            )
        )
        for unc in matrix.uncertainties:
            if unc.id == unc_id:
                unc.unknown_id = case.unknowns[-1].id
    for extra in dto.nice_to_know:
        q = ResearchQuestion(
            id=case.new_id("question", reserved=reserved),
            question=extra.question,
            rationale=extra.why_not_needed,
            status=ResearchQuestionStatus.SKIPPED,
            skip_reason="Полезно знать, но не меняет решение: " + extra.why_not_needed,
            saved_tokens_estimate=DEFAULT_SKIP_TOKENS,
        )
        reserved.append(q.id)
        questions.append(q)
    plan = ResearchPlan(
        questions=questions,
        matrix=matrix,
        budget_tokens=budget + usage.input_tokens + usage.output_tokens,
    )
    case.research_plan = plan
    skipped = [q for q in questions if q.status == ResearchQuestionStatus.SKIPPED]
    ctx.emit(
        "research.planned",
        case.id,
        planned=len(questions) - len(skipped),
        skipped=len(skipped),
        saved_tokens=sum(q.saved_tokens_estimate for q in skipped),
    )
    return plan


def plan_summary(case: DecisionCase) -> str:
    plan = case.research_plan
    if plan is None:
        return "План не построен"
    names = {u.id: u.name for u in plan.matrix.uncertainties}
    lines = ["| Вопрос | Неопределённость | Статус | Почему |", "|---|---|---|---|"]
    for q in plan.questions:
        status = "исследовать" if q.status != ResearchQuestionStatus.SKIPPED else "пропустить"
        why = q.rationale if q.status != ResearchQuestionStatus.SKIPPED else q.skip_reason
        lines.append(
            f"| [{q.id}] {q.question} | {names.get(q.uncertainty_id or '', '—')} | {status} | {why} |"
        )
    saved = sum(q.saved_tokens_estimate for q in plan.questions)
    lines.append(f"\nСмета исследования: ~{plan.budget_tokens:,} токенов; сэкономлено пропуском: ~{saved:,}.")
    return "\n".join(lines).replace(",", " ")


def apply_research_gate(ctx: AppContext, case: DecisionCase, response: GateResponse) -> bool:
    """Шлюз «план + смета». Возвращает True, если план принят (с правками или без)."""
    plan = case.research_plan
    if plan is None:
        return False
    if response.action == GateAction.REJECT:
        plan.human_edits = response.comment
        return False
    exclude = set(response.edits.get("exclude", []))
    include = set(response.edits.get("include", []))
    for q in plan.questions:
        if q.id in exclude:
            q.status = ResearchQuestionStatus.SKIPPED
            q.skip_reason = "Исключено руководителем"
        if q.id in include:
            q.status = ResearchQuestionStatus.PLANNED
    reserved: list[str] = []
    for text in response.edits.get("add", []):
        q = ResearchQuestion(
            id=case.new_id("question", reserved=reserved),
            question=str(text),
            rationale="Добавлено руководителем",
        )
        reserved.append(q.id)
        plan.questions.append(q)
    plan.approved = True
    plan.human_edits = response.comment
    ctx.emit("research.approved", case.id, edits=bool(exclude or include or reserved))
    return True


def research_gate_request(case: DecisionCase) -> GateRequest:
    return GateRequest(
        gate=GateKind.RESEARCH_PLAN,
        case_id=case.id,
        title="План исследования и смета",
        summary=plan_summary(case),
        choices=["approve", "edit", "reject"],
        default_choice="approve",
        payload={
            "questions": [
                q.model_dump(mode="json")
                for q in (case.research_plan.questions if case.research_plan else [])
            ]
        },
    )


# --- Исследовательский цикл: модель сама выбирает следующее действие ----------------


def _action_menu(case: DecisionCase, allow_web: bool) -> str:
    tables = [s for s in case.sources if s.kind == SourceKind.TABLE and s.is_valid]
    lines = ["Доступные действия:"]
    if tables:
        lines.append("- compute_metric: посчитать метрику по таблице кодом (pandas + DuckDB). Таблицы:")
        lines += [f"    {s.id}: {s.title}; {s.note[:300]}" for s in tables]
    if allow_web:
        lines.append("- fetch_url: загрузить страницу из разрешённого списка открытых источников (allowlist)")
    lines.append("- search_knowledge: найти методику или материал в базе знаний по запросу")
    lines.append("- finish: данных достаточно или следующий шаг дороже его ценности")
    return "\n".join(lines)


def research_loop(ctx: AppContext, case: DecisionCase, *, max_steps: int | None = None) -> dict[str, int]:
    """Цикл «выбери действие → выполни инструментом → проверь». Ограничен шагами и сметой."""
    from strategist.application.use_cases.start_case import register_materials

    steps = int(max_steps if max_steps is not None else ctx.setting("research.max_steps", 8))
    budget = case.research_plan.budget_tokens if case.research_plan else 0
    used = 0
    counters = {"steps": 0, "metrics": 0, "pages": 0, "searches": 0}
    history: list[str] = []
    for _ in range(steps):
        if budget and used > budget:
            ctx.emit("research.budget_exhausted", case.id, used=used, budget=budget)
            break
        dto, usage = ask_structured(
            ctx,
            task="research_step",
            prompt="research_step",
            values={
                "frame": frame_digest(case),
                "questions": research_questions_text(case),
                "evidence_count": len(case.evidence),
                "claims": "\n".join(f"[{c.id}] {c.text[:200]}" for c in case.claims[-40:]) or "—",
                "menu": _action_menu(case, allow_web=bool(ctx.setting("research.allow_web", True))),
                "history": "\n".join(history[-8:]) or "—",
            },
            schema=ResearchActionDTO,
            case_id=case.id,
            step="research_loop",
        )
        used += usage.input_tokens + usage.output_tokens
        counters["steps"] += 1
        action = dto.action
        if not tool_policy.check(ctx, case.id, "research_step", action):
            history.append(f"{action}: инструмент запрещён политикой шага")
            continue
        if action == "finish":
            history.append(f"finish: {dto.reason}")
            break
        if action == "compute_metric" and dto.metric and dto.table_source_id:
            source = case.source(dto.table_source_id)
            if source is None or source.kind != SourceKind.TABLE:
                history.append(f"compute_metric: нет таблицы {dto.table_source_id}")
                continue
            errors: list[str] = []
            claim = compute_metric(ctx, case, source, dto.metric, [], errors)
            if claim is not None:
                claim.id = case.new_id("claim")
                case.claims.append(claim)
                counters["metrics"] += 1
                history.append(f"compute_metric → [{claim.id}] {claim.text[:160]}")
            else:
                history.append(
                    f"compute_metric не удался — {'; '.join(errors) or 'ошибка расчёта'}. Не повторяй как есть."
                )
        elif action == "fetch_url" and dto.url:
            if not ctx.web.allowed(dto.url):
                ctx.emit("web.blocked", case.id, url=dto.url)
                history.append(f"fetch_url {dto.url}: запрещено (вне allowlist)")
                continue
            try:
                snap = ctx.web.fetch(dto.url)
            except Exception as exc:
                history.append(f"fetch_url {dto.url}: ошибка {type(exc).__name__}")
                continue
            material = ctx.materials.add_bytes(
                case.id,
                f"web-{len(case.sources) + 1}.md",
                f"# {snap.title}\n\n{snap.text}".encode(),
                {
                    "title": snap.title or dto.url,
                    "kind": "web_snapshot",
                    "source_url": snap.final_url,
                    "retrieved": snap.fetched_at.date().isoformat(),
                    "synthetic": False,
                },
            )
            before = len(case.sources)
            from strategist.application.use_cases.start_case import source_from_material

            if not any(s.sha256 == material.sha256 for s in case.sources):
                case.sources.append(source_from_material(case, material))
            counters["pages"] += len(case.sources) - before
            history.append(f"fetch_url → источник {case.sources[-1].id} ({snap.title[:80]}), нужен разбор")
        elif action == "search_knowledge" and dto.query:
            hits = ctx.knowledge.search(dto.query, k=5)
            counters["searches"] += 1
            history.append(
                f"search_knowledge «{dto.query}» → " + "; ".join(f"{h.title or h.doc_id}" for h in hits)[:400]
            )
        else:
            history.append(f"{action}: некорректное действие")
    _ = register_materials  # импорт оставлен для единообразия API модуля
    ctx.emit("research.loop", case.id, **counters, tokens=used)
    return counters
