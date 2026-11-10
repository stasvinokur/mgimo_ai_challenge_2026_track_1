"""Выжимка для руководителя: прямой ответ, три довода с числами, шаги с датами, что нужно от руководителя.

Пишется после фиксации решения и заново после каждого пересмотра. Решение не меняет — излагает. Числа в ней
проверяет та же сверка, что и всё повествование, служебные коды вырезаются.

С флагом summary_repair работает промпт v2 (executive_summary_v2: число довода — из строки, на которую он ссылается;
довод на гипотезе или допущении так и называется) и числа выжимки сверяются сразу: число довода должно быть в
процитированной строке реестра (в той же единице) или выводиться из неё, число ответа и условия пересмотра — во
входах шага. Ошибка — один повтор с замечаниями в слоте {{feedback}}; вторая ошибка — довод собирается из текста
опорной строки (ничего не удаляется молча), у ответа и условия пересмотра ставится пометка «не сверено». У каждого
довода — тип опоры и оговорка. Без флага — промпт и путь v0.1.1.
"""

from __future__ import annotations

import re

from strategist.application.context import AppContext
from strategist.application.digests import claims_digest, frame_digest, today_line
from strategist.application.dto.llm_outputs import ExecutiveSummaryDTO
from strategist.application.llm_calls import ask_structured
from strategist.application.quality import enabled
from strategist.domain.entities import (
    ActionStep,
    ClaimType,
    DecisionCase,
    DecisionStatus,
    ExecutiveSummary,
    OptionStatus,
    ReasonBasis,
    TriggerKind,
)
from strategist.domain.services.citations import BARE_REF_RE, expand_ranges
from strategist.domain.services.numbers import VERDICT_RU, Verdict, check_text

_SERVICE_CODE = re.compile(r"\s*\(?\[?\b(?:OPT|UNK|EXP|FEAT|PRM|DEC|SEG|TRG|RQ)-\d+\b\]?\)?")


def strip_service_codes(text: str) -> str:
    """Коды вариантов, неизвестных, экспериментов и т. п. руководителю не нужны; ссылки [CL-…] остаются."""
    return re.sub(r"\s{2,}", " ", _SERVICE_CODE.sub("", text or "")).strip()


_FALSE_REOPEN = re.compile(r"^\s*Решение открыто заново[^:.—]*[:.—]\s*", re.I)


def drop_false_reopen(answer: str, reopened: bool) -> str:
    """Модель иногда начинает ответ с «Решение открыто заново:», хотя решение принято и не пересматривалось
    (правило промпта условное, а модель применяет его всегда). Статус решения знает код, а не модель."""
    if reopened:
        return answer
    cleaned = _FALSE_REOPEN.sub("", answer, count=1)
    return cleaned[:1].upper() + cleaned[1:] if cleaned else answer


def _decision_text(case: DecisionCase) -> str:
    decision = case.active_decision()
    selected = case.option(case.selected_option_id or "")
    leader = next((o for o in case.options if o.status == OptionStatus.REOPENED), None)
    if decision and decision.status == DecisionStatus.REOPENED:
        rev = case.revisions[-1].summary if case.revisions else ""
        if leader:
            return (
                f"Решение открыто заново по новым данным ({rev}). Прежняя рекомендация: "
                f"{selected.title if selected else '—'}. Новая рекомендация: {leader.title} — {leader.summary}"
            )
        return f"Решение открыто заново по новым данным ({rev}). Рекомендация пока прежняя: " + (
            f"{selected.title} — {selected.summary}" if selected else "—"
        )
    if selected is None:
        return "—"
    return f"{selected.title} — {selected.summary}\nОбоснование: {decision.rationale if decision else selected.rationale}"


def _plan_text(case: DecisionCase) -> str:
    lines = []
    if case.product:
        lines += [
            f"- {s.when}: {s.action} ({s.owner or '—'}; {s.metric or '—'})" for s in case.product.action_plan
        ]
    for e in case.experiments[:3]:
        kill = e.kill_criterion
        lines.append(
            f"- проверка: {e.hypothesis} Метрика: {e.metric}"
            + (f", baseline {e.baseline:g}" if e.baseline is not None else "")
            + (f"; kill: {kill.signal} {kill.threshold or ''} до {kill.deadline}" if kill else "")
            + (f"; ответственный: {e.owner}" if e.owner else "")
        )
    decision = case.active_decision()
    if decision:
        lines += [
            f"- обязательство: {c.description} ({c.owner or '—'}, до {c.due or '—'})"
            for c in decision.commitments
        ]
    return "\n".join(lines) or "—"


def _revisit_text(case: DecisionCase) -> str:
    decision = case.active_decision()
    if decision is None:
        return "—"
    lines = [
        f"- {t.description} {t.condition}".strip() + (" — СРАБОТАЛ" if t.fired else "")
        for t in decision.triggers
        if t.kind in (TriggerKind.KILL, TriggerKind.SIGNAL)
    ][:6]
    lines += [
        f"- kill: {k.signal} — {k.threshold or '—'} до {k.deadline or '—'}"
        for k in decision.kill_criteria[:3]
    ]
    if decision.revisit_date:
        lines.append(f"- плановый пересмотр не позднее {decision.revisit_date}")
    return "\n".join(lines) or "—"


KIND_LABEL = {
    ClaimType.FACT: "факт из материалов",
    ClaimType.DERIVED_METRIC: "расчёт по данным",
    ClaimType.HYPOTHESIS: "гипотеза",
    ClaimType.ASSUMPTION: "допущение",
    ClaimType.FORECAST: "прогноз",
    ClaimType.SYNTHETIC: "синтетика — не доказательство",
    ClaimType.UNKNOWN: "неизвестно",
}
# Слабее опора — выше в списке: тип довода определяется самой слабой процитированной строкой
_WEAKEST = [
    ClaimType.SYNTHETIC,
    ClaimType.UNKNOWN,
    ClaimType.HYPOTHESIS,
    ClaimType.ASSUMPTION,
    ClaimType.FORECAST,
    ClaimType.DERIVED_METRIC,
    ClaimType.FACT,
]
_REASON_FLAGS = {Verdict.UNSUPPORTED, Verdict.UNIT_MISMATCH, Verdict.ELSEWHERE, Verdict.NO_REF}
_FREE_FLAGS = {Verdict.UNSUPPORTED, Verdict.UNIT_MISMATCH}  # в ответе и пересмотре ссылок нет — это не ошибка
_CALC_NOTE = re.compile(r"\s*\[расчёт:[^\]]*\]")


def _refs(text: str) -> list[str]:
    return list(dict.fromkeys(BARE_REF_RE.findall(expand_ranges(text))))


def summary_problems(
    case: DecisionCase, dto: ExecutiveSummaryDTO, inputs: str, reason_inputs: str
) -> dict[str, list[str]]:
    """Числа выжимки без опоры: довод — по процитированным строкам, брифу и рамке задачи (`reason_inputs`), ответ
    и пересмотр — по всем входам шага.

    Число довода должно прийти через строку, на которую он ссылается. Решение, стресс-тест, план и список реестра во
    входах шага содержат числа других строк и результаты модели: если считать их опорой, довод со ссылкой на другую
    строку проходил бы проверку. Ключ — поле (reason:0, answer, revisit), значение — замечания для повтора."""
    claims = {c.id: c.text for c in case.active_claims()}
    allowed = f"{case.brief}\n{inputs}"
    own = f"{case.brief}\n{reason_inputs}"
    out: dict[str, list[str]] = {}
    for i, reason in enumerate(dto.reasons[:3]):
        bad = [f for f in check_text(reason, claims, brief=own) if f.verdict in _REASON_FLAGS]
        if bad:
            out[f"reason:{i}"] = [f"довод {i + 1}: {f.number} — {VERDICT_RU[f.verdict]}" for f in bad]
    for name, text in (("answer", dto.answer), ("revisit", dto.revisit)):
        bad = [f for f in check_text(text, claims, brief=allowed) if f.verdict in _FREE_FLAGS]
        if bad:
            out[name] = [
                f"{'ответ' if name == 'answer' else 'пересмотр'}: {f.number} — {VERDICT_RU[f.verdict]}"
                for f in bad
            ]
    return out


def _feedback(problems: dict[str, list[str]], dto: ExecutiveSummaryDTO) -> str:
    """Замечания для слота {{feedback}} промпта v2: что не сошлось и прошлые доводы — чтобы исправить названные, а
    верные оставить."""
    lines = [x for group in problems.values() for x in group]
    previous = "\n".join(f"{i + 1}) {r}" for i, r in enumerate(dto.reasons[:3]))
    return (
        "проверка нашла ошибки в числах:\n- "
        + "\n- ".join(lines)
        + f"\nДоводы прошлого ответа:\n{previous}\n"
        "Исправь названное: число довода — из строки реестра, на которую он ссылается [CL-…], в той же единице, "
        "или выведено из неё; в ответе и условии пересмотра — только числа из переданных данных."
    )


def _fallback_reason(case: DecisionCase, reason: str, used: set[str]) -> tuple[str, list[str]]:
    """Довод из текста опорной строки: её же ссылка, без технической формулы расчёта, не длиннее 220 знаков."""
    decision = case.active_decision()
    candidates = [*_refs(reason), *(decision.load_bearing_claim_ids if decision else [])]
    cid = next((c for c in candidates if case.claim(c) and c not in used), None) or next(
        (c for c in candidates if case.claim(c)), None
    )
    if cid is None:
        return reason, _refs(reason)
    claim = case.claim(cid)
    assert claim is not None
    text = _CALC_NOTE.sub("", claim.text).split(". Внимание")[0].split(". По «")[0].strip()
    if len(text) > 220:
        text = text[:220].rsplit(" ", 1)[0] + "…"
    used.add(cid)
    return f"{text} [{cid}]", [cid]


def basis_for(
    case: DecisionCase, reason: str, *, verified: bool = True, replaced: bool = False
) -> ReasonBasis:
    """Тип опоры (по самой слабой процитированной строке), источник и период расчёта, оговорка."""
    ids = [cid for cid in _refs(reason) if case.claim(cid)]
    claims = [c for cid in ids if (c := case.claim(cid)) is not None]
    if not claims:
        return ReasonBasis(kind_label="без ссылки на реестр", verified=verified, replaced=replaced)
    weakest = min(claims, key=lambda c: _WEAKEST.index(c.type) if c.type in _WEAKEST else len(_WEAKEST))
    source = ""
    comp = weakest.computation
    if comp and comp.source_id:
        source = comp.source_id + (f", период {comp.period}" if comp.period else "")
    elif weakest.evidence_ids:
        ev = case.evidence_item(weakest.evidence_ids[0])
        source = f"цитата из {ev.source_id}" if ev else ""
    caveats: list[str] = []
    decision = case.active_decision()
    for c in claims:
        if "Внимание:" in c.text:
            caveats.append(c.text.split("Внимание:", 1)[1].split(";")[0].split(". ")[0].strip())
        if c.type in (ClaimType.HYPOTHESIS, ClaimType.ASSUMPTION) and c.what_would_change:
            caveats.append(f"изменит вывод: {c.what_would_change}")
        if c.vulnerable or (decision and c.id in decision.vulnerable_claim_ids):
            caveats.append("уязвимое основание решения")
        if c.type == ClaimType.SYNTHETIC:
            caveats.append("синтетика не доказывает спрос")
    caveat = "; ".join(dict.fromkeys(caveats))
    if len(caveat) > 160:
        caveat = caveat[:160].rsplit(" ", 1)[0] + "…"
    return ReasonBasis(
        claim_ids=ids,
        kind_label=KIND_LABEL.get(weakest.type, weakest.type.value),
        source=source,
        caveat=caveat,
        verified=verified,
        replaced=replaced,
    )


def write_executive_summary(ctx: AppContext, case: DecisionCase) -> ExecutiveSummary:
    decision = case.active_decision()
    key_ids: list[str] = []
    if decision:
        key_ids += decision.load_bearing_claim_ids + decision.vulnerable_claim_ids
    if case.revisions:
        key_ids += case.revisions[-1].new_claim_ids
    values = {
        "today": today_line(ctx.clock.today()),
        "frame": frame_digest(case),
        "decision": _decision_text(case),
        "stress": case.stress.summary if case.stress else "—",
        "claims": claims_digest(case, key_ids, limit=30) if key_ids else claims_digest(case, limit=30),
        "plan": _plan_text(case),
        "revisit": _revisit_text(case),
        "message": case.comms.key_message if case.comms else "—",
    }
    repair = enabled(ctx.settings, "summary_repair")
    # Без флага — промпт v0.1.1 как есть; с флагом — v2: правила о числах доводов и слот замечаний проверки
    prompt = "executive_summary_v2" if repair else "executive_summary"
    inputs = "\n".join(str(v) for v in values.values())
    dto, _ = ask_structured(
        ctx,
        task="executive_summary",
        prompt=prompt,
        values={**values, "feedback": "—"} if repair else values,
        schema=ExecutiveSummaryDTO,
        case_id=case.id,
        step="summary",
    )
    problems: dict[str, list[str]] = {}
    if repair:
        reason_inputs = f"{values['today']}\n{values['frame']}"
        problems = summary_problems(case, dto, inputs, reason_inputs)
        if problems:
            fixed, _ = ask_structured(
                ctx,
                task="executive_summary",
                prompt=prompt,
                values={**values, "feedback": _feedback(problems, dto)},
                schema=ExecutiveSummaryDTO,
                case_id=case.id,
                step="summary:fix1",
            )
            dto, problems = fixed, summary_problems(case, fixed, inputs, reason_inputs)
            ctx.emit("summary.repaired", case.id, remaining=sum(len(v) for v in problems.values()))
    reopened = decision is not None and decision.status == DecisionStatus.REOPENED
    reasons = [strip_service_codes(r) for r in dto.reasons[:3]]
    basis: list[ReasonBasis] = []
    if repair:
        used: set[str] = set()
        for i, reason in enumerate(reasons):
            if f"reason:{i}" in problems:  # вторая неудача: довод — из текста опорной строки
                reasons[i], _ = _fallback_reason(case, reason, used)
                basis.append(basis_for(case, reasons[i], replaced=True))
                ctx.emit("summary.fallback", case.id, reason=i + 1)
            else:
                used.update(_refs(reason))
                basis.append(basis_for(case, reason))
    summary = ExecutiveSummary(
        answer=drop_false_reopen(strip_service_codes(dto.answer), reopened),
        reasons=reasons,
        basis=basis,
        unverified=[k for k in ("answer", "revisit") if k in problems],
        next_steps=[
            ActionStep(when=s.when, action=strip_service_codes(s.action), owner=s.owner, metric=s.metric)
            for s in dto.next_steps[:5]
        ],
        asks=[strip_service_codes(a) for a in dto.asks[:3]],
        revisit=strip_service_codes(dto.revisit),
        say_to_team=strip_service_codes(dto.say_to_team),
        case_version=case.version,
    )
    case.executive = summary
    ctx.emit("summary.written", case.id, reasons=len(summary.reasons), steps=len(summary.next_steps))
    return summary
