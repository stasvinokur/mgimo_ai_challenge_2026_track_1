"""Контекст сценариев: набор портов, который собирает composition root (или тест на фейках)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strategist.application.ports import (
    ArtifactWriter,
    CaseRepository,
    Clock,
    ContentGuard,
    CostLedger,
    DecisionPort,
    DocumentParser,
    DocumentRenderer,
    EmbeddingPort,
    Event,
    EventBus,
    GateRequest,
    GateResponse,
    HumanGate,
    JournalPort,
    KnowledgeIndex,
    LedgerRepository,
    LLMPort,
    MaterialStore,
    NLIPort,
    PromptRepository,
    TableEngine,
    WebFetcher,
)


@dataclass
class AppContext:
    llm: LLMPort
    decision: DecisionPort
    embeddings: EmbeddingPort
    nli: NLIPort
    cases: CaseRepository
    ledger: LedgerRepository
    materials: MaterialStore
    knowledge: KnowledgeIndex
    parsers: list[DocumentParser]
    tables: TableEngine
    web: WebFetcher
    prompts: PromptRepository
    costs: CostLedger
    journal: JournalPort
    gate: HumanGate
    events: EventBus
    clock: Clock
    artifacts: ArtifactWriter | None = None
    guard: ContentGuard | None = None
    renderer: DocumentRenderer | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    run_id: str = "run-1"

    def ask_gate(self, request: GateRequest) -> GateResponse:
        """Шлюз человека. Событие gate.answered публикуется один раз — когда ответ получен
        (при interrupt() узел перезапускается, но до ответа ask() не возвращается)."""
        response = self.gate.ask(request)
        self.emit(
            "gate.answered",
            request.case_id,
            gate=request.gate.value,
            action=response.action.value,
            choice=response.choice,
            approver=response.approver,
        )
        return response

    def emit(self, event_type: str, case_id: str = "", **data: Any) -> None:
        self.events.publish(Event(type=event_type, case_id=case_id, at=self.clock.now(), data=data))

    def setting(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.settings
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def parser_for(self, path: str) -> DocumentParser | None:
        return next((p for p in self.parsers if p.supports(path)), None)
