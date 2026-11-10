"""Компактные текстовые представления кейса для промптов (что видит модель на каждом шаге)."""

from __future__ import annotations

from datetime import date

from strategist.domain.entities import ClaimType, DecisionCase, Option


def _cut(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def materials_overview(case: DecisionCase) -> str:
    lines = []
    for s in case.sources:
        flags = []
        if s.synthetic:
            flags.append("СИНТЕТИКА")
        if s.url:
            flags.append(s.url)
        lines.append(f"- {s.id}: {s.title} ({s.kind.value}{'; ' + '; '.join(flags) if flags else ''})")
    return "\n".join(lines) or "— материалов нет —"


def frame_digest(case: DecisionCase) -> str:
    f = case.frame
    if f is None:
        return f"Запрос руководителя: {case.brief}"
    parts = [
        f"Запрос руководителя: {case.brief}",
        f"Вопрос решения: {f.question}",
        f"Нейтральная формулировка: {f.neutral_question}",
        f"Цель: {f.goal}",
        f"Получатель: {f.recipient}",
    ]
    if f.decision_owner:
        parts.append(f"Владелец решения: {f.decision_owner}")
    if f.deadline:
        parts.append(f"Срок решения: {f.deadline.isoformat()}")
    if f.constraints:
        parts.append("Ограничения: " + "; ".join(f.constraints))
    if f.premise:
        parts.append(f"Посылка запроса: {f.premise} — вердикт: {f.premise_verdict.label_ru}")
    if f.competing_frames:
        parts.append("Конкурирующие рамки: " + "; ".join(f.competing_frames))
    answered = [q for q in f.clarifying_questions if q.answer]
    for q in answered:
        parts.append(f"Уточнение: {q.question} → {q.answer}")
    return "\n".join(parts)


def claims_digest(
    case: DecisionCase,
    ids: list[str] | None = None,
    *,
    limit: int = 160,
    include_types: tuple[ClaimType, ...] | None = None,
    priority: list[str] | None = None,
) -> str:
    """priority — строки, которые идут первыми и не отрезаются лимитом (например, новые данные пересмотра)."""
    wanted = set(ids) if ids else None
    lines = []
    first = set(priority or [])
    ordered = sorted(case.active_claims(), key=lambda c: c.id not in first)
    for c in ordered:
        if wanted is not None and c.id not in wanted:
            continue
        if include_types and c.type not in include_types:
            continue
        if c.computation is not None and c.computation.blocked:  # контракт расчёта: только реестр, не опора
            continue
        tags = [c.type.label_ru, f"опора: {c.support.value}", f"надёжность: {c.reliability.label_ru}"]
        if c.probability is not None:
            tags.append(f"p={c.probability:.2f}")
        if c.status.value == "suspect":
            tags.append("ПОД ПОДОЗРЕНИЕМ")
        lines.append(f"[{c.id}] {_cut(c.text, 260)} ({'; '.join(tags)})")
        if len(lines) >= limit:
            lines.append("… (список сокращён)")
            break
    return "\n".join(lines) or "— утверждений нет —"


def unknowns_digest(case: DecisionCase) -> str:
    lines = []
    for u in case.unknowns:
        mark = " [МЕНЯЕТ РЕШЕНИЕ]" if u.flips_decision else ""
        lines.append(
            f"[{u.id}] {u.question}{mark} — {u.status.value}; почему важно: {_cut(u.why_it_matters, 160)}"
        )
    return "\n".join(lines) or "— пробелов не зафиксировано —"


def option_line(o: Option) -> str:
    kill = f"; kill: {o.kill_criterion.signal}" if o.kill_criterion else ""
    return f"[{o.id}] ({o.kind.label_ru}) {o.title}: {_cut(o.summary, 300)}{kill}"


def options_digest(case: DecisionCase, *, active_only: bool = True) -> str:
    rows = [option_line(o) for o in case.options if not active_only or o.status.value not in ("clone",)]
    return "\n".join(rows) or "— вариантов нет —"


def evidence_digest(case: DecisionCase, ids: list[str] | None = None, limit: int = 200) -> str:
    wanted = set(ids) if ids else None
    lines = []
    for ev in case.evidence:
        if wanted is not None and ev.id not in wanted:
            continue
        src = case.source(ev.source_id)
        title = src.title if src else ev.source_id
        synthetic = " [СИНТЕТИКА]" if src and src.synthetic else ""
        lines.append(f"[{ev.id}] «{_cut(ev.quote, 400)}» — {title}{synthetic}, {ev.locator.render()}")
        if len(lines) >= limit:
            break
    return "\n".join(lines) or "— цитат нет —"


def today_line(today: date) -> str:
    return f"Сегодня: {today.isoformat()}"
