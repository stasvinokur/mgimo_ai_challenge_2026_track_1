"""GenerateOptions и StressTest: обязательные «оставить» и «закрыть», возражения на доказательствах.

Многораундовые дебаты правильность не повышают (Choi et al.); помогают возражения, обязанные цитировать
доказательства (Khan et al.), и голосование с перестановкой порядка (Doshi et al.). Варианты-клоны
отсекаются по эмбеддингам обоснований плюс попарный вопрос decide().
"""

from __future__ import annotations

from strategist.application.context import AppContext
from strategist.application.digests import claims_digest, frame_digest, options_digest, unknowns_digest
from strategist.application.dto.llm_outputs import (
    DecisionModelDTO,
    LevelUpdateDTO,
    OptionDTO,
    OptionsDTO,
    PremortemDTO,
    RolePanelDTO,
    SteelmanDTO,
)
from strategist.application.llm_calls import ask_structured
from strategist.application.ports import DecisionKind, DecisionQuestion
from strategist.application.utils import enum_or, known_ids, parse_date
from strategist.domain.entities import (
    MANDATORY_OPTION_KINDS,
    Claim,
    ClaimType,
    DecisionCase,
    DecisionMatrix,
    KillCriterion,
    Objection,
    ObjectionKind,
    Option,
    OptionKind,
    OptionStatus,
    Origin,
    RoleScore,
    ScoreOrder,
    StressResult,
    Uncertainty,
)
from strategist.domain.services.decision_analysis import recenter_effects, stress_test
from strategist.domain.services.ranking import PanelResult, aggregate_panel
from strategist.domain.services.similarity import find_clone_pairs

ROLES = {
    "Финансовый директор": (
        "Ты отвечаешь за выручку, затраты и окупаемость: оцени деньги варианта, включая выручку, которую теряем "
        "при отказе от направления; скептичен к недоказанному спросу."
    ),
    "Директор по маркетингу": (
        "Ты отвечаешь за привлечение, удержание аудитории и бренд: оцени, сможем ли объяснить ценность и "
        "привлечь людей, и что потеряем в доверии и охвате, если направление свернуть."
    ),
    "Адвокат клиента": (
        "Ты защищаешь интересы текущих и будущих пользователей: оцени, решает ли вариант их реальную задачу и не "
        "ухудшает ли опыт; вариант, который бросает уже подключённых клиентов без плана для них, для тебя плохой."
    ),
}


def _option_from_dto(ctx: AppContext, case: DecisionCase, dto: OptionDTO, reserved: list[str]) -> Option:
    valid = {c.id for c in case.claims}
    assumption_ids = []
    for a in dto.assumptions:
        claim = Claim(
            id=case.new_id("claim", reserved=reserved),
            text=a.text,
            type=ClaimType.ASSUMPTION,
            what_would_change=a.what_would_change,
            origin=Origin.AGENT_INFERENCE,
            vulnerable=True,
            created_at=ctx.clock.now(),
        )
        reserved.append(claim.id)
        case.claims.append(claim)
        assumption_ids.append(claim.id)
    option = Option(
        id=case.new_id("option", reserved=reserved),
        kind=enum_or(OptionKind, dto.kind, OptionKind.OTHER),
        title=dto.title,
        summary=dto.summary,
        rationale=dto.rationale,
        supporting_claim_ids=known_ids(dto.supporting_claim_ids, valid),
        assumption_claim_ids=assumption_ids,
        conscious_refusals=dto.conscious_refusals,
        kill_criterion=KillCriterion(
            signal=dto.kill_criterion.signal,
            threshold=dto.kill_criterion.threshold,
            deadline=parse_date(dto.kill_criterion.deadline),
        ),
        costs=dto.costs,
        revenue=dto.revenue,
    )
    reserved.append(option.id)
    return option


def _missing_kinds(options: list[Option]) -> list[OptionKind]:
    kinds = {o.kind for o in options}
    return [k for k in MANDATORY_OPTION_KINDS if k not in kinds]


MANDATORY_STUBS = {
    OptionKind.STATUS_QUO: (
        "Оставить как есть",
        "Не менять продукт; исправить измерение и наблюдать ключевые метрики ещё один цикл.",
        "Отказываемся от изменений продукта и коммуникации до получения данных.",
    ),
    OptionKind.KILL: (
        "Закрыть или заморозить направление",
        "Свернуть развитие, перераспределить команду и бюджет на более перспективные направления.",
        "Отказываемся от дальнейших инвестиций в направление.",
    ),
}


def _mandatory_stub(case: DecisionCase, kind: OptionKind, reserved: list[str]) -> Option:
    title, summary, refusal = MANDATORY_STUBS[kind]
    option = Option(
        id=case.new_id("option", reserved=reserved),
        kind=kind,
        title=title,
        summary=summary,
        rationale="Добавлено правилом: вариант обязателен для честного сравнения (модель его не предложила).",
        conscious_refusals=[refusal],
        kill_criterion=KillCriterion(
            signal="Ключевая метрика продолжает ухудшаться", threshold="ещё один цикл"
        ),
    )
    reserved.append(option.id)
    return option


def generate_options(ctx: AppContext, case: DecisionCase) -> list[Option]:
    min_options = int(ctx.setting("min_options", 4))
    feedback = ""
    options: list[Option] = []
    for attempt in range(2):
        before = len(case.claims)
        dto, _ = ask_structured(
            ctx,
            task="options",
            prompt="options",
            values={
                "frame": frame_digest(case),
                "claims": claims_digest(case),
                "unknowns": unknowns_digest(case),
                "min_options": min_options,
                "feedback": feedback or "—",
                "today": ctx.clock.today().isoformat(),
            },
            schema=OptionsDTO,
            case_id=case.id,
            step="options",
        )
        reserved: list[str] = []
        options = [_option_from_dto(ctx, case, o, reserved) for o in dto.options]
        missing = _missing_kinds(options)
        if (not missing and len(options) >= min_options) or attempt == 1:
            break
        case.claims = case.claims[:before]  # допущения отброшенной попытки не остаются в реестре
        feedback = (
            "Предыдущий ответ не прошёл проверку: "
            + (f"нет обязательных вариантов {', '.join(k.label_ru for k in missing)}; " if missing else "")
            + (f"вариантов меньше {min_options}. " if len(options) < min_options else "")
            + "Верни полный набор заново."
        )
    reserved = [o.id for o in options]
    for kind in _missing_kinds(options):
        options.append(_mandatory_stub(case, kind, reserved))
        ctx.emit("options.mandatory_added", case.id, kind=kind.value)
    case.options = options
    mark_clones(ctx, case)
    ctx.emit("options.generated", case.id, count=len(options), kinds=[o.kind.value for o in options])
    return options


def mark_clones(ctx: AppContext, case: DecisionCase) -> list[tuple[str, str]]:
    """Клоны: высокий косинус обоснований и decide() «по существу не отличаются»."""
    threshold = float(ctx.setting("options.distinctness_threshold", 0.92))
    texts = {o.id: f"{o.title}. {o.summary}. {o.rationale}" for o in case.options}
    vectors = dict(zip(texts, ctx.embeddings.embed(list(texts.values())), strict=True))
    clones = []
    for a, b, sim in find_clone_pairs(vectors, threshold):
        oa, ob = case.option(a), case.option(b)
        if (
            oa is None
            or ob is None
            or OptionKind.STATUS_QUO in (oa.kind, ob.kind)
            or OptionKind.KILL in (oa.kind, ob.kind)
        ):
            continue
        result = ctx.decision.decide(
            DecisionQuestion(
                id=f"{case.id}:{a}:{b}:distinct",
                kind=DecisionKind.NOUL,
                instructions="Отличаются ли эти два варианта по существу (что делаем, для кого, от чего отказываемся)?",
                state=f"Вариант 1: {texts[a]}\nВариант 2: {texts[b]}",
                task="distinct_options",
                case_id=case.id,
                run_id=ctx.run_id,
                step="options",
            )
        )
        if not result.yes:
            ob.status = OptionStatus.CLONE
            ob.clone_of = a
            clones.append((a, b))
            ctx.emit("options.clone", case.id, option=b, clone_of=a, similarity=sim)
    return clones


def active_options(case: DecisionCase) -> list[Option]:
    return [o for o in case.options if o.status != OptionStatus.CLONE]


# --- Возражения ----------------------------------------------------------------------


def premortem(ctx: AppContext, case: DecisionCase, option: Option) -> list[Objection]:
    """Свежий контекст: «прошло 12 месяцев, вариант провалился — назови пять причин»."""
    dto, _ = ask_structured(
        ctx,
        task="premortem",
        prompt="premortem",
        values={
            "frame": frame_digest(case),
            "option": f"{option.title}: {option.summary}",
            "claims": claims_digest(case),
        },
        schema=PremortemDTO,
        case_id=case.id,
        step=f"stress:premortem:{option.id}",
    )
    valid = {c.id for c in case.claims}
    out = []
    for reason in dto.reasons[:5]:
        ids = known_ids(reason.claim_ids, valid)
        out.append(
            Objection(
                id="",
                option_id=option.id,
                kind=ObjectionKind.PREMORTEM,
                text=reason.text,
                claim_ids=ids,
                is_hypothesis=reason.is_hypothesis or not ids,
            )
        )
    return out


def steelman(ctx: AppContext, case: DecisionCase, option: Option) -> Objection | None:
    others = [o for o in active_options(case) if o.id != option.id]
    if not others:
        return None
    dto, _ = ask_structured(
        ctx,
        task="steelman",
        prompt="steelman",
        values={
            "frame": frame_digest(case),
            "option": f"[{option.id}] {option.title}: {option.summary}",
            "competitors": "\n".join(f"[{o.id}] {o.title}: {o.summary}" for o in others),
            "claims": claims_digest(case),
        },
        schema=SteelmanDTO,
        case_id=case.id,
        step=f"stress:steelman:{option.id}",
    )
    ids = known_ids(dto.claim_ids, {c.id for c in case.claims})
    return Objection(
        id="",
        option_id=option.id,
        kind=ObjectionKind.STEELMAN,
        text=f"Сильнейший конкурент {dto.competitor_option_id}: {dto.argument}",
        claim_ids=ids,
        is_hypothesis=dto.is_hypothesis or not ids,
    )


def score_objections(ctx: AppContext, case: DecisionCase, objections: list[Objection]) -> None:
    levels = ["1 — слабое", "2 — частное", "3 — существенное", "4 — сильное", "5 — критическое"]
    questions = [
        DecisionQuestion(
            id=f"{case.id}:obj:{i}",
            kind=DecisionKind.SCORE,
            instructions="Насколько сильно возражение против варианта (с учётом опоры на данные)?",
            state=f"Возражение: {o.text}\nОпора: {', '.join(o.claim_ids) or 'нет (гипотеза)'}",
            levels=levels,
            task="objection_strength",
            case_id=case.id,
            run_id=ctx.run_id,
            step="stress",
        )
        for i, o in enumerate(objections)
    ]
    for o, r in zip(objections, ctx.decision.decide_many(questions), strict=True):
        if r.score is not None:
            o.strength = max(1, min(5, round(r.score) + 1))


def collect_objections(ctx: AppContext, case: DecisionCase) -> list[Objection]:
    objections: list[Objection] = []
    for option in active_options(case):
        objections += premortem(ctx, case, option)
        sm = steelman(ctx, case, option)
        if sm is not None:
            objections.append(sm)
    score_objections(ctx, case, objections)
    reserved: list[str] = []
    for o in objections:
        o.id = case.new_id("objection", reserved=reserved)
        reserved.append(o.id)
    case.objections = objections
    ctx.emit(
        "stress.objections",
        case.id,
        count=len(objections),
        hypotheses=sum(o.is_hypothesis for o in objections),
    )
    return objections


# --- Панель ролей и модель решения ---------------------------------------------------


def role_panel(ctx: AppContext, case: DecisionCase) -> PanelResult:
    """Три роли оценивают варианты дважды — в прямом и обратном порядке; авторство скрыто."""
    options = active_options(case)
    scores: list[RoleScore] = []
    for role, brief in ROLES.items():
        for order in (ScoreOrder.FORWARD, ScoreOrder.REVERSE):
            ordered = options if order == ScoreOrder.FORWARD else list(reversed(options))
            labels = {f"Вариант {chr(65 + i)}": o for i, o in enumerate(ordered)}
            listing = "\n".join(
                f"{label}: {o.title}. {o.summary} Возражения: "
                + "; ".join(obj.text[:140] for obj in case.objections_for(o.id)[:3])
                for label, o in labels.items()
            )
            dto, _ = ask_structured(
                ctx,
                task="role_panel",
                prompt="role_panel",
                values={
                    "role": role,
                    "role_brief": brief,
                    "frame": frame_digest(case),
                    "options": listing,
                    "claims": claims_digest(case, limit=60),
                },
                schema=RolePanelDTO,
                case_id=case.id,
                step=f"stress:panel:{order.value}",
            )
            for item in dto.scores:
                option = labels.get(item.option_label.strip())
                if option is None:
                    continue
                scores.append(
                    RoleScore(
                        role=role,
                        option_id=option.id,
                        score=item.score,
                        order=order,
                        rationale=item.rationale,
                    )
                )
    case.role_scores = scores
    result = aggregate_panel(scores)
    ctx.emit("stress.panel", case.id, winner=result.winner, order_sensitive=result.order_sensitive)
    return result


def decision_model(
    ctx: AppContext, case: DecisionCase, new_claim_ids: list[str] | None = None
) -> DecisionMatrix:
    """Матрица полезности реальных вариантов на неопределённостях плана исследования.

    new_claim_ids — строки, пришедшие после решения: при пересмотре они идут отдельным блоком и первыми в реестре,
    иначе лимит выжимки мог бы их отрезать и модель оценила бы варианты по старым данным."""
    plan_matrix = case.research_plan.matrix if case.research_plan else DecisionMatrix()
    uncertainties = plan_matrix.uncertainties
    unc_text = (
        "\n".join(
            f"{u.id}: {u.name} — уровни: "
            + ", ".join(f"{lv.name} (p={lv.probability:.2f})" for lv in u.levels)
            for u in uncertainties
        )
        or "—"
    )
    dto, _ = ask_structured(
        ctx,
        task="decision_model",
        prompt="decision_model",
        values={
            "frame": frame_digest(case),
            "options": options_digest(case),
            "uncertainties": unc_text,
            "new_evidence": claims_digest(case, new_claim_ids) if new_claim_ids else "—",
            "claims": claims_digest(case, limit=80, priority=new_claim_ids),
        },
        schema=DecisionModelDTO,
        case_id=case.id,
        step="stress:model",
    )
    before = {u.id: u.base_level for u in uncertainties}
    uncertainties = _apply_level_updates(ctx, case, uncertainties, dto.level_updates, new_claim_ids or [])
    moved = {u.id: u.base_level for u in uncertainties if u.base_level != before.get(u.id)}
    ids = {o.id for o in active_options(case)}
    unc_ids = {u.id for u in uncertainties}
    base: dict[str, float] = {}
    effects: dict[str, dict[str, dict[str, float]]] = {}
    for row in dto.utilities:
        if row.option_key in ids:
            base[row.option_key] = row.base_utility
            effects[row.option_key] = {k: v for k, v in row.effects.items() if k in unc_ids}
    # Полезность модель оценила уже с учётом новых данных; эффекты отсчитываются от базового уровня. Базовый
    # уровень сдвинулся на наблюдаемый — пересчитываем эффекты от него, иначе новые данные учтутся дважды.
    effects = recenter_effects(effects, moved)
    for oid in ids - set(base):
        base[oid] = 0.0
    return DecisionMatrix(uncertainties=uncertainties, base_utility=base, effects=effects, note=dto.note)


def _apply_level_updates(
    ctx: AppContext,
    case: DecisionCase,
    uncertainties: list[Uncertainty],
    updates: list[LevelUpdateDTO],
    new_claim_ids: list[str],
) -> list[Uncertainty]:
    """Новые данные разрешили неопределённость → базовый сценарий на наблюдаемом уровне (вероятность 0,9).

    Принимается только обновление со ссылкой на новую строку реестра: без новых данных неопределённость
    не «разрешается» по мнению модели. Остаток 0,1 делится между прочими уровнями — данные бывают неполными."""
    allowed = set(new_claim_ids)
    if not allowed or not updates:
        return uncertainties
    by_id = {u.id: u for u in uncertainties}
    out = {u.id: u for u in uncertainties}
    for upd in updates:
        unc = by_id.get(upd.uncertainty_id)
        names = [lv.name for lv in unc.levels] if unc else []
        if unc is None or upd.observed_level not in names or not set(upd.evidence) & allowed:
            continue
        others = max(len(names) - 1, 1)
        levels = [
            lv.model_copy(update={"probability": 0.9 if lv.name == upd.observed_level else 0.1 / others})
            for lv in unc.levels
        ]
        out[unc.id] = unc.model_copy(update={"levels": levels, "base_level": upd.observed_level})
        ctx.emit(
            "stress.uncertainty_resolved",
            case.id,
            uncertainty=unc.id,
            level=upd.observed_level,
            evidence=sorted(set(upd.evidence) & allowed),
        )
    return [out[u.id] for u in uncertainties]


def run_stress_test(
    ctx: AppContext, case: DecisionCase, new_claim_ids: list[str] | None = None
) -> StressResult:
    matrix = decision_model(ctx, case, new_claim_ids)
    max_factors = int(ctx.setting("stress.max_scenario_factors", 4))
    result = stress_test(matrix, max_factors=max_factors)
    case.stress = result
    if case.research_plan is not None:
        case.research_plan.matrix = matrix
    _link_unknowns(case, matrix)
    _mark_load_bearing(case, result)
    ctx.emit(
        "stress.tested",
        case.id,
        winner=result.base_winner,
        survived=result.scenarios_survived,
        total=result.scenarios_total,
    )
    return result


def _link_unknowns(case: DecisionCase, matrix: DecisionMatrix) -> None:
    """Реестр неизвестного ↔ решения: какие варианты зависят от неопределённости (ненулевой эффект)."""
    for unc in matrix.uncertainties:
        unknown = case.unknown(unc.unknown_id or "")
        if unknown is None:
            continue
        unknown.linked_option_ids = sorted(
            opt for opt, effects in matrix.effects.items() if any(v for v in effects.get(unc.id, {}).values())
        )


def _mark_load_bearing(case: DecisionCase, result: StressResult) -> None:
    winner = case.option(result.base_winner)
    if winner is None:
        return
    critical_claims = set()
    if case.research_plan:
        for u in case.research_plan.matrix.uncertainties:
            if u.id in result.critical_uncertainty_ids:
                critical_claims |= set(u.linked_claim_ids)
    for cid in {*winner.supporting_claim_ids, *winner.assumption_claim_ids, *critical_claims}:
        claim = case.claim(cid)
        if claim is not None:
            claim.load_bearing = True
            if claim.type in (ClaimType.ASSUMPTION, ClaimType.HYPOTHESIS) or claim.reliability.value in (
                "low",
                "very_low",
            ):
                claim.vulnerable = True


def recommended_option(case: DecisionCase, panel: PanelResult | None = None) -> str | None:
    """По умолчанию на шлюзе — победитель стресс-теста; при расхождении с панелью это видно человеку."""
    if case.stress is not None:
        return case.stress.base_winner
    return panel.winner if panel else None
