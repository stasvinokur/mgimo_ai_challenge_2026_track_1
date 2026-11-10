"""Живые тесты адаптеров на реальных моделях Ollama. Запуск: `make live` (нужен демон и ollama signin)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from fakes import FixedClock, InMemoryCostLedger, ListEventBus
from strategist.application.ports import DecisionKind, DecisionQuestion, LLMMessage, LLMRequest
from strategist.domain.services.similarity import cosine
from strategist.infrastructure.config import load_settings
from strategist.infrastructure.llm.auxiliary import LLMNLI, OllamaEmbeddings
from strategist.infrastructure.llm.decide import SystemOneDecision
from strategist.infrastructure.llm.ollama_client import OllamaClient
from strategist.infrastructure.llm.router import RoutedLLM

pytestmark = pytest.mark.live


class Verdict(BaseModel):
    verdict: str
    reason: str


@pytest.fixture(scope="module")
def settings():
    return load_settings()


@pytest.fixture(scope="module")
def client(settings):
    return OllamaClient(settings.ollama_host, settings.ollama_api_key)


def test_routed_structured_call_returns_valid_json(settings, client):
    costs, events = InMemoryCostLedger(), ListEventBus()
    llm = RoutedLLM(client, settings.models, costs=costs, events=events, clock=FixedClock())
    response = llm.complete(
        LLMRequest(
            task="extract_claims",
            system="Ты аналитик. Ответь JSON.",
            messages=[
                LLMMessage(
                    role="user",
                    content="Удержание M3 упало с 41% до 33%. Это факт или гипотеза? verdict: fact|hypothesis",
                )
            ],
            json_schema=Verdict.model_json_schema(),
            schema_name="Verdict",
            case_id="live",
        )
    )
    obj = Verdict.model_validate(response.data)
    assert obj.verdict in ("fact", "hypothesis")
    assert costs.items and costs.items[0].input_tokens > 0
    assert events.of("llm.called")


def test_clef_choice_and_consistency(settings):
    costs, events = InMemoryCostLedger(), ListEventBus()
    decide = SystemOneDecision(
        f"{settings.ollama_host}/v1",
        "clef:cloud",
        costs=costs,
        events=events,
        clock=FixedClock(),
        threshold=0.7,
        consistency_tasks=("promise_supported",),
    )
    choice = decide.decide(
        DecisionQuestion(
            id="q1",
            kind=DecisionKind.CHOICE,
            instructions="Тип утверждения?",
            state="Утверждение: по данным когорт удержание M3 снизилось с 41% до 33%.",
            options={"fact": "факт из данных", "hypothesis": "гипотеза о причине", "forecast": "прогноз"},
            task="claim_type",
            case_id="live",
        )
    )
    assert choice.choice == "fact" and choice.confidence > 0.5
    noul = decide.decide(
        DecisionQuestion(
            id="q2",
            kind=DecisionKind.NOUL,
            instructions="Подтверждает ли функция обещание?",
            state="Функция: мастер настройки из 3 шагов. Обещание: «настройка за три шага».",
            task="promise_supported",
            case_id="live",
        )
    )
    assert noul.probability is not None and noul.probability > 0.5
    assert costs.items and all(r.backend == "systemone" for r in costs.items)


def test_embeddings_and_nli(settings, client):
    emb = OllamaEmbeddings(client, settings.models["roles"]["embeddings"]["model"])
    a, b, c = emb.embed(
        ["Удержание подписки падает", "Пользователи перестают продлевать подписку", "Рецепт борща"]
    )
    assert cosine(a, b) > cosine(a, c)
    llm = RoutedLLM(client, settings.models, costs=None, events=ListEventBus(), clock=FixedClock())
    result = LLMNLI(llm).check("Удержание M3 снизилось с 41% до 33% за квартал.", "Удержание M3 снизилось")
    assert result.label.value == "entailment"
