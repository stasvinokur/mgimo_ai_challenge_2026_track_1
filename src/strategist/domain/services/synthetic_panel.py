"""Модельные респонденты на карантине и линтер гайда интервью (по мотивам The Mom Test).

Синтетика — генератор гипотез и контраргументов, а не измеритель спроса: двойники смещают средние, занижают
разброс и настроены протехнологично. Здесь — детерминированные правила, которые не зависят от модели:
плацебо-концепт, проверка смены рамки, сигнал разброса и линтер вопросов интервью.
"""

from __future__ import annotations

import re
from enum import StrEnum
from statistics import pstdev

from pydantic import Field

from strategist.domain.common import Entity
from strategist.domain.entities.case import DecisionCase
from strategist.domain.entities.claims import ClaimType
from strategist.domain.entities.panel import SYNTHETIC_LABEL, Disagreement, PersonaAnswer


class QuestionIssue(StrEnum):
    HYPOTHETICAL = "hypothetical"  # «купили бы вы…», «стали бы…»
    LEADING = "leading"  # «не правда ли…», «согласитесь…»
    DOUBLE = "double_barreled"  # два вопроса в одном
    GENERIC = "generic"  # «обычно», «как часто» вместо конкретного случая
    OPINION_ABOUT_IDEA = "idea_opinion"  # «нравится ли вам идея…»


_PATTERNS: list[tuple[QuestionIssue, re.Pattern[str]]] = [
    (
        QuestionIssue.HYPOTHETICAL,
        re.compile(r"\b(?:\w+ли\s+бы|\w+л[аи]?\s+бы|бы\s+вы|если\s+бы|would you|will you)\b", re.I),
    ),
    (
        QuestionIssue.LEADING,
        re.compile(
            r"(?:не\s+правда\s+ли|согласитесь|разве\s+не|ведь\s+|не\s+кажется\s+ли|isn't it|don't you)", re.I
        ),
    ),
    (
        QuestionIssue.GENERIC,
        re.compile(r"\b(?:обычно|как\s+правило|как\s+часто|в\s+целом|usually|how often)\b", re.I),
    ),
    (
        QuestionIssue.OPINION_ABOUT_IDEA,
        re.compile(r"(?:нравится\s+ли|понравил\w+\s+бы|хорошая\s+ли\s+идея|что\s+думаете\s+об?\s+иде)", re.I),
    ),
]


class QuestionFinding(Entity):
    question: str
    issues: list[QuestionIssue] = Field(default_factory=list)
    suggestion: str = ""


def _double_barreled(question: str) -> bool:
    if question.count("?") > 1:
        return True
    return bool(re.search(r"\?\s*\S|\b(?:и|или)\s+(?:почему|как|что|когда|сколько|зачем)\b", question, re.I))


_LEADING_PREFIX = re.compile(
    r"^(?:скажите,?\s*)?(?:согласитесь|не\s+правда\s+ли|разве\s+не|не\s+кажется\s+ли\s+вам,?\s*что|ведь)[\s,]*",
    re.I,
)
_HYPOTHETICAL_PREFIX = re.compile(
    r"^(?:скажите,?\s*)?(?:если\s+бы\s+[^,]*,\s*)?(?:\w+л[аи]?\s+бы\s+вы|бы\s+вы)\s+"
    r"|^(?:нравится\s+ли\s+вам|понравил\w*\s+бы\s+вам)\s+(?:идея\s+)?",
    re.I,
)
_QUESTION_WORD = re.compile(
    r"^(?:как\s+часто|как\s+правило,?|обычно|в\s+целом,?|как|почему|что|когда|сколько|зачем)\s+(?:вы\s+)?",
    re.I,
)
_SPLIT = re.compile(r"\?\s*|\s+(?:и|или)\s+(?=(?:почему|как|что|когда|сколько|зачем)\b)", re.I)


def _past_behaviour(part: str) -> str:
    topic = _LEADING_PREFIX.sub("", part.strip(" ?"))
    topic = _HYPOTHETICAL_PREFIX.sub("", topic)
    topic = _QUESTION_WORD.sub("", topic).strip(" ,?") or "это"
    return (
        f"Расскажите, когда в последний раз с вами было такое: «{topic}». "
        "Что вы тогда сделали и во что это обошлось?"
    )


def suggest_rewrite(question: str, issues: list[QuestionIssue]) -> str:
    """Шаблон переписывания в вопрос о прошлом поведении: без наводящих и гипотетических оборотов,
    двойной вопрос — на отдельные. Окончательную формулировку уточняет человек или модель."""
    if not issues:
        return question
    parts = [question]
    if QuestionIssue.DOUBLE in issues:
        parts = [p for p in _SPLIT.split(question) if p and p.strip(" ?")]
    rewrites = [_past_behaviour(p) for p in parts]
    if len(rewrites) == 1:
        return rewrites[0]
    return "Разделите на отдельные вопросы: " + " / ".join(rewrites)


def lint_question(question: str) -> QuestionFinding:
    issues = [issue for issue, pattern in _PATTERNS if pattern.search(question)]
    if _double_barreled(question):
        issues.append(QuestionIssue.DOUBLE)
    return QuestionFinding(question=question, issues=issues, suggestion=suggest_rewrite(question, issues))


def lint_interview_guide(questions: list[str]) -> list[QuestionFinding]:
    return [lint_question(q) for q in questions]


class PanelVerdict(Entity):
    trust: str  # normal | low
    placebo_liked: bool
    framing_sensitive_questions: list[str] = Field(default_factory=list)
    spread_warning: str = ""
    label: str = SYNTHETIC_LABEL


def panel_verdict(
    concept_scores: dict[str, list[float]],
    placebo_id: str,
    framing_pairs: dict[str, tuple[float, float]] | None = None,
    scale_max: float = 5.0,
    scale_min: float = 1.0,
) -> PanelVerdict:
    """Плацебо понравилось (не хуже среднего реального концепта или выше середины шкалы) → доверие к панели низкое.
    Ответы переворачиваются при позитивной и негативной формулировке → вопрос наводящий.
    Малый разброс → реальный разброс почти наверняка шире (двойники занижают его в 94% исходов)."""
    placebo = concept_scores.get(placebo_id, [])
    real = [s for cid, scores in concept_scores.items() if cid != placebo_id for s in scores]
    placebo_mean = sum(placebo) / len(placebo) if placebo else 0.0
    real_mean = sum(real) / len(real) if real else 0.0
    mid = (scale_min + scale_max) / 2  # у шкалы 1–5 нейтральная точка — 3, а не 2,5
    placebo_liked = bool(placebo) and (placebo_mean >= real_mean or placebo_mean > mid + 0.5)
    flips = [
        q
        for q, (positive, negative) in (framing_pairs or {}).items()
        if (positive - mid) * (negative - mid) > 0  # «за» (или «против») в обеих формулировках
    ]
    spreads = [
        pstdev(scores) for cid, scores in concept_scores.items() if cid != placebo_id and len(scores) > 1
    ]
    spread_warning = (
        "Разброс оценок панели мал — у реальных людей он почти наверняка шире"
        if spreads and max(spreads) < 0.5
        else ""
    )
    return PanelVerdict(
        trust="low" if placebo_liked or flips else "normal",
        placebo_liked=placebo_liked,
        framing_sensitive_questions=flips,
        spread_warning=spread_warning,
    )


# --- SSR: оценка по тексту реакции, а не по названной моделью цифре ----------------------------

SSR_ANCHORS = (
    "Мне это точно не нужно, пользоваться не буду.",
    "Скорее не нужно, вряд ли стану пользоваться.",
    "Не уверен, может быть, попробую.",
    "Скорее нужно, вероятно, буду пользоваться.",
    "Это мне точно нужно, буду пользоваться.",
)


def ssr_score(similarities: list[float]) -> float | None:
    """Semantic similarity rating (по мотивам arXiv 2510.08338): сходство текста с опорными формулировками
    шкалы 1–5 → распределение (за вычетом минимального сходства) → ожидаемая оценка.
    Модели, которых просят назвать цифру, сжимают шкалу; оценка по тексту ближе к человеческому разбросу."""
    if len(similarities) != len(SSR_ANCHORS):
        return None
    low = min(similarities)
    weights = [s - low for s in similarities]
    total = sum(weights)
    if total <= 1e-9:
        return None  # текст одинаково похож на все опоры — оценки нет
    return sum((i + 1) * w for i, w in enumerate(weights)) / total


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def find_disagreements(
    answers: list[PersonaAnswer], placebo_id: str, min_spread: float = 1.0
) -> list[Disagreement]:
    """Разногласия профилей по каждому концепту: кто «за», кто «против» и почему. Средние не считаются."""
    by_concept: dict[str, list[PersonaAnswer]] = {}
    for a in answers:
        if a.concept_id != placebo_id:
            by_concept.setdefault(a.concept_id, []).append(a)
    out = []
    for concept_id, items in by_concept.items():
        scores = [a.score for a in items]
        spread = max(scores) - min(scores)
        if spread < min_spread:
            continue
        hi = [a for a in items if a.score >= 3.5]
        lo = [a for a in items if a.score <= 2.5]
        out.append(
            Disagreement(
                concept_id=concept_id,
                spread=round(spread, 2),
                supporters=[a.persona_id for a in hi],
                opponents=[a.persona_id for a in lo],
                reasons_for=[a.reaction for a in hi][:3],
                reasons_against=[a.concern or a.reaction for a in lo][:3],
            )
        )
    return sorted(out, key=lambda d: d.spread, reverse=True)


# --- Карантин: синтетика не становится фактом и числом о спросе -----------------------------

_DEMAND_NUMBER = re.compile(r"\d+(?:[.,]\d+)?\s*%|\d+\s+из\s+\d+", re.I)


def quarantine_violations(case: DecisionCase) -> list[str]:
    """Строки реестра, где модельные респонденты стали фактом, метрикой или числом о спросе и долях.

    Синтетика разрешена только как тип SYNTHETIC (гипотеза). Смоделированные внутренние данные
    (synthetic_kind=simulated_data) — не модельные респонденты: это сценарий кейса, правило к ним не применяется.
    """
    problems = []
    for claim in case.active_claims():
        if claim.type == ClaimType.SYNTHETIC:
            continue
        sources = [case.source(ev.source_id) for e in claim.evidence_ids if (ev := case.evidence_item(e))]
        if claim.computation and claim.computation.source_id:
            sources.append(case.source(claim.computation.source_id))
        flags = [s.is_model_respondents for s in sources if s is not None]
        if not flags or not all(flags):
            continue
        if claim.type in (ClaimType.FACT, ClaimType.DERIVED_METRIC):
            problems.append(f"{claim.id}: модельные респонденты записаны как {claim.type.value}")
        elif _DEMAND_NUMBER.search(claim.text):
            problems.append(f"{claim.id}: число о спросе или долях из модельных респондентов")
    return problems
