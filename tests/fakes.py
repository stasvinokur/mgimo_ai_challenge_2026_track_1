"""In-memory фейки всех портов: сценарии тестируются без сети, моделей и файлов."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any

from strategist.application.context import AppContext
from strategist.application.errors import CaseNotFound, NotAllowedError
from strategist.application.ports import (
    CaseSummary,
    CaseVersion,
    CostRecord,
    CostSummary,
    DecisionKind,
    DecisionQuestion,
    DecisionResult,
    Event,
    Fragment,
    GateRequest,
    GateResponse,
    HumanTimeRecord,
    JournalEntry,
    JournalStatus,
    KnowledgeDoc,
    LedgerEntry,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    MaterialFile,
    MemoryRecord,
    MemoryStatus,
    MetricResult,
    MetricSpec,
    NLILabel,
    NLIResult,
    ParsedDocument,
    PromptTemplate,
    SearchHit,
    TablePassport,
    WebSnapshot,
)
from strategist.domain.entities import DecisionCase, Locator


class FixedClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 11, 4, 10, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def today(self) -> date:
        return self.current.date()

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


class ListEventBus:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.handlers: dict[str, list[Callable[[Event], None]]] = defaultdict(list)

    def publish(self, event: Event) -> None:
        self.events.append(event)
        for handler in [*self.handlers.get(event.type, []), *self.handlers.get("*", [])]:
            handler(event)

    def subscribe(self, event_type: str, handler: Callable[[Event], None]) -> None:
        self.handlers[event_type].append(handler)

    def of(self, event_type: str) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


Script = Callable[[LLMRequest], Any] | list[Any] | dict[str, Any]


class ScriptedLLM:
    """Ответы по типу задачи: функция от запроса, список (по очереди) или готовый dict."""

    def __init__(self, scripts: dict[str, Script] | None = None, model: str = "fake-model") -> None:
        self.scripts = scripts or {}
        self.model = model
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        script = self.scripts.get(request.task)
        if script is None:
            raise AssertionError(f"Нет сценария для задачи {request.task}")
        if callable(script):
            data = script(request)
        elif isinstance(script, list):
            data = script.pop(0) if len(script) > 1 else script[0]
        else:
            data = script
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        parsed = None if isinstance(data, str) else data
        tokens_in = sum(len(m.content) for m in request.messages) // 4 + len(request.system) // 4
        return LLMResponse(
            text=text,
            data=parsed,
            usage=LLMUsage(
                model=self.model + ("+esc" if request.escalation else ""),
                role="test",
                input_tokens=tokens_in,
                output_tokens=len(text) // 4,
                escalated=request.escalation > 0,
            ),
        )

    def tasks(self) -> list[str]:
        return [c.task for c in self.calls]


class ScriptedDecision:
    """decide() по правилу; по умолчанию — первый вариант / «да» с уверенностью 0.9."""

    def __init__(self, rule: Callable[[DecisionQuestion], tuple[Any, float]] | None = None) -> None:
        self.rule = rule
        self.questions: list[DecisionQuestion] = []

    def decide(self, question: DecisionQuestion) -> DecisionResult:
        self.questions.append(question)
        if self.rule:
            answer, confidence = self.rule(question)
        elif question.kind == DecisionKind.CHOICE:
            answer, confidence = next(iter(question.options)), 0.9
        elif question.kind == DecisionKind.NOUL:
            answer, confidence = True, 0.9
        else:
            answer, confidence = len(question.levels) - 1, 0.9
        result = DecisionResult(
            question_id=question.id, kind=question.kind, confidence=confidence, backend="fake"
        )
        if question.kind == DecisionKind.CHOICE:
            result.choice = str(answer)
            result.probs = {str(answer): confidence}
        elif question.kind == DecisionKind.NOUL:
            result.probability = confidence if answer else 1 - confidence
        else:
            result.score = float(answer)
        return result

    def decide_many(self, questions: list[DecisionQuestion]) -> list[DecisionResult]:
        return [self.decide(q) for q in questions]


class HashEmbeddings:
    """Детерминированные «эмбеддинги» по триграммам: похожие тексты → близкие векторы."""

    def __init__(self, dims: int = 128) -> None:
        self.dims = dims

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dims
            norm = re.sub(r"\s+", " ", text.lower())
            for i in range(max(1, len(norm) - 2)):
                gram = norm[i : i + 3]
                h = int(hashlib.md5(gram.encode()).hexdigest(), 16)
                vec[h % self.dims] += 1.0
            length = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / length for v in vec])
        return out


class KeywordNLI:
    """Опора, если все значимые слова гипотезы есть в посылке; противоречие — по маркеру «не»."""

    def check(self, premise: str, hypothesis: str, *, case_id: str | None = None) -> NLIResult:
        words = [w for w in re.findall(r"[\wё]{4,}", hypothesis.lower())]
        p = premise.lower()
        hit = sum(1 for w in words if w[:5] in p)
        share = hit / len(words) if words else 0.0
        if " не " in f" {hypothesis.lower()} " and " не " not in f" {p} " and share > 0.5:
            return NLIResult(label=NLILabel.CONTRADICTION, score=0.8, model="keyword")
        if share >= 0.8:
            return NLIResult(label=NLILabel.ENTAILMENT, score=share, model="keyword")
        return NLIResult(label=NLILabel.NEUTRAL, score=share, model="keyword")


class InMemoryCaseRepository:
    def __init__(self) -> None:
        self.snapshots: dict[str, list[tuple[CaseVersion, str]]] = {}

    def exists(self, case_id: str) -> bool:
        return case_id in self.snapshots

    def create(self, case: DecisionCase) -> None:
        case.version = 1
        self.snapshots[case.id] = [
            (CaseVersion(version=1, at=case.updated_at, message="create"), case.model_dump_json())
        ]

    def load(self, case_id: str, version: int | None = None) -> DecisionCase:
        if case_id not in self.snapshots:
            raise CaseNotFound(case_id)
        items = self.snapshots[case_id]
        data = items[-1][1] if version is None else next(s for v, s in items if v.version == version)
        return DecisionCase.model_validate_json(data)

    def save(self, case: DecisionCase, message: str) -> int:
        items = self.snapshots[case.id]
        case.version = items[-1][0].version + 1
        items.append(
            (CaseVersion(version=case.version, at=case.updated_at, message=message), case.model_dump_json())
        )
        return case.version

    def versions(self, case_id: str) -> list[CaseVersion]:
        return [v for v, _ in self.snapshots.get(case_id, [])]

    def list_cases(self) -> list[CaseSummary]:
        out = []
        for case_id in self.snapshots:
            c = self.load(case_id)
            out.append(
                CaseSummary(
                    id=c.id, title=c.title, stage=c.stage.value, version=c.version, updated_at=c.updated_at
                )
            )
        return out


class InMemoryLedger:
    def __init__(self) -> None:
        self.entries: list[LedgerEntry] = []

    def append(self, entries: list[LedgerEntry]) -> None:
        self.entries.extend(entries)

    def history(self, case_id: str, claim_id: str | None = None) -> list[LedgerEntry]:
        return [
            e for e in self.entries if e.case_id == case_id and (claim_id is None or e.claim_id == claim_id)
        ]


class InMemoryMaterialStore:
    """Не копирует файлы: хранит хеши и метаданные (meta из словаря sidecar_meta по имени)."""

    def __init__(self, clock: FixedClock, sidecar_meta: dict[str, dict[str, Any]] | None = None) -> None:
        self.clock = clock
        self.files: dict[str, list[MaterialFile]] = defaultdict(list)
        self.paths: dict[tuple[str, str], str] = {}
        self.sidecar_meta = sidecar_meta or {}
        self.blobs: dict[tuple[str, str], bytes] = {}

    def add_file(self, case_id: str, src_path: str, *, subdir: str = "") -> MaterialFile:
        data = Path(src_path).read_bytes()
        name = (subdir + "/" if subdir else "") + Path(src_path).name
        meta = self.sidecar_meta.get(Path(src_path).name, {})
        mf = MaterialFile(
            path=name,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            title=meta.get("title", Path(src_path).stem),
            kind=meta.get("kind", "table" if src_path.endswith((".csv", ".xlsx")) else "document"),
            synthetic=bool(meta.get("synthetic", False)),
            source_url=meta.get("source_url"),
            added_at=self.clock.now(),
            meta=meta,
        )
        self.files[case_id].append(mf)
        self.paths[(case_id, name)] = src_path
        return mf

    def add_bytes(self, case_id: str, name: str, data: bytes, meta: dict[str, Any]) -> MaterialFile:
        mf = MaterialFile(
            path=name,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            title=meta.get("title", name),
            kind=meta.get("kind", "web_snapshot"),
            synthetic=bool(meta.get("synthetic", False)),
            source_url=meta.get("source_url"),
            added_at=self.clock.now(),
            meta=meta,
        )
        self.files[case_id].append(mf)
        self.blobs[(case_id, name)] = data
        return mf

    def list(self, case_id: str) -> list[MaterialFile]:
        return list(self.files.get(case_id, []))

    def verify(self, case_id: str) -> list[str]:
        bad = []
        for mf in self.files.get(case_id, []):
            path = self.paths.get((case_id, mf.path))
            if path and hashlib.sha256(Path(path).read_bytes()).hexdigest() != mf.sha256:
                bad.append(mf.path)
        return bad

    def absolute_path(self, case_id: str, rel_path: str) -> str:
        return self.paths.get((case_id, rel_path), rel_path)


class InMemoryKnowledgeIndex:
    def __init__(self) -> None:
        self.docs: dict[str, KnowledgeDoc] = {}
        self.links: list[tuple[str, str, str]] = []
        self.mem: dict[str, MemoryRecord] = {}

    def upsert(self, docs: list[KnowledgeDoc]) -> None:
        for d in docs:
            self.docs[d.id] = d

    def search(
        self, query: str, k: int = 8, kinds: list[str] | None = None, case_id: str | None = None
    ) -> list[SearchHit]:
        words = set(re.findall(r"[\wё]{4,}", query.lower()))
        hits = []
        for d in self.docs.values():
            if kinds and d.kind not in kinds:
                continue
            score = sum(1 for w in words if w in d.text.lower())
            if score:
                hits.append(
                    SearchHit(doc_id=d.id, title=d.title, kind=d.kind, snippet=d.text[:160], score=score)
                )
        return sorted(hits, key=lambda h: -h.score)[:k]

    def link(self, src_id: str, dst_id: str, relation: str) -> None:
        self.links.append((src_id, dst_id, relation))

    def neighbors(self, doc_id: str, relation: str | None = None) -> list[str]:
        return [d for s, d, r in self.links if s == doc_id and (relation is None or r == relation)]

    def remember(self, record: MemoryRecord) -> None:
        self.mem[record.id] = record

    def memories(self, case_id: str | None = None, include_inactive: bool = False) -> list[MemoryRecord]:
        return [
            m
            for m in self.mem.values()
            if (case_id is None or m.case_id == case_id)
            and (include_inactive or m.status == MemoryStatus.ACTIVE)
        ]

    def retract(self, memory_id: str, reason: str, at: datetime) -> None:
        self.mem[memory_id] = self.mem[memory_id].model_copy(
            update={"status": MemoryStatus.RETRACTED, "reason": reason}
        )

    def correct(self, memory_id: str, new_record: MemoryRecord) -> None:
        self.mem[memory_id] = self.mem[memory_id].model_copy(
            update={"status": MemoryStatus.CORRECTED, "superseded_by": new_record.id}
        )
        self.mem[new_record.id] = new_record

    def approve(self, memory_id: str, at: datetime) -> None:
        self.mem[memory_id] = self.mem[memory_id].model_copy(
            update={"approved_by_user": True, "approved_at": at}
        )


class TextParser:
    def supports(self, path: str) -> bool:
        return path.endswith((".md", ".txt"))

    def parse(self, path: str) -> ParsedDocument:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        fragments = [
            Fragment(text=line, locator=Locator(line_start=i, line_end=i))
            for i, line in enumerate(lines, start=1)
            if line.strip()
        ]
        return ParsedDocument(path=path, title=Path(path).stem, fragments=fragments)


class SimpleTableEngine:
    """Минимальный движок на csv-модуле (для тестов application)."""

    def supports(self, path: str) -> bool:
        return path.endswith(".csv")

    def _rows(self, path: str) -> list[dict[str, str]]:
        with open(path, encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def passport(self, path: str, sheet: str | None = None) -> TablePassport:
        rows = self._rows(path)
        cols = list(rows[0]) if rows else []
        return TablePassport(path=path, rows=len(rows), columns=cols)

    def compute(self, path: str, spec: MetricSpec, sheet: str | None = None) -> MetricResult:
        rows = self._rows(path)
        values = [float(r[spec.column]) for r in rows if spec.column and r.get(spec.column, "") != ""]
        fn = {"mean": mean, "sum": sum, "median": median, "min": min, "max": max, "count": len}[spec.agg]
        value = float(fn(values))
        return MetricResult(spec=spec, value=value, check_value=value, consistent=True, rows_used=len(values))

    def distinct_values(self, path: str, column: str, sheet: str | None = None, limit: int = 20) -> list[str]:
        return sorted({r[column] for r in self._rows(path)})[:limit]


class FakeWeb:
    def __init__(
        self,
        pages: dict[str, str] | None = None,
        allowed_domains: set[str] | None = None,
        clock: FixedClock | None = None,
    ) -> None:
        self.pages = pages or {}
        self.allowed_domains = allowed_domains or set()
        self.clock = clock or FixedClock()
        self.fetched: list[str] = []

    def allowed(self, url: str) -> bool:
        host = re.sub(r"^https?://", "", url).split("/")[0]
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains)

    def fetch(self, url: str) -> WebSnapshot:
        if not self.allowed(url):
            raise NotAllowedError(url)
        self.fetched.append(url)
        text = self.pages.get(url, "")
        return WebSnapshot(
            url=url,
            final_url=url,
            status=200,
            fetched_at=self.clock.now(),
            title=url,
            text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        )


class _JsonTemplate(PromptTemplate):
    def render_user(self, **values: Any) -> str:
        return json.dumps(values, ensure_ascii=False, default=str)


class DictPrompts:
    def __init__(self, templates: dict[str, PromptTemplate] | None = None) -> None:
        self.templates = templates or {}

    def get(self, name: str) -> PromptTemplate:
        return self.templates.get(name) or _JsonTemplate(name=name, version="test", system=f"system:{name}")

    def skill(self, name: str) -> str:
        return f"skill:{name}"

    def names(self) -> list[str]:
        return list(self.templates)


class InMemoryCostLedger:
    def __init__(self) -> None:
        self.items: list[CostRecord] = []
        self.human: list[HumanTimeRecord] = []

    def record(self, record: CostRecord) -> None:
        self.items.append(record)

    def record_human(self, record: HumanTimeRecord) -> None:
        self.human.append(record)

    def records(self, case_id: str) -> list[CostRecord]:
        return [r for r in self.items if r.case_id == case_id]

    def summary(self, case_id: str) -> CostSummary:
        recs = self.records(case_id)
        return CostSummary(
            calls=len(recs),
            input_tokens=sum(r.input_tokens for r in recs),
            output_tokens=sum(r.output_tokens for r in recs),
            usd=sum(r.usd for r in recs),
            human_minutes=sum(h.minutes for h in self.human if h.case_id == case_id),
        )


class InMemoryJournal:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str], JournalEntry] = {}
        self.log: list[JournalEntry] = []

    def get(self, case_id: str, run_id: str, step: str) -> JournalEntry | None:
        return self.items.get((case_id, run_id, step))

    def intent(
        self, case_id: str, run_id: str, step: str, input_hash: str, *, side_effect: bool, at: datetime
    ) -> JournalEntry:
        prev = self.items.get((case_id, run_id, step))
        entry = JournalEntry(
            case_id=case_id,
            run_id=run_id,
            step=step,
            status=JournalStatus.INTENT,
            input_hash=input_hash,
            side_effect=side_effect,
            at=at,
            attempt=(prev.attempt + 1) if prev else 1,
        )
        self.items[(case_id, run_id, step)] = entry
        self.log.append(entry)
        return entry

    def done(
        self, case_id: str, run_id: str, step: str, output_hash: str, payload: dict[str, Any], at: datetime
    ) -> JournalEntry:
        prev = self.items[(case_id, run_id, step)]
        entry = prev.model_copy(
            update={"status": JournalStatus.DONE, "output_hash": output_hash, "payload": payload, "at": at}
        )
        self.items[(case_id, run_id, step)] = entry
        self.log.append(entry)
        return entry

    def fail(self, case_id: str, run_id: str, step: str, error: str, at: datetime) -> JournalEntry:
        prev = self.items[(case_id, run_id, step)]
        entry = prev.model_copy(update={"status": JournalStatus.FAILED, "error": error, "at": at})
        self.items[(case_id, run_id, step)] = entry
        self.log.append(entry)
        return entry

    def entries(self, case_id: str, run_id: str | None = None) -> list[JournalEntry]:
        return [
            e for e in self.items.values() if e.case_id == case_id and (run_id is None or e.run_id == run_id)
        ]


class ScriptedGate:
    def __init__(
        self, responses: list[GateResponse] | Callable[[GateRequest], GateResponse] | None = None
    ) -> None:
        self.responses = responses
        self.requests: list[GateRequest] = []

    def ask(self, request: GateRequest) -> GateResponse:
        self.requests.append(request)
        if callable(self.responses):
            return self.responses(request)
        if self.responses:
            return self.responses.pop(0)
        return GateResponse(choice=request.default_choice)


def make_context(**overrides: Any) -> AppContext:
    clock = overrides.pop("clock", None) or FixedClock()
    defaults: dict[str, Any] = {
        "llm": ScriptedLLM(),
        "decision": ScriptedDecision(),
        "embeddings": HashEmbeddings(),
        "nli": KeywordNLI(),
        "cases": InMemoryCaseRepository(),
        "ledger": InMemoryLedger(),
        "materials": InMemoryMaterialStore(clock),
        "knowledge": InMemoryKnowledgeIndex(),
        "parsers": [TextParser()],
        "tables": SimpleTableEngine(),
        "web": FakeWeb(clock=clock),
        "prompts": DictPrompts(),
        "costs": InMemoryCostLedger(),
        "journal": InMemoryJournal(),
        "gate": ScriptedGate(),
        "events": ListEventBus(),
        "clock": clock,
        "settings": {},
    }
    defaults.update(overrides)
    return AppContext(**defaults)
