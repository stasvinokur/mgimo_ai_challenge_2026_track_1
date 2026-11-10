"""JSON для экранов веб-панели: только то, что видит руководитель, без внутренних сущностей целиком.

Каждая функция берёт кейс (и, где нужно, статус исполнителя) и отдаёт словарь, который фронтенд показывает как есть.
Логика — в use cases и правилах домена; здесь только выбор и переименование полей.
"""

from __future__ import annotations

from typing import Any

from strategist.application.ports import CostSummary, GateKind, GateRequest, RunStatus
from strategist.domain.common import SENTENCE_END, clip_words
from strategist.domain.entities import DecisionCase
from strategist.domain.services.dependency_graph import NODE_KIND_RU
from strategist.domain.services.revisit_rules import what_to_decide_now
from strategist.domain.services.self_audit import compute_self_audit


def short(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[: limit - 3].rsplit(" ", 1)[0] + "…"


def display_title(title: str, limit: int = 60) -> str:
    """Название для шапки и списка: у демо оно — первая строка запроса целиком, на экране — до первого тире."""
    head = title.split(" — ", 1)[0].strip()
    return short(head if len(head) >= 20 else title, limit)


SUPPORT_RU = {
    "supported": "подтверждено",
    "partial": "частично",
    "unsupported": "нет опоры",
    "contradicted": "опровергнуто",
}


def _ended(reason: str, limit: int = 300) -> str:
    """Причина пересмотра из прогонов до v0.3.0 обрезана при записи ровно на 300 символах, бывает — посреди слова:
    такую показываем до границы слова и с «…»."""
    if len(reason) < limit or reason.rstrip().endswith(SENTENCE_END):
        return reason
    return clip_words(reason, limit - 1)


def case_list_item(summary: Any) -> dict[str, Any]:
    return {
        "id": summary.id,
        "title": display_title(summary.title),
        "stage": summary.stage,
        "updated_at": summary.updated_at.isoformat(),
    }


def gate_view(gate: GateRequest | None, case: DecisionCase) -> dict[str, Any] | None:
    if gate is None:
        return None
    out: dict[str, Any] = {
        "kind": gate.gate.value,
        "title": gate.title,
        "details_md": gate.summary,  # таблицы плана, оценки ролей, стресс-тест — под «Подробнее»
        "questions": gate.questions,
        "default_choice": gate.default_choice,
    }
    if gate.gate == GateKind.OPTION_CHOICE:
        titles = {o.id: o for o in case.options}
        out["choices"] = [
            {
                "id": cid,
                "title": titles[cid].title if cid in titles else cid,
                "kind": titles[cid].kind.label_ru if cid in titles else "",
            }
            for cid in gate.choices
        ]
    if gate.gate == GateKind.RESEARCH_PLAN and case.research_plan:
        out["plan"] = [
            {
                "id": q.id,
                "question": q.question,
                "planned": q.status.value == "planned",
                "why": q.rationale if q.status.value == "planned" else q.skip_reason,
            }
            for q in case.research_plan.questions
        ]
    if gate.gate == GateKind.DECISION_COMMIT:
        chosen = case.option(case.selected_option_id or "")
        out["option"] = chosen.title if chosen else ""
    return out


def metrics_view(case: DecisionCase, cost: CostSummary | None) -> dict[str, Any]:
    audit = compute_self_audit(case)
    return {
        "claims": audit.claims_total,
        "unsupported_share": audit.unsupported_share,
        "citation_accuracy": audit.citation_accuracy,
        "sources": audit.sources_total,
        "rub": round(cost.rub, 2) if cost else 0.0,
        "usd": round(cost.usd, 4) if cost else 0.0,
        "calls": cost.calls if cost else 0,
    }


def executive_view(case: DecisionCase) -> dict[str, Any] | None:
    s = case.executive
    if s is None:
        return None
    reasons = []
    for i, text in enumerate(s.reasons):
        b = s.basis[i] if i < len(s.basis) else None
        reasons.append(
            {
                "text": text,
                "kind": b.kind_label if b else "",
                "source": b.source if b else "",
                "caveat": b.caveat if b else "",
                "claim_ids": b.claim_ids if b else [],
            }
        )
    return {
        "answer": s.answer,
        "reasons": reasons,
        "next_steps": [
            {"when": st.when, "action": st.action, "owner": st.owner, "metric": st.metric}
            for st in s.next_steps
        ],
        "asks": s.asks,
        "revisit": s.revisit,
        "say_to_team": s.say_to_team,
    }


def revision_view(case: DecisionCase) -> dict[str, Any] | None:
    if not case.revisions:
        return None
    rev = case.revisions[-1]
    decision = case.active_decision()
    fired = [t for t in (decision.triggers if decision else []) if t.fired]
    share = (
        rev.tokens_used / rev.tokens_full_regeneration_estimate
        if rev.tokens_full_regeneration_estimate
        else None
    )
    return {
        "summary": rev.summary,
        "reopened": bool(rev.reopened_decision_id),
        "contested": rev.contested_claim_ids,
        "revised": [
            {
                "id": r.node_id,
                "kind": NODE_KIND_RU.get(r.node_kind, r.node_kind),
                "before": r.before,
                "after": r.after,
                "reason": _ended(r.reason),
            }
            for r in rev.results
            if r.verdict.value == "revised"
        ],
        "untouched": len(rev.untouched_ids),
        "signals": [t.model_dump(mode="json") for t in fired],
        "tokens": rev.tokens_used,
        "token_share": share,
    }


def case_view(case: DecisionCase, status: RunStatus, today: Any) -> dict[str, Any]:
    return {
        "id": case.id,
        "title": display_title(case.title, 90),
        "brief": case.brief,
        "stage": case.stage.value,
        "stage_label": case.stage.label_ru,
        "version": case.version,
        "finished": status.finished,
        "gate": gate_view(status.waiting_gate, case),
        "steps": status.completed_steps,
        "metrics": metrics_view(case, status.cost),
        "executive": executive_view(case),
        "agenda": [
            {"title": a.title, "detail": a.detail, "due": a.due.isoformat() if a.due else None}
            for a in what_to_decide_now(case, today)[:6]
        ],
        "revision": revision_view(case),
    }
