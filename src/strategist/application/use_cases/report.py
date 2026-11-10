"""BuildReport: бизнес-документ для руководителя, собранный только из реестра.

Каждое утверждение ведёт ссылкой к строке реестра и источнику. В отчёт не попадают строки с ошибками
линтера; длинные предложения без ссылок на реестр считаются и показываются на панели самоаудита.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from enum import StrEnum

from strategist.application.context import AppContext
from strategist.application.errors import SyntheticQuarantineViolation
from strategist.application.ports import CostSummary
from strategist.application.quality import enabled
from strategist.application.use_cases.executive import strip_service_codes
from strategist.application.use_cases.frame import check_premise
from strategist.domain.entities import (
    ClaimType,
    DecisionCase,
    DecisionStatus,
    NodeVerdict,
    ObjectionKind,
    OptionStatus,
    PromiseStatus,
)
from strategist.domain.services.challenge import claim_sources
from strategist.domain.services.citations import REF_RE, number_mismatches, numbers_in, uncited_sentences
from strategist.domain.services.claim_linter import reportable
from strategist.domain.services.dependency_graph import NODE_KIND_RU
from strategist.domain.services.numbers import VERDICT_RU, NumberFinding, Verdict, check_text
from strategist.domain.services.ranking import aggregate_panel
from strategist.domain.services.revisit_rules import stale_grounds, what_to_decide_now
from strategist.domain.services.self_audit import SelfAudit, compute_self_audit
from strategist.domain.services.synthetic_panel import quarantine_violations

MODEL_STRESS = (
    "*Расчёт модели агента по заданным допущениям, а не данные из материалов: сценарии и границы показывают, "
    "при каких значениях неопределённостей меняется выбор.*"
)
MODEL_FORECASTS = "*Оценки модели агента, а не данные: проверяются по Brier после даты разрешения.*"


def build_situation(ctx: AppContext, case: DecisionCase) -> SelfAudit:
    """Картина ситуации: вердикт по посылке запроса + панель самоаудита.

    Перед картиной ситуации — карантин синтетики: модельные респонденты в фактах или в числах о спросе
    останавливают работу (обычно такое не возникает: извлечение и расчёт сами помечают их как SYNTHETIC)."""
    ensure_synthetic_quarantine(case)
    check_premise(ctx, case)
    problems, _ = number_problems(ctx, case)
    audit = compute_self_audit(
        case,
        uncited_sentences=len(narrative_uncited(case)),
        number_mismatches=len(problems),
    )
    ctx.emit("situation.built", case.id, headline=audit.headline())
    return audit


def ensure_synthetic_quarantine(case: DecisionCase) -> None:
    problems = quarantine_violations(case)
    if problems:
        raise SyntheticQuarantineViolation(problems)


STATUS_RU = {"accepted": "принято", "reopened": "открыто заново", "superseded": "заменено"}
# значения перечислений в тексте отчёта — по-русски; ключ — имя перечисления
ENUM_RU: dict[str, dict[str, str]] = {
    "DecisionStatus": STATUS_RU,
    "OptionStatus": {
        "candidate": "кандидат",
        "selected": "выбран",
        "rejected": "отклонён",
        "reopened": "открыт заново",
        "clone": "клон",
    },
    "ClaimStatus": {
        "active": "действует",
        "suspect": "под сомнением",
        "retracted": "отозван",
        "superseded": "заменён",
    },
    "UnknownStatus": {
        "open": "открыт",
        "researching": "исследуется",
        "resolved": "закрыт",
        "skipped": "пропущен",
    },
    "TriggerKind": {"signal": "сигнал", "kill": "kill-критерий", "date": "дата", "evidence": "новые данные"},
    "NodeVerdict": {"unchanged": "без изменений", "revised": "пересмотрен", "retracted": "снят"},
    "SourceKind": {
        "document": "документ",
        "table": "таблица",
        "web": "веб",
        "interview": "интервью",
        "note": "заметка",
        "user_input": "ввод пользователя",
    },
}


# флаг ru_labels на время сборки отчёта (render_markdown); вне отчёта (MCP, CLI) подписи русские
_RU_LABELS: ContextVar[bool] = ContextVar("ru_labels", default=True)


def ru(value: StrEnum) -> str:
    """Значение перечисления по-русски; при выключенном ru_labels — само значение, как в v0.1.1."""
    if not _RU_LABELS.get():
        return value.value
    return (
        ENUM_RU.get(type(value).__name__, {}).get(value.value)
        or getattr(value, "label_ru", None)
        or value.value
    )


def ru_node(kind: str) -> str:
    return NODE_KIND_RU.get(kind, kind) if _RU_LABELS.get() else kind


def executive_md(case: DecisionCase) -> list[str]:
    """Верх отчёта для руководителя: ответ, доводы, шаги, что нужно от него, пересмотр — без служебных кодов."""
    s = case.executive
    assert s is not None
    unchecked = " *(числа не сверены)*"
    out = [f"**Ответ:** {link(s.answer)}" + (unchecked if "answer" in s.unverified else "")]
    if s.reasons:
        out += ["", "**Почему:**", ""]
        for i, r in enumerate(s.reasons, start=1):
            item = f"{i}. {link(r)}"
            b = s.basis[i - 1] if i - 1 < len(s.basis) else None
            if b is not None:  # summary_repair: на чём держится довод
                parts = [b.kind_label + (f": {b.source}" if b.source else "")]
                if b.caveat:
                    parts.append(f"оговорка: {b.caveat}")
                if b.replaced:
                    parts.append(
                        "довод собран из текста строки реестра: в предложенном числа не сошлись с опорой"
                    )
                item += f"  \n   *Опора:* {'; '.join(parts)}"
            out.append(item)
    if s.next_steps:
        out += ["", "**Что делаем дальше:**", "", "| Когда | Что | Кто |", "|---|---|---|"]
        out += [f"| {st.when} | {st.action} | {st.owner or '—'} |" for st in s.next_steps]
    if s.asks:
        out += ["", "**Что нужно от вас:**", ""] + [f"- {a}" for a in s.asks]
    if s.revisit:
        out.append(
            f"\n**Когда пересматриваем:** {link(s.revisit)}"
            + (unchecked if "revisit" in s.unverified else "")
        )
    if s.say_to_team:
        out.append(f"\n**Что сказать команде:** «{s.say_to_team.strip('«»')}»")
    selected = case.option(case.selected_option_id or "")
    decision = case.active_decision()
    facts = []
    if selected:
        facts.append(f"вариант «{selected.title}» ({selected.kind.label_ru})")
    if decision:
        facts.append(f"статус: {STATUS_RU.get(decision.status.value, decision.status.value)}")
        if decision.revisit_date:
            facts.append(f"пересмотр не позднее {decision.revisit_date}")
    if case.frame and case.frame.premise:
        premise = case.frame.premise.split(". ")[0].rstrip(".")
        premise = premise if len(premise) <= 140 else premise[:137].rstrip() + "…"
        facts.append(f"посылка запроса «{premise}» — {case.frame.premise_verdict.label_ru}")
    if facts:
        out.append("\n*" + " · ".join(facts) + "*")
    return out


def legacy_decision_md(case: DecisionCase) -> list[str]:
    """Без выжимки (старые кейсы, решение ещё не принято): рекомендация, обоснование, рамка задачи."""
    out: list[str] = []
    frame = case.frame
    selected = case.option(case.selected_option_id or "")
    decision = case.active_decision()
    new_leader = next((o for o in case.options if o.status == OptionStatus.REOPENED), None)
    if decision and decision.status == DecisionStatus.REOPENED and new_leader:
        out.append(
            f"**Решение открыто заново. Новая рекомендация агента: {new_leader.title}** "
            f"({new_leader.kind.label_ru}). {link(new_leader.summary)}"
        )
        out.append(
            "\nПо новым данным этот вариант стал лучшим в базовом сценарии стресс-теста. Подтвердить смену "
            "решения — за руководителем (шлюз «выбор варианта»)."
        )
        if selected:
            out.append(f"\nПрежняя рекомендация: {selected.title}.")
    elif selected:
        out.append(f"**Рекомендация: {selected.title}** ({selected.kind.label_ru}). {link(selected.summary)}")
    if decision:
        out.append(f"\n{link(decision.rationale)}")
        out.append(
            f"\nСтатус решения: **{ru(decision.status)}** · пересмотр не позднее {decision.revisit_date}"
        )
    if case.stress:
        out.append(f"\n**Устойчивость:** {case.stress.summary}")
    if frame:
        out.append(
            f"\n**Вопрос:** {frame.question}  \n**Получатель:** {frame.recipient}  \n**Цель:** {frame.goal}"
        )
        if frame.premise:
            out.append(
                f"\n**Посылка запроса** «{frame.premise}» — {frame.premise_verdict.label_ru}. {link(frame.premise_reasoning)}"
            )
    return out


def panel_md(case: DecisionCase) -> list[str]:
    """Панель модельных респондентов: доверие, разногласия и вопросы для интервью — без средних оценок."""
    if not case.panels:
        return []
    run = case.panels[-1]
    names = {p.id: p.name for p in run.personas}
    out = [f"## Модельные респонденты — {run.label}", ""]
    trust = "низкое" if run.trust == "low" else "обычное"
    reasons = []
    if run.placebo_liked:
        reasons.append("плацебо-концепт понравился персонам")
    if run.framing_sensitive:
        reasons.append(
            "ответы не меняются при смене формулировки на противоположную: "
            + ", ".join(run.concepts.get(c, c) for c in run.framing_sensitive)
        )
    out.append(
        f"Панель {run.id}: {len(run.personas)} персон, концептов {len(run.concepts) - 1} и плацебо. "
        f"Доверие к панели — **{trust}**"
        + (f" ({'; '.join(reasons)})" if reasons else "")
        + "."
        + (f" Оговорка: {run.spread_warning.lower()}." if run.spread_warning else "")
    )
    out.append(
        "\nСредние оценки не показываются: модельные респонденты смещают средние и занижают разброс. "
        "Ниже — разногласия профилей как гипотезы и вопросы, которые нужно задать реальным людям."
    )
    if run.disagreements:
        out += ["", "| Концепт | Разброс | Кто «за» и почему | Кто «против» и почему |", "|---|---|---|---|"]
        for d in run.disagreements:
            pro = "; ".join(
                f"{names.get(p, p)}: {r[:120]}" for p, r in zip(d.supporters, d.reasons_for, strict=False)
            )
            con = "; ".join(
                f"{names.get(p, p)}: {r[:120]}" for p, r in zip(d.opponents, d.reasons_against, strict=False)
            )
            out.append(
                f"| {run.concepts.get(d.concept_id, d.concept_id)} | {d.spread:.1f} | {pro or '—'} | {con or '—'} |"
            )
    else:
        out.append(
            "\nСильных разногласий нет — это скорее признак слишком согласованной панели, чем консенсуса."
        )
    if run.interview_questions:
        out += ["", "**Вопросы для реальных интервью** (о прошлом поведении; проверены линтером гайда):", ""]
        for q in run.interview_questions:
            concept = f" — {run.concepts.get(q.concept_id, q.concept_id)}" if q.concept_id else ""
            line = f"- {q.question}{concept}" + (f" *(проверяет: {q.checks})*" if q.checks else "")
            if q.issues:
                line += f" ⚠ {', '.join(q.issues)} → «{q.suggestion}»"
            out.append(line)
    out.append("")
    return out


_REF_GROUP = re.compile(
    r"\[((?:CL|EV|SRC|OPT|UNK|EXP|FEAT|PRM|OBJ|DEC|FC|TRG)-\d+"
    r"(?:\s*[,;]\s*(?:CL|EV|SRC|OPT|UNK|EXP|FEAT|PRM|OBJ|DEC|FC|TRG)-\d+)+)\]"
)


def link(text: str) -> str:
    """[CL-001] → [CL-001](#CL-001): в HTML ссылки кликабельны и ведут в приложение-реестр.
    Группа «[CL-077, CL-076]» разбивается на отдельные ссылки."""
    text = _REF_GROUP.sub(
        lambda m: ", ".join(f"[{i.strip()}]" for i in re.split(r"[,;]", m.group(1))), text or ""
    )
    return REF_RE.sub(lambda m: f"[{m.group(1)}](#{m.group(1)})", text)


def narrative_texts(case: DecisionCase) -> list[str]:
    texts = []
    if case.product:
        texts += [case.product.direction, case.product.value_logic]
    if case.comms:
        texts += [case.comms.situation, case.comms.barrier_explanation, case.comms.key_message]
    texts += [o.rationale for o in case.options]
    texts += [o.text for o in case.objections]
    texts += [d.rationale for d in case.decisions]
    if case.frame:
        texts.append(case.frame.premise_reasoning)
    if case.executive:
        texts += [case.executive.answer, *case.executive.reasons, case.executive.revisit]
    return [t for t in texts if t]


def _support_ids(case: DecisionCase, text: str) -> list[str]:
    """Опора, указанная у элемента отдельно от текста (список claim_ids у возражения, варианта, решения)."""
    ids: list[str] = []
    for o in case.objections:
        if o.text == text:
            ids += o.claim_ids
    for o in case.options:
        if o.rationale == text:
            ids += o.supporting_claim_ids + o.assumption_claim_ids
    for d in case.decisions:
        if d.rationale == text:
            ids += d.load_bearing_claim_ids + d.vulnerable_claim_ids
    return ids


def narrative_number_mismatches(case: DecisionCase) -> list[str]:
    claim_texts = {c.id: c.text for c in case.claims}
    # числа из запроса руководителя — заявление пользователя, не вывод модели
    allowed = numbers_in(case.brief)
    problems: list[str] = []
    for text in narrative_texts(case):
        support = [n for cid in _support_ids(case, text) for n in numbers_in(claim_texts.get(cid, ""))]
        problems += number_mismatches(text, claim_texts, allowed=allowed + support)
    return problems


def narrative_number_findings(case: DecisionCase) -> list[NumberFinding]:
    """Сверка v2: все числа повествования с вердиктом. Опоры — только активные строки реестра (v1 сверял и с
    отозванными), опора элемента (claim_ids у варианта, возражения, решения) и числа запроса."""
    claims = {c.id: c.text for c in case.active_claims()}
    # Пре-мортем и гипотетические возражения — воображаемый сценарий: числа в них — прогноз, а не данные
    scenarios = {o.text for o in case.objections if o.kind == ObjectionKind.PREMORTEM or o.is_hypothesis}
    proof = case.comms.proof_claim_ids if case.comms else []
    findings: list[NumberFinding] = []
    for text in narrative_texts(case):
        if text in scenarios:
            continue
        support = _support_ids(case, text)
        if (
            case.comms and text == case.comms.key_message
        ):  # текст для клиента без ссылок: опора — подтверждения
            support = [*support, *proof]
        findings += check_text(text, claims, brief=case.brief, support_ids=support)
    return findings


def number_problems(ctx: AppContext, case: DecisionCase) -> tuple[list[str], list[NumberFinding]]:
    """Что показать в «Проверьте числа»: строки v1 или находки v2 (numbers_v2) — и все находки v2 для самоаудита."""
    if not enabled(ctx.settings, "numbers_v2"):
        return narrative_number_mismatches(case), []
    findings = narrative_number_findings(case)
    return [f"{f.number} — {f.sentence[:200]}" for f in findings if f.flagged], findings


def numbers_block(findings: list[NumberFinding]) -> list[str]:
    """«Проверьте числа» по видам: нет опоры, другая единица, число есть, но в другой строке или без ссылки."""
    flagged = [f for f in findings if f.flagged]
    if not flagged:
        return []
    out = ["**Проверьте числа:**", ""]
    for verdict, label in VERDICT_RU.items():
        group = [f for f in flagged if f.verdict == verdict]
        if not group:
            continue
        out.append(f"*{label[:1].upper() + label[1:]}* ({len(group)}):")
        for f in group[:6]:
            where = f" — есть в {', '.join(f.other_ids)}" if f.other_ids else ""
            out.append(f"- {f.number}{where}: {f.sentence[:180]}")
    out.append("")
    return out


def narrative_uncited(case: DecisionCase) -> list[str]:
    texts = []
    if case.product:
        texts += [case.product.direction, case.product.value_logic]
    if case.comms:
        texts += [case.comms.situation, case.comms.barrier_explanation]
    texts += [o.rationale for o in case.options]
    sentences: list[str] = []
    for t in texts:
        sentences += uncited_sentences(t, min_length=60)
    return sentences


def _claim_row(case: DecisionCase, cid: str) -> str:
    c = case.claim(cid)
    if c is None:
        return f"- {cid} — нет в реестре"
    return f'- <a id="{c.id}"></a>**[{c.id}]** {c.text} — *{c.type.label_ru}*, опора: {ru(c.support)}, надёжность: {c.reliability.label_ru}'


def _sources_for(case: DecisionCase, claim_ids: list[str]) -> str:
    out = []
    for cid in claim_ids:
        for s in claim_sources(case, cid):
            mark = " (синтетика)" if s.synthetic else ""
            if s.is_calculation:
                cells = f", ячейки: {', '.join(s.cells[:3])}" if s.cells else ""
                # период и n — только в отчёте (контракт расчёта): в текст строки и во входы моделей не попадают
                scope = (f", период {s.period}" if s.period else "") + (
                    f", n = {s.rows} строк" if s.rows else ""
                )
                out.append(f"расчёт по [{s.source_id}](#{s.source_id}) {s.title}{mark}{scope}{cells}")
            else:
                out.append(
                    f"«{s.quote[:160]}» — [{s.source_id}](#{s.source_id}) {s.title}{mark}, {s.locator}"
                )
    return "; ".join(out)


def _cost_block(cost: CostSummary | None) -> list[str]:
    if cost is None:
        return ["Чек стоимости недоступен."]
    lines = [
        f"- Вызовов моделей: {cost.calls}; токенов: вход {cost.input_tokens:,}, выход {cost.output_tokens:,}".replace(
            ",", " "
        ),
        f"- Стоимость по прайсу моделей: ${cost.usd:.4f} ≈ {cost.rub:.2f} ₽"
        + (" (справочная оценка)" if cost.usd_estimated else ""),
        f"- Время ответа моделей: {cost.model_latency_seconds:.0f} с",
        f"- Время человека: {cost.human_minutes:.0f} мин ≈ {cost.human_rub:.0f} ₽",
        f"- Итого (модели + человек): {cost.total_rub:.2f} ₽",
    ]
    if cost.subscription_share_usd:
        lines.append(
            f"- Альтернатива для подписки Ollama: доля подписки на прогон ≈ ${cost.subscription_share_usd:.2f}"
        )
    if cost.by_step:
        lines.append("\n| Шаг | Вызовы | Токены (вход/выход) | $ |\n|---|---|---|---|")
        for step, v in sorted(cost.by_step.items(), key=lambda kv: (-kv[1].get("usd", 0), kv[0]))[:20]:
            lines.append(
                f"| {step} | {int(v.get('calls', 0))} | {int(v.get('input_tokens', 0))}/{int(v.get('output_tokens', 0))} "
                f"| {v.get('usd', 0):.4f} |"
            )
    return lines


def what_changed_md(case: DecisionCase) -> list[str]:
    if not case.revisions:
        return []
    rev = case.revisions[-1]
    lines = ["## Что изменилось", "", f"**{rev.summary}**", ""]
    if rev.invalidated_claim_ids:
        lines.append("**Основания под вопросом:**")
        lines += [_claim_row(case, cid) for cid in rev.invalidated_claim_ids]
    changed = [r for r in rev.results if r.verdict != NodeVerdict.UNCHANGED]
    if changed:
        lines += ["", "| Узел | Вердикт | Было | Стало | Почему |", "|---|---|---|---|---|"]
        for r in changed:
            lines.append(
                f"| {r.node_id} ({ru_node(r.node_kind)}) | {ru(r.verdict)} | {r.before[:140]} | {r.after[:140] or '—'} | {r.reason[:140]} |"
            )
    unchanged = [r.node_id for r in rev.results if r.verdict == NodeVerdict.UNCHANGED]
    if unchanged:
        lines.append(f"\nПроверено и не изменилось: {', '.join(unchanged)}")
    lines.append(f"\nНе затронуто новыми данными: {len(rev.untouched_ids)} узлов (не пересматривались).")
    reopened_options = [o for o in case.options if o.status == OptionStatus.REOPENED]
    if rev.reopened_decision_id:
        lines.append(f"\nРешение **{rev.reopened_decision_id}** открыто заново.")
    for o in reopened_options:
        lines.append(f"Открыт заново вариант **[{o.id}] {o.title}**: {o.revisit_when or ''}")
    if rev.tokens_full_regeneration_estimate:
        share = rev.tokens_used / rev.tokens_full_regeneration_estimate
        lines.append(
            f"\nТокенов на пересмотр: {rev.tokens_used:,} против ~{rev.tokens_full_regeneration_estimate:,} "
            f"на полную перегенерацию ({share:.0%}).".replace(",", " ")
        )
    lines.append("")
    return lines


def render_markdown(ctx: AppContext, case: DecisionCase) -> str:
    token = _RU_LABELS.set(enabled(ctx.settings, "ru_labels"))
    try:
        return _render_markdown(ctx, case)
    finally:
        _RU_LABELS.reset(token)


def _render_markdown(ctx: AppContext, case: DecisionCase) -> str:
    mismatches, findings = number_problems(ctx, case)
    audit = compute_self_audit(
        case, uncited_sentences=len(narrative_uncited(case)), number_mismatches=len(mismatches)
    )
    v2 = enabled(ctx.settings, "numbers_v2")
    if v2:
        audit = audit.model_copy(
            update={
                "numbers_checked": len(findings),
                "numbers_unsupported": sum(f.verdict == Verdict.UNSUPPORTED for f in findings),
                "numbers_unit_mismatch": sum(f.verdict == Verdict.UNIT_MISMATCH for f in findings),
                "numbers_miscited": sum(f.verdict in (Verdict.ELSEWHERE, Verdict.NO_REF) for f in findings),
            }
        )
    cost = ctx.costs.summary(case.id)
    decision = case.active_decision()
    out: list[str] = [f"# {case.title}", ""]
    out.append(f"*Версия кейса {case.version} · этап: {case.stage.label_ru}*\n")

    out += ["## Решение на одной странице", ""]
    if case.executive:
        out += executive_md(case)
    else:
        out += legacy_decision_md(case)
    agenda = what_to_decide_now(case, ctx.clock.today())
    if agenda:
        out.append("\n**Что решать сейчас:**")
        shown = agenda[: 3 if case.executive else 6]
        clean = strip_service_codes if case.executive else str
        out += [f"- {link(clean(a.title))}" + (f" (до {a.due})" if a.due else "") for a in shown]
    out.append("")
    out += what_changed_md(case)

    out += ["## Панель самоаудита", "", audit.headline(), ""]
    out += [
        "| Показатель | Значение |",
        "|---|---|",
        f"| Утверждений в реестре | {audit.claims_total} |",
        f"| Доля фактов и метрик без опоры | {audit.unsupported_share:.0%} |",
        f"| Непроверенных фактов и метрик | {audit.unverified_share:.0%} |",
        f"| Точность цитат (проверено {audit.verified_count}) | "
        + (f"{audit.citation_accuracy:.0%}" if audit.citation_accuracy is not None else "—")
        + " |",
        f"| Неиспользованные источники | {audit.unused_sources_share:.0%} ({', '.join(audit.unused_source_ids) or '—'}) |",
        f"| Варианты без возражений | {', '.join(audit.options_without_objections) or 'нет'} |",
        f"| Излишняя уверенность | {', '.join(audit.overconfident_claim_ids) or 'нет'} |",
        f"| Ошибки линтера (строки скрыты из отчёта) | {audit.lint_errors} |",
        f"| Синтетика в реестре | {audit.synthetic_claims} (не доказывает спрос) |",
        f"| Предложения без ссылок на реестр | {audit.uncited_sentences} |",
        *(
            [
                f"| Числа в тексте: проверено / без опоры / другая единица / не та строка | "
                f"{audit.numbers_checked} / {audit.numbers_unsupported} / {audit.numbers_unit_mismatch} / "
                f"{audit.numbers_miscited} |"
            ]
            if v2
            else [f"| Числа, не совпадающие с опорой | {audit.number_mismatches} |"]
        ),
        f"| Обнаруженные инъекции в материалах | {audit.injections_detected} |",
        "",
    ]

    if v2:
        out += numbers_block(findings)
    elif mismatches:
        out += [
            "**Проверьте числа:** в этих предложениях число не совпадает с цитируемой строкой реестра —",
            "",
        ]
        out += [f"- {m}" for m in mismatches[:12]]
        out.append("")
    out += ["## Картина ситуации", "", "### Что известно и откуда", ""]
    facts = [
        c
        for c in case.active_claims()
        if c.type in (ClaimType.FACT, ClaimType.DERIVED_METRIC) and reportable(c, case)
    ]
    for c in facts:
        out.append(f"- **[{c.id}]** {c.text} — *{c.type.label_ru}*; {_sources_for(case, [c.id])}")
    out += ["", "### Гипотезы, допущения и прогнозы", ""]
    for c in case.active_claims():
        if c.type in (ClaimType.HYPOTHESIS, ClaimType.ASSUMPTION, ClaimType.FORECAST) and reportable(c, case):
            extra = (
                f"; p≈{c.probability:.0%} ({c.likelihood.label_ru})"
                if c.probability is not None and c.likelihood
                else ""
            )
            check = c.falsification_test or c.what_would_change or c.resolution_criterion or ""
            out.append(
                f"- **[{c.id}]** {c.text} — *{c.type.label_ru}*{extra}"
                + (f"; проверка: {check}" if check else "")
            )
    synthetic = [c for c in case.active_claims() if c.type == ClaimType.SYNTHETIC]
    if synthetic:
        out += ["", "### Синтетика — гипотезы, не доказательство", ""]
        out += [f"- **[{c.id}]** СИНТЕТИКА: {c.text}" for c in synthetic]
    out += ["", "### Чего мы не знаем", ""]
    for u in case.unknowns:
        mark = " **[меняет решение]**" if u.flips_decision else ""
        out.append(
            f"- [{u.id}] {u.question}{mark} — {ru(u.status)}; как узнать: {u.resolution_method or '—'}; причина: {u.reason}"
        )
    out.append("")

    panel = aggregate_panel(case.role_scores)
    ranks = {r.option_id: r for r in panel.rankings}
    labels = enabled(ctx.settings, "model_labels")  # внутренние результаты агента — не данные материалов
    out += [
        "## Варианты",
        "",
        f"| Вариант | Тип | Статус | {'Панель ролей (оценка модели)' if labels else 'Панель ролей'} | Kill-критерий |",
        "|---|---|---|---|---|",
    ]
    for o in case.options:
        if o.status == OptionStatus.CLONE:
            continue
        r = ranks.get(o.id)
        kc = f"{o.kill_criterion.signal} (до {o.kill_criterion.deadline})" if o.kill_criterion else "—"
        out.append(
            f"| [{o.id}] {o.title} | {o.kind.label_ru} | {ru(o.status)} | {f'{r.mean:.1f}±{r.spread:.1f}' if r else '—'} | {kc} |"
        )
    out.append(f"\n{panel.note}\n")
    for o in case.options:
        if o.status == OptionStatus.CLONE:
            continue
        out += [f"### [{o.id}] {o.title}", "", link(o.summary), "", f"*Обоснование:* {link(o.rationale)}"]
        if o.conscious_refusals:
            out.append("*Осознанные отказы:* " + "; ".join(o.conscious_refusals))
        if o.revisit_when and o.status != OptionStatus.SELECTED:
            out.append(f"*Пересмотреть, если:* {o.revisit_when}")
        objs = sorted(case.objections_for(o.id), key=lambda x: -(x.strength or 0))
        if objs:
            out.append("\n*Сильные возражения:*")
            for obj in objs[:5]:
                basis = ", ".join(f"[{c}](#{c})" for c in obj.claim_ids) if obj.claim_ids else "гипотеза"
                out.append(f"- ({obj.strength or '?'}/5, {ru(obj.kind)}) {obj.text} — опора: {basis}")
        out.append("")
    if case.stress:
        out += ["## Стресс-тест решения", ""]
        if labels:
            out += [MODEL_STRESS, ""]
        out += [case.stress.summary, ""]
        out += [f"- {'Модель: ' if labels else ''}{b.statement}" for b in case.stress.boundaries]
        out.append("")

    if case.product:
        p = case.product
        out += ["## Продуктовая стратегия", "", f"**Аудитория и задача:** {p.audience} — {p.job_to_be_done}"]
        if p.segments:
            out.append("**Сегменты:** " + "; ".join(f"{s.id} {s.name}" for s in p.segments))
        out += [
            f"\n**Альтернативы у пользователя:** {'; '.join(p.alternatives)}",
            f"\n**Направление:** {link(p.direction)}",
            f"\n**Осознанные отказы:** {'; '.join(p.conscious_refusals)}",
            f"\n**Связь пользы с затратами и доходами:** {link(p.value_logic)}",
        ]
        if p.costs or p.revenue:
            out.append(f"\nЗатраты: {p.costs or '—'}. Доходы: {p.revenue or '—'}.")
        if p.features:
            out += ["", "| Функция | Статус | Опора |", "|---|---|---|"]
            out += [
                f"| {f.id} {f.name} — {f.description} | {ru(f.status)} | {', '.join(f.claim_ids) or '—'} |"
                for f in p.features
            ]
        if p.metrics:
            out.append("\n**Метрики:** " + "; ".join(p.metrics))
        if p.action_plan:
            out += [
                "",
                "### План действий",
                "",
                "| Когда | Что | Кто | Как поймём, что получилось |",
                "|---|---|---|---|",
            ]
            out += [
                f"| {s.when} | {s.action} | {s.owner or '—'} | {s.metric or '—'} |" for s in p.action_plan
            ]
        out.append("")
    if case.comms:
        c = case.comms
        out += [
            "## Коммуникационная стратегия",
            "",
            f"**Аудитория и ситуация:** {c.audience}. {link(c.situation)}",
            f"\n**Барьер:** {c.barrier.label_ru} — {link(c.barrier_explanation)}",
            f"\n**Ключевое сообщение:** «{c.key_message}»",
            "\n**Подтверждение:** " + (", ".join(f"[{x}](#{x})" for x in c.proof_claim_ids) or "—"),
            "",
            "| Канал | Роль | Сообщение |",
            "|---|---|---|",
        ]
        out += [f"| {ch.channel} | {ch.role.label_ru} | {ch.message} |" for ch in c.channels]
        if c.segment_messages:
            names = {s.id: s.name for s in (case.product.segments if case.product else [])}
            out += [
                "",
                "### Что говорим каждому сегменту",
                "",
                "| Сегмент | Сообщение | Канал | Когда |",
                "|---|---|---|---|",
            ]
            out += [
                f"| {names.get(m.segment_id or '', m.segment_id or '—')} | {m.message} | {m.channel or '—'} | {m.timing or '—'} |"
                for m in c.segment_messages
            ]
        if c.timeline:
            out += [
                "",
                "### Календарь коммуникации",
                "",
                "| Когда | Что | Кто | Метрика |",
                "|---|---|---|---|",
            ]
            out += [f"| {s.when} | {s.action} | {s.owner or '—'} | {s.metric or '—'} |" for s in c.timeline]
        out += ["", "**Сверка «обещание = продукт»:**", ""]
        for pr in c.promises:
            mark = "✅" if pr.status == PromiseStatus.SUPPORTED else "⛔"
            basis = ", ".join([*pr.feature_ids, *pr.claim_ids]) or "—"
            out.append(
                f"- {mark} {pr.id} «{pr.text}» — {ru(pr.status)}; опора: {basis}"
                + (f"; {pr.issue}" if pr.issue else "")
            )
        if c.revision_note:
            out.append(f"\n*{c.revision_note}*")
        if c.press_release:
            out += ["", "### Пресс-релиз (Working Backwards)", "", c.press_release]
        out.append("")
    if case.experiments:
        out += [
            "## План проверки",
            "",
            "| Гипотеза | Метрика и baseline | MDE и срок | Guardrails | Kill-критерий | Правило решения |",
            "|---|---|---|---|---|---|",
        ]
        for e in case.experiments:
            base = (
                f"{e.baseline}" if e.baseline is not None else f"нет — {e.baseline_step or 'нужно получить'}"
            )
            guard = "; ".join(f"{g.metric} {g.threshold}" for g in e.guardrails) or "—"
            kc = f"{e.kill_criterion.signal} до {e.kill_criterion.deadline}" if e.kill_criterion else "—"
            out.append(
                f"| [{e.id}] {e.hypothesis} | {e.metric}; baseline: {base} | {e.mde}; {e.observation_period_days} дн. | {guard} | {kc} | {e.decision_rule} |"
            )
        out.append("")
    if case.forecasts:
        out += ["## Прогнозы (для проверки по Brier)", ""]
        if labels:
            out += [MODEL_FORECASTS, ""]
        for f in case.forecasts:
            out.append(
                f"- [{f.id}] {f.statement}: p≈{f.probability:.0%} (выборки {', '.join(f'{s:.2f}' for s in f.samples)}); разрешение {f.resolution_date}"
            )
        out.append("")
    if decision:
        out += [
            "## Журнал решения",
            "",
            f"**{decision.id}** · вариант [{decision.selected_option_id}] · {ru(decision.status)}",
            "",
            f"**Обоснование:** {link(decision.rationale)}",
        ]
        out.append(
            f"\nDACI: driver — {decision.daci.driver}; approver — {decision.daci.approver}; "
            f"contributors — {', '.join(decision.daci.contributors)}; informed — {', '.join(decision.daci.informed) or '—'}"
        )
        out.append(f"\n**Как проверим исполнение:** {decision.confirmation}")
        if decision.shaping_actions:
            out.append("\n**Формирующие действия:** " + "; ".join(decision.shaping_actions))
        if decision.hedging_actions:
            out.append("\n**Страхующие действия:** " + "; ".join(decision.hedging_actions))
        out += ["", "| Сигнал пересмотра | Тип | Срок | Сработал |", "|---|---|---|---|"]
        out += [
            f"| {t.description} | {ru(t.kind)} | {t.due or '—'} | {'да — ' + t.fired_reason if t.fired else 'нет'} |"
            for t in decision.triggers
        ]
        if decision.commitments:
            out += ["", "**Обязательства:**"]
            out += [
                f"- {cm.id} {cm.description} — {cm.owner or 'ответственный не назначен'}, до {cm.due}"
                for cm in decision.commitments
            ]
        stale = stale_grounds(case, ctx.clock.today())
        if stale:
            out += ["", "**Устаревшие основания:**"]
            out += [f"- [{s.claim_id}] {s.text[:120]} — {'; '.join(s.reasons)}" for s in stale[:8]]
        out.append("")
    out += panel_md(case)
    out += ["## Источники", "", "| ID | Источник | Тип | Надёжность | Адрес |", "|---|---|---|---|---|"]
    for s in case.sources:
        flags = (" · СИНТЕТИКА" if s.synthetic else "") + (" · отозван" if s.retracted else "")
        out.append(
            f'| <a id="{s.id}"></a>{s.id} | {s.title}{flags} | {ru(s.kind)} | {s.reliability.label_ru} | {s.url or s.path or "—"} |'
        )
    out += ["", "## Чек стоимости", "", *_cost_block(cost), ""]
    out += ["## Приложение: реестр утверждений", ""]
    for c in case.claims:
        status = "" if c.is_active else f" ~~{ru(c.status)}~~"
        src = _sources_for(case, [c.id])
        out.append(_claim_row(case, c.id) + status + (f"  \n  ↳ {src}" if src else ""))
    return "\n".join(out) + "\n"


def build_report(ctx: AppContext, case: DecisionCase) -> dict[str, str]:
    markdown_text = render_markdown(ctx, case)
    paths: dict[str, str] = {}
    if ctx.artifacts is not None:
        paths["markdown"] = ctx.artifacts.write(case.id, "out/report.md", markdown_text)
        if ctx.renderer is not None:
            paths["html"] = ctx.artifacts.write(
                case.id, "out/report.html", ctx.renderer.to_html(markdown_text, case.title)
            )
    ctx.emit("report.built", case.id, **paths)
    paths["text"] = markdown_text
    return paths
