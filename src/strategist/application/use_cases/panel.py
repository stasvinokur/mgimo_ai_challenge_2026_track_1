"""Панель модельных респондентов на карантине (СИНТЕТИКА): гипотезы и вопросы для реальных интервью.

Порядок: персоны (не меньше трети скептиков) → реакции свободным текстом на обезличенные концепты вместе с
плацебо → оценка по тексту (SSR через эмбеддинги) → утверждения в позитивной и негативной формулировке →
вердикт доверия → разногласия профилей → вопросы для реальных интервью через линтер гайда.

Ответы персон не становятся строками реестра: панель хранится в case.panels с меткой СИНТЕТИКА.
"""

from __future__ import annotations

import hashlib
from statistics import mean
from string import ascii_uppercase

from strategist.application.context import AppContext
from strategist.application.digests import claims_digest, frame_digest
from strategist.application.dto.llm_outputs import PanelInterviewsDTO, PanelPersonasDTO, PanelResponseDTO
from strategist.application.errors import StrategistError, SyntheticQuarantineViolation
from strategist.application.llm_calls import ask_structured
from strategist.application.use_cases.options import active_options
from strategist.domain.entities import (
    PLACEBO_CONCEPT,
    PLACEBO_ID,
    DecisionCase,
    FramingAnswer,
    InterviewQuestion,
    OptionKind,
    PanelRun,
    Persona,
    PersonaAnswer,
)
from strategist.domain.services.synthetic_panel import (
    SSR_ANCHORS,
    cosine,
    find_disagreements,
    lint_question,
    panel_verdict,
    quarantine_violations,
    ssr_score,
)

POSITIVE = "«{label}» решит мою задачу лучше, чем то, чем я пользуюсь сейчас."
NEGATIVE = "«{label}» не решит мою задачу лучше, чем то, чем я пользуюсь сейчас."


def _concepts(case: DecisionCase) -> dict[str, str]:
    """Концепты панели — варианты с продуктовым содержанием; «оставить» и «закрыть» персонам не показываются."""
    concepts = {
        o.id: f"{o.title}. {o.summary}"
        for o in active_options(case)
        if o.kind not in (OptionKind.STATUS_QUO, OptionKind.KILL)
    }
    if not concepts:
        raise StrategistError("Нет вариантов-концептов для панели: сначала нужен шаг вариантов")
    concepts[PLACEBO_ID] = PLACEBO_CONCEPT
    return concepts


def _labels(case: DecisionCase, concept_ids: list[str]) -> dict[str, str]:
    """Обезличенные метки в воспроизводимом «случайном» порядке: плацебо не стоит последним по построению."""
    order = sorted(concept_ids, key=lambda cid: hashlib.sha256(f"{case.id}:{cid}".encode()).hexdigest())
    return {cid: f"Концепт {ascii_uppercase[i]}" for i, cid in enumerate(order)}


def _segments(case: DecisionCase) -> str:
    if case.product and case.product.segments:
        return "\n".join(f"- {s.name}: {s.description}" for s in case.product.segments)
    if case.frame:
        return f"- аудитория из постановки: {case.frame.question}"
    return "—"


def _ssr(ctx: AppContext, reactions: list[str]) -> list[float | None]:
    try:
        vectors = ctx.embeddings.embed([*SSR_ANCHORS, *reactions])
    except Exception:  # нет эмбеддингов — оценка модели, это видно в score_method
        return [None] * len(reactions)
    anchors, texts = vectors[: len(SSR_ANCHORS)], vectors[len(SSR_ANCHORS) :]
    return [ssr_score([cosine(t, a) for a in anchors]) for t in texts]


def run_synthetic_panel(ctx: AppContext, case: DecisionCase, personas: int = 6) -> PanelRun:
    concepts = _concepts(case)
    labels = _labels(case, list(concepts))
    by_label = {label: cid for cid, label in labels.items()}
    ordered = sorted(labels, key=lambda cid: labels[cid])
    concepts_text = "\n".join(f"{labels[cid]}: {concepts[cid]}" for cid in ordered)
    statements = {}
    for cid in ordered:
        letter = labels[cid].split()[-1]
        statements[f"{letter}+"] = (cid, POSITIVE.format(label=labels[cid]))
        statements[f"{letter}-"] = (cid, NEGATIVE.format(label=labels[cid]))
    statements_text = "\n".join(f"{key}: {text}" for key, (_, text) in statements.items())

    cast, _ = ask_structured(
        ctx,
        task="panel_personas",
        prompt="panel_personas",
        values={
            "count": personas,
            "frame": frame_digest(case),
            "segments": _segments(case),
            "claims": claims_digest(case, limit=40),
        },
        schema=PanelPersonasDTO,
        case_id=case.id,
        step="panel:personas",
    )
    people = [
        Persona(
            id=f"P{i + 1}",
            name=p.name,
            profile=p.profile,
            segment=p.segment,
            tech_attitude=p.tech_attitude,
            current_alternative=p.current_alternative,
        )
        for i, p in enumerate(cast.personas[:personas])
    ]

    answers: list[PersonaAnswer] = []
    framing: list[FramingAnswer] = []
    for person in people:
        reply, _ = ask_structured(
            ctx,
            task="panel_respond",
            prompt="panel_respond",
            values={
                "persona": f"{person.name}. {person.profile} Сегмент: {person.segment or '—'}. "
                f"Отношение к технологиям: {person.tech_attitude}. Сейчас: {person.current_alternative or '—'}.",
                "concepts": concepts_text,
                "statements": statements_text,
            },
            schema=PanelResponseDTO,
            case_id=case.id,
            step=f"panel:respond:{person.id}",
            temperature=0.7,  # разные персоны должны звучать по-разному
        )
        reactions = [r for r in reply.reactions if r.concept_label.strip() in by_label]
        scores = _ssr(ctx, [r.reaction for r in reactions])
        for r, ssr in zip(reactions, scores, strict=True):
            model_score = max(1.0, min(5.0, float(r.score)))
            answers.append(
                PersonaAnswer(
                    persona_id=person.id,
                    concept_id=by_label[r.concept_label.strip()],
                    reaction=r.reaction,
                    concern=r.concern,
                    model_score=model_score,
                    score=round(ssr, 2) if ssr is not None else model_score,
                    score_method="ssr" if ssr is not None else "model",
                )
            )
        agreements = {f.statement_key.strip(): float(f.agreement) for f in reply.framing}
        for key, (cid, _) in statements.items():
            if not key.endswith("+"):
                continue
            pos, neg = agreements.get(key), agreements.get(key[:-1] + "-")
            if pos is not None and neg is not None:
                framing.append(
                    FramingAnswer(persona_id=person.id, concept_id=cid, positive=pos, negative=neg)
                )

    concept_scores: dict[str, list[float]] = {}
    for a in answers:
        concept_scores.setdefault(a.concept_id, []).append(a.score)
    pairs: dict[str, tuple[float, float]] = {}
    for cid in concepts:
        items = [f for f in framing if f.concept_id == cid]
        if items:
            pairs[cid] = (mean(f.positive for f in items), mean(f.negative for f in items))
    verdict = panel_verdict(concept_scores, PLACEBO_ID, pairs)
    disagreements = find_disagreements(answers, PLACEBO_ID)

    disagreements_text = (
        "\n".join(
            f"{labels[d.concept_id]}: «за» — {', '.join(d.reasons_for) or '—'}; «против» — "
            f"{', '.join(d.reasons_against) or '—'}"
            for d in disagreements
        )
        or "Сильных разногласий нет: проверьте, не слишком ли согласована панель."
    )
    asked, _ = ask_structured(
        ctx,
        task="panel_interviews",
        prompt="panel_interviews",
        values={
            "count": 5,
            "frame": frame_digest(case),
            "concepts": "\n".join(f"{labels[cid]}: {concepts[cid]}" for cid in ordered if cid != PLACEBO_ID),
            "disagreements": disagreements_text,
        },
        schema=PanelInterviewsDTO,
        case_id=case.id,
        step="panel:interviews",
    )
    questions = []
    for q in asked.questions:
        finding = lint_question(q.question)
        questions.append(
            InterviewQuestion(
                question=q.question,
                concept_id=by_label.get(q.concept_label.strip()),
                checks=q.checks,
                issues=[i.value for i in finding.issues],
                suggestion=finding.suggestion if finding.issues else "",
            )
        )

    run = PanelRun(
        id=case.new_id("panel"),
        at=ctx.clock.now(),
        personas=people,
        concepts={cid: (case.option(cid).title if case.option(cid) else "плацебо") for cid in concepts},
        answers=answers,
        framing=framing,
        trust=verdict.trust,
        placebo_liked=verdict.placebo_liked,
        framing_sensitive=verdict.framing_sensitive_questions,
        spread_warning=verdict.spread_warning,
        disagreements=disagreements,
        interview_questions=questions,
    )
    case.panels.append(run)
    problems = quarantine_violations(case)
    if problems:
        raise SyntheticQuarantineViolation(problems)
    ctx.emit(
        "panel.completed",
        case.id,
        panel=run.id,
        personas=len(people),
        trust=run.trust,
        disagreements=len(disagreements),
    )
    return run


def run_panel_for_case(ctx: AppContext, case_id: str, personas: int = 6) -> PanelRun:
    """Панель по последней версии кейса: новая версия с панелью и пересобранный отчёт."""
    from strategist.application.use_cases.report import build_report

    case = ctx.cases.load(case_id)
    run = run_synthetic_panel(ctx, case, personas)
    ctx.cases.save(case, f"panel: {run.id} — {len(run.personas)} персон, доверие {run.trust}")
    build_report(ctx, ctx.cases.load(case_id))
    return run
