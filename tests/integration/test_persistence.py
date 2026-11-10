"""Интеграционные тесты SQLite-адаптеров и хранилища материалов на временных каталогах."""

from __future__ import annotations

import stat
from datetime import UTC, datetime

import pytest

from factories import empty_case, grounded_fact
from fakes import FixedClock, make_context
from strategist.application.errors import IntegrityError
from strategist.application.ports import CostRecord, HumanTimeRecord, JournalStatus, LedgerEntry
from strategist.application.use_cases.integrity import ensure_integrity
from strategist.domain.entities import Stage
from strategist.infrastructure.persistence.costs import SqliteCostLedger
from strategist.infrastructure.persistence.db import CaseDatabase
from strategist.infrastructure.persistence.materials import FileMaterialStore
from strategist.infrastructure.persistence.repositories import (
    SqliteCaseRepository,
    SqliteJournal,
    SqliteLedger,
    SqliteResponseCache,
)
from strategist.infrastructure.pricing import Pricing

NOW = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    return CaseDatabase(tmp_path / "cases")


def test_case_versions(db):
    repo = SqliteCaseRepository(db)
    case = empty_case("c1")
    repo.create(case)
    assert repo.exists("c1") and case.version == 1
    grounded_fact(case)
    case.stage = Stage.CLAIMS
    assert repo.save(case, "claims") == 2
    assert len(repo.load("c1").claims) == 1
    assert repo.load("c1", version=1).claims == []
    assert [v.message for v in repo.versions("c1")] == ["create", "claims"]
    assert repo.list_cases()[0].stage == "claims"
    with pytest.raises(ValueError):
        repo.create(empty_case("c1"))


def test_ledger_history(db):
    SqliteCaseRepository(db).create(empty_case("c1"))
    ledger = SqliteLedger(db)
    ledger.append(
        [
            LedgerEntry(case_id="c1", claim_id="CL-001", action="created", at=NOW, snapshot={"text": "a"}),
            LedgerEntry(case_id="c1", claim_id="CL-001", action="verified", at=NOW),
            LedgerEntry(case_id="c1", claim_id="CL-002", action="created", at=NOW),
        ]
    )
    assert [e.action for e in ledger.history("c1", "CL-001")] == ["created", "verified"]
    assert len(ledger.history("c1")) == 3


def test_journal_lifecycle(db):
    journal = SqliteJournal(db)
    journal.intent("c1", "run-1", "frame", "h1", side_effect=False, at=NOW)
    assert journal.get("c1", "run-1", "frame").status == JournalStatus.INTENT
    journal.done("c1", "run-1", "frame", "o1", {"x": 1}, NOW)
    entry = journal.get("c1", "run-1", "frame")
    assert entry.status == JournalStatus.DONE and entry.payload == {"x": 1}
    journal.intent("c1", "run-1", "frame", "h2", side_effect=True, at=NOW)
    journal.fail("c1", "run-1", "frame", "boom", NOW)
    entry = journal.get("c1", "run-1", "frame")
    assert entry.status == JournalStatus.FAILED and entry.attempt == 2 and entry.side_effect


def test_cost_ledger_summary(db):
    pricing = Pricing(
        {
            "currency_rate_rub_per_usd": 100,
            "human_rate_rub_per_hour": 1200,
            "subscription": {"usd_per_month": 20, "expected_runs_per_month": 40},
            "models": {"gpt-oss:120b-cloud": {"input": 1.0, "output": 2.0, "estimated": True}},
        }
    )
    costs = SqliteCostLedger(db, pricing)
    costs.record(
        CostRecord(
            case_id="c1",
            step="frame",
            task="frame",
            model="gpt-oss:120b-cloud",
            input_tokens=1_000_000,
            output_tokens=500_000,
            at=NOW,
        )
    )
    costs.record_human(HumanTimeRecord(case_id="c1", minutes=30, at=NOW))
    s = costs.summary("c1")
    assert s.usd == pytest.approx(2.0)
    assert s.rub == pytest.approx(200.0)
    assert s.human_rub == pytest.approx(600.0)
    assert s.total_rub == pytest.approx(800.0)
    assert s.subscription_share_usd == pytest.approx(0.5)
    assert s.by_step["frame"]["calls"] == 1
    rec = costs.records("c1")[0]
    assert rec.attributes["gen_ai.request.model"] == "gpt-oss:120b-cloud"
    assert rec.attributes["gen_ai.usage.input_tokens"] == 1_000_000


def test_material_store_copies_hashes_and_reads_meta(tmp_path):
    src_dir = tmp_path / "incoming"
    src_dir.mkdir()
    md = src_dir / "market.md"
    md.write_text(
        "---\ntitle: Рынок подписок\nkind: web_excerpt\nsynthetic: false\nsource_url: https://example.org\n"
        "retrieved: 2026-11-04\n---\nТекст выдержки\n",
        encoding="utf-8",
    )
    csv = src_dir / "cohorts.csv"
    csv.write_text("month,m3\n2026-01,0.41\n", encoding="utf-8")
    (src_dir / "cohorts.csv.meta.yaml").write_text("title: Когорты\nsynthetic: true\n", encoding="utf-8")
    store = FileMaterialStore(tmp_path / "cases", FixedClock())
    a = store.add_file("c1", str(md))
    b = store.add_file("c1", str(csv))
    assert a.title == "Рынок подписок" and a.kind == "web_excerpt" and a.source_url == "https://example.org"
    assert str(a.retrieved) == "2026-11-04"
    assert b.kind == "table" and b.synthetic and b.title == "Когорты"
    copied = tmp_path / "cases" / "c1" / "materials" / "cohorts.csv"
    assert copied.exists() and not copied.stat().st_mode & stat.S_IWUSR
    assert (copied.parent / "cohorts.csv.meta.yaml").exists()
    assert store.add_file("c1", str(csv)).sha256 == b.sha256  # повтор — без дублей
    assert len(store.list("c1")) == 2
    csv.write_text("month,m3\n2026-01,0.99\n", encoding="utf-8")  # то же имя, другое содержимое
    v2 = store.add_file("c1", str(csv))
    assert v2.path == "cohorts-v2.csv"
    assert store.verify("c1") == []


def test_tampered_material_stops_work(tmp_path):
    src = tmp_path / "note.md"
    src.write_text("Исходный текст", encoding="utf-8")
    store = FileMaterialStore(tmp_path / "cases", FixedClock())
    store.add_file("c1", str(src))
    copied = tmp_path / "cases" / "c1" / "materials" / "note.md"
    copied.chmod(0o644)
    copied.write_text("Подменённый текст", encoding="utf-8")
    problems = store.verify("c1")
    assert problems and "SHA-256" in problems[0]
    ctx = make_context(materials=store)
    with pytest.raises(IntegrityError, match=r"note\.md"):
        ensure_integrity(ctx, "c1")
    copied.unlink()
    assert "отсутствует" in store.verify("c1")[0]


def test_response_cache(db):
    cache = SqliteResponseCache(db)
    assert cache.get("c1", "k") is None
    cache.put("c1", "k", "m", {"text": "ok"}, NOW)
    assert cache.get("c1", "k") == {"text": "ok"}
