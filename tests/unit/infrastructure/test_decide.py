"""SystemOneDecision: разбор ответов, эскалация ниже порога, расхождение вариантов, недоступность."""

from __future__ import annotations

import json

import httpx

from fakes import FixedClock, InMemoryCostLedger, ListEventBus, ScriptedDecision
from strategist.application.ports import DecisionKind, DecisionQuestion
from strategist.infrastructure.llm.decide import SystemOneDecision


def _transport(answers_fn):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"model": "clef", "answers": answers_fn(body), "usage": {"input_tokens": 300}}
        )

    return httpx.MockTransport(handler)


def _decide(answers_fn, **kw):
    fallback = ScriptedDecision(
        lambda q: ("hypothesis", 0.99) if q.kind == DecisionKind.CHOICE else (True, 0.95)
    )
    events = ListEventBus()
    costs = InMemoryCostLedger()
    d = SystemOneDecision(
        "http://test/v1",
        "clef:cloud",
        costs=costs,
        events=events,
        clock=FixedClock(),
        threshold=0.7,
        fallback=fallback,
        transport=_transport(answers_fn),
        **kw,
    )
    return d, fallback, events, costs


def _choice_q(task="claim_type"):
    return DecisionQuestion(
        id="q",
        kind=DecisionKind.CHOICE,
        instructions="Тип?",
        state="s",
        options={"fact": "факт", "hypothesis": "гипотеза"},
        task=task,
        case_id="c1",
    )


def test_confident_choice_is_kept_and_costed():
    d, fallback, events, costs = _decide(
        lambda body: {
            "q": {
                "type": "choice",
                "choice": "fact",
                "confidence": 0.9,
                "probabilities": {"fact": 0.95, "hypothesis": 0.05},
            }
        }
    )
    result = d.decide(_choice_q())
    assert result.choice == "fact" and result.confidence == 0.95 and result.backend == "systemone"
    assert not fallback.questions and not events.of("route.misrouted")
    assert costs.items[0].input_tokens == 300 and costs.items[0].backend == "systemone"


def test_low_confidence_escalates_and_logs_misroute():
    d, fallback, events, _ = _decide(
        lambda body: {
            "q": {"type": "choice", "choice": "fact", "probabilities": {"fact": 0.55, "hypothesis": 0.45}}
        }
    )
    result = d.decide(_choice_q())
    assert result.escalated and result.choice == "hypothesis"
    assert fallback.questions and events.of("route.misrouted")


def test_variant_disagreement_escalates_for_critical_tasks():
    def answers(body):
        assert "v" in body["questions"], "для критичного вопроса должен уйти проверочный вариант"
        return {
            "q": {"type": "noul", "noul": 0.9},
            "v": {"type": "noul", "noul": 0.8},
        }  # оба «да» — противоречие

    d, fallback, events, _ = _decide(answers, consistency_tasks=("promise_supported",))
    q = DecisionQuestion(
        id="q",
        kind=DecisionKind.NOUL,
        instructions="Подтверждено?",
        state="s",
        task="promise_supported",
        case_id="c1",
    )
    result = d.decide(q)
    assert result.escalated and events.of("route.misrouted")


def test_consistent_variant_keeps_answer():
    d, fallback, _, _ = _decide(
        lambda body: {"q": {"type": "noul", "noul": 0.9}, "v": {"type": "noul", "noul": 0.1}},
        consistency_tasks=("promise_supported",),
    )
    q = DecisionQuestion(
        id="q", kind=DecisionKind.NOUL, instructions="?", state="s", task="promise_supported"
    )
    result = d.decide(q)
    assert not result.escalated and result.probability == 0.9 and not fallback.questions


def test_unavailable_system_one_falls_back(monkeypatch):
    monkeypatch.setattr("strategist.infrastructure.llm.decide.time.sleep", lambda s: None)

    def handler(request):
        return httpx.Response(503)

    fallback = ScriptedDecision()
    events = ListEventBus()
    d = SystemOneDecision(
        "http://test/v1",
        "clef:cloud",
        costs=None,
        events=events,
        clock=FixedClock(),
        fallback=fallback,
        transport=httpx.MockTransport(handler),
    )
    result = d.decide(_choice_q())
    assert result.backend == "fake" and events.of("route.fallback")


def test_score_parsing():
    d, *_ = _decide(
        lambda body: {"q": {"type": "score", "score": 3.4, "confidence": 0.8, "probabilities": {"3": 0.8}}}
    )
    q = DecisionQuestion(
        id="q", kind=DecisionKind.SCORE, instructions="?", state="s", levels=["1", "2", "3", "4", "5"]
    )
    assert d.decide(q).score == 3.4
