"""Composition root: единственное место, где собираются адаптеры и сценарии.

Интерфейсы (CLI, MCP, UI) получают отсюда готовое приложение и не импортируют infrastructure.
Режимы: live — реальные модели; record — реальные модели + запись кассет; replay — только кассеты
(демо без доступа к моделям, «сегодня» сдвинуто к дню записи).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strategist.application.context import AppContext
from strategist.application.dto.diagnostics import DiagnosticCheck
from strategist.application.ports import Event, WorkflowRunner
from strategist.infrastructure.config import Settings, load_settings
from strategist.infrastructure.diagnostics import run_diagnostics
from strategist.infrastructure.persistence.repositories import SqliteEventLog
from strategist.infrastructure.runtime import OffsetClock


@dataclass
class App:
    ctx: AppContext
    runner: WorkflowRunner
    settings: Settings
    mode: str
    event_log: SqliteEventLog
    cassette_dir: Path | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def subscribe(self, handler: Callable[[Event], None], event_type: str = "*") -> None:
        self.ctx.events.subscribe(event_type, handler)

    @property
    def cases_dir(self) -> Path:
        return self.settings.paths.cases_dir


def settings(root: Path | None = None) -> Settings:
    return load_settings(root)


def diagnostics(root: Path | None = None) -> list[DiagnosticCheck]:
    return run_diagnostics(load_settings(root))


def _cassette_clock(cassette_dir: Path, mode: str) -> Any:
    meta_path = cassette_dir / "meta.json"
    if mode == "replay":
        if not meta_path.exists():
            raise FileNotFoundError(f"Нет записанного прогона: {meta_path}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return OffsetClock(datetime.fromisoformat(meta["recorded_at"]))
    if meta_path.exists():  # дозапись в тот же прогон — то же «сегодня»
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return OffsetClock(datetime.fromisoformat(meta["recorded_at"]))
    cassette_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    meta_path.write_text(json.dumps({"recorded_at": now.isoformat()}, ensure_ascii=False), encoding="utf-8")
    return OffsetClock(now)


def build_app(
    root: Path | None = None,
    *,
    mode: str = "live",
    cassette_dir: Path | None = None,
    interactive: bool = True,
    auto_answers: dict[str, Any] | None = None,
    allow_web: bool | None = None,
    cases_dir: Path | None = None,
) -> App:
    from strategist.infrastructure.artifacts import FileArtifactWriter, MarkdownRenderer
    from strategist.infrastructure.hooks.handlers import register_hooks
    from strategist.infrastructure.knowledge.index import SqliteKnowledgeIndex
    from strategist.infrastructure.llm.auxiliary import LLMNLI, OllamaEmbeddings
    from strategist.infrastructure.llm.cassette import CassetteDecision, CassetteEmbeddings, CassetteLLM
    from strategist.infrastructure.llm.decide import LLMDecision, SystemOneDecision
    from strategist.infrastructure.llm.ollama_client import OllamaClient
    from strategist.infrastructure.llm.router import RoutedLLM
    from strategist.infrastructure.orchestration.langgraph.graph import AutoGate, LangGraphGate
    from strategist.infrastructure.orchestration.langgraph.runner import LangGraphRunner
    from strategist.infrastructure.persistence.costs import SqliteCostLedger
    from strategist.infrastructure.persistence.db import CaseDatabase
    from strategist.infrastructure.persistence.materials import FileMaterialStore
    from strategist.infrastructure.persistence.repositories import (
        SqliteCaseRepository,
        SqliteEventLog,
        SqliteJournal,
        SqliteLedger,
        SqliteResponseCache,
    )
    from strategist.infrastructure.pricing import Pricing
    from strategist.infrastructure.prompts import FilePromptRepository
    from strategist.infrastructure.runtime import InProcessEventBus, SystemClock
    from strategist.infrastructure.security.guard import RegexContentGuard
    from strategist.infrastructure.tools.parsers import default_parsers
    from strategist.infrastructure.tools.tables import PandasDuckdbTableEngine
    from strategist.infrastructure.tools.web import AllowlistWebFetcher

    s = load_settings(root)
    if cases_dir is not None:
        s.paths = s.paths.__class__.from_root(s.paths.root, str(cases_dir))
    if mode not in ("live", "record", "replay", "update"):
        raise ValueError("mode: live | record | replay | update")
    if mode != "live" and cassette_dir is None:
        raise ValueError("Для record/replay/update нужен каталог кассет")
    cases = s.paths.cases_dir
    db = CaseDatabase(cases)
    clock = _cassette_clock(cassette_dir, mode) if mode != "live" and cassette_dir else SystemClock()
    events = InProcessEventBus()
    event_log = SqliteEventLog(db)
    events.subscribe("*", event_log)
    pricing = Pricing(s.prices)
    costs = SqliteCostLedger(db, pricing)

    client = OllamaClient(
        s.ollama_host, s.ollama_api_key, timeout=float(s.models.get("request_timeout_seconds", 600))
    )
    routed = RoutedLLM(
        client, s.models, costs=costs, events=events, clock=clock, cache=SqliteResponseCache(db)
    )
    roles = s.models.get("roles", {})
    embeddings: Any = OllamaEmbeddings(
        client,
        roles.get("embeddings", {}).get("model", "qwen3-embedding:8b"),
        cache_path=cases / ".cache" / "embeddings.json",
    )
    decide_cfg = s.models.get("decide", {})
    threshold = float(decide_cfg.get("confidence_threshold", 0.7))
    fallback = LLMDecision(routed, role=decide_cfg.get("escalate_role", "reasoning"))
    backend = s.decide_backend
    consistency = tuple(decide_cfg.get("consistency_tasks", []))
    decision: Any
    if backend == "systemone":
        decision = SystemOneDecision(
            f"{s.ollama_host.rstrip('/')}/v1",
            decide_cfg.get("systemone", {}).get("model", "clef:cloud"),
            api_key=s.ollama_api_key,
            costs=costs,
            events=events,
            clock=clock,
            backend_name="systemone",
            threshold=threshold,
            fallback=fallback,
            consistency_tasks=consistency,
            score_threshold=float(decide_cfg.get("score_threshold", 0.0)),
        )
    elif backend == "jev":
        jev = decide_cfg.get("jev", {})
        decision = SystemOneDecision(
            jev.get("base_url", "https://api.typesafe.ai/v1"),
            jev.get("model", "jev-1.13.0"),
            api_key=s.env.get(jev.get("api_key_env", "TYPESAFE_API_KEY")),
            costs=costs,
            events=events,
            clock=clock,
            backend_name="jev",
            # Свой порог профиля: на русском наборе у Jev 0,8 ловит все ошибки (evals/spikes/decide/REPORT.md)
            threshold=float(jev.get("confidence_threshold", threshold)),
            fallback=fallback,
            consistency_tasks=consistency,
            score_threshold=float(decide_cfg.get("score_threshold", 0.0)),
        )
    else:
        decision = LLMDecision(routed, role=decide_cfg.get("llm_role", "workhorse"))

    llm: Any = routed
    if mode != "live" and cassette_dir is not None:
        live = mode in ("record", "update")  # update: нет записи — вызвать модель и дописать
        llm = CassetteLLM(cassette_dir, mode, inner=routed if live else None, costs=costs, clock=clock)
        embeddings = CassetteEmbeddings(cassette_dir, mode, inner=embeddings if live else None)
        decision = CassetteDecision(
            cassette_dir, mode, inner=decision if live else None, costs=costs, clock=clock
        )

    workflow = dict(s.workflow)
    workflow["decide"] = decide_cfg
    if allow_web is not None:
        workflow.setdefault("research", {})["allow_web"] = allow_web
    if mode == "replay":
        workflow.setdefault("research", {})["allow_web"] = False

    knowledge = SqliteKnowledgeIndex(cases / ".knowledge" / "knowledge.db", embeddings, clock)
    gate: Any = LangGraphGate() if interactive else AutoGate(auto_answers)
    ctx = AppContext(
        llm=llm,
        decision=decision,
        embeddings=embeddings,
        nli=LLMNLI(llm),
        cases=SqliteCaseRepository(db),
        ledger=SqliteLedger(db),
        materials=FileMaterialStore(cases, clock),
        knowledge=knowledge,
        parsers=default_parsers(),
        tables=PandasDuckdbTableEngine(),
        web=AllowlistWebFetcher(s.allowlist, clock),
        prompts=FilePromptRepository(s.paths.instructions_dir, s.paths.skills_dir),
        costs=costs,
        journal=SqliteJournal(db),
        gate=gate,
        events=events,
        clock=clock,
        artifacts=FileArtifactWriter(cases),
        guard=RegexContentGuard(mask_pii=bool(s.workflow.get("security", {}).get("mask_pii", True))),
        renderer=MarkdownRenderer(),
        settings=workflow,
    )
    register_hooks(ctx, s)
    runner = LangGraphRunner(ctx, cases)
    return App(ctx=ctx, runner=runner, settings=s, mode=mode, event_log=event_log, cassette_dir=cassette_dir)


def reindex_knowledge(application: App, case_ids: list[str] | None = None) -> dict[str, int]:
    """Пересборка базы знаний из файлов методики и навыков + индекс выбранных кейсов."""
    from strategist.application.use_cases.knowledge import index_case
    from strategist.infrastructure.knowledge.index import load_methodology_docs

    paths = application.settings.paths
    docs = load_methodology_docs(paths.knowledge_dir / "methodology", paths.skills_dir)
    application.ctx.knowledge.rebuild(docs, kinds=["methodology"])  # type: ignore[attr-defined]
    counts = {"methodology": len(docs)}
    ids = case_ids if case_ids is not None else [c.id for c in application.ctx.cases.list_cases()]
    for case_id in ids:
        counts[case_id] = index_case(application.ctx, application.ctx.cases.load(case_id))
    return counts
