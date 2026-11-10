"""SqliteKnowledgeIndex на временной БД и use case памяти: FTS5, sqlite-vec, RRF, связи, отзыв/одобрение."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from fakes import FixedClock, HashEmbeddings, make_context
from strategist.application.ports import KnowledgeDoc, MemoryRecord, MemoryStatus
from strategist.application.use_cases.memory import (
    approve_memory,
    correct_memory,
    recall,
    remember,
    retract_memory,
    usable_as_evidence,
)
from strategist.domain.entities import Origin
from strategist.infrastructure.knowledge.index import (
    SqliteKnowledgeIndex,
    fts_query,
    load_methodology_docs,
    reciprocal_rank_fusion,
)

DOCS = [
    KnowledgeDoc(
        id="m-retention",
        title="Удержание клиентов",
        text="Удержание клиентов измеряется по когортам: доля активных на третий месяц.",
        kind="methodology",
    ),
    KnowledgeDoc(
        id="c1-tariffs",
        title="Семейный тариф",
        text="Семейный тариф с безлимитным интернетом для четырёх участников.",
        kind="material",
        case_id="c1",
    ),
    KnowledgeDoc(
        id="c2-churn",
        title="Отток",
        text="Отток абонентов снизился на 2 п.п.; ёлочные акции не повлияли.",
        kind="material",
        case_id="c2",
    ),
    KnowledgeDoc(
        id="m-pricing",
        title="Ценообразование",
        text="Ценовая лестница и якорение цены при выводе тарифа.",
        kind="methodology",
    ),
]


class CountingEmbeddings(HashEmbeddings):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return super().embed(texts)


class FailingEmbeddings:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise ConnectionError("модель эмбеддингов недоступна")


@pytest.fixture
def clock():
    return FixedClock()


@pytest.fixture
def index(tmp_path, clock):
    idx = SqliteKnowledgeIndex(tmp_path / "knowledge.db", HashEmbeddings(), clock)
    idx.upsert(DOCS)
    return idx


def ids(hits):
    return [h.doc_id for h in hits]


# --- Поиск ---------------------------------------------------------------------------------------


def test_fts_russian_word_forms_and_yo(index):
    hits = index.search("удержания клиента", k=3)
    assert hits[0].doc_id == "m-retention"
    assert hits[0].ranks["fts"] == 1
    assert "Удержание" in hits[0].snippet
    assert index.search("ЕЛОЧНЫЕ", k=1)[0].doc_id == "c2-churn"  # регистр и ё→е
    assert index.search("Отток", k=1)[0].ranks.get("fts") == 1


def test_fts_query_is_safe(index):
    hostile = 'тариф" OR 1=1; DROP TABLE docs --'
    assert fts_query(hostile) == '"тари"* OR "1"* OR "drop"* OR "tabl"* OR "docs"*'
    assert index.search(hostile)[0].doc_id == "c1-tariffs"  # синтаксис FTS5 из запроса не исполняется
    assert fts_query("и в на") == ""


def test_fts_only_without_embeddings(tmp_path, clock):
    idx = SqliteKnowledgeIndex(tmp_path / "fts.db", None, clock)
    assert not idx.vector_enabled
    idx.upsert(DOCS)
    hits = idx.search("семейный тариф")
    assert hits[0].doc_id == "c1-tariffs"
    assert all(set(h.ranks) <= {"fts", "graph"} for h in hits)


def test_embeddings_failure_falls_back_to_fts(tmp_path, clock):
    idx = SqliteKnowledgeIndex(tmp_path / "fail.db", FailingEmbeddings(), clock)
    idx.upsert(DOCS)  # не падает
    hits = idx.search("ценовая лестница")
    assert hits[0].doc_id == "m-pricing" and "vector" not in hits[0].ranks


def test_vector_search_finds_meaning_without_word_match(index):
    # «безлимитность» не совпадает со словами текста ни целиком, ни по префиксу — FTS молчит,
    # но триграммные «эмбеддинги» близки к «безлимитным»
    assert index.search("безлимитность", kinds=["material"])
    hit = index.search("безлимитность", k=1)[0]
    assert hit.doc_id == "c1-tariffs"
    assert "fts" not in hit.ranks and hit.ranks["vector"] == 1


def test_rrf_combines_ranks():
    fused = reciprocal_rank_fusion({"fts": ["a", "b", "c"], "vector": ["c", "a"]})
    assert [d for d, _, _ in fused] == ["a", "c", "b"]
    doc, score, ranks = fused[0]
    assert score == pytest.approx(1 / 61 + 1 / 62)
    assert ranks == {"fts": 1, "vector": 2}
    assert fused[2][2] == {"fts": 2}


def test_search_scores_are_rrf(index):
    hits = index.search("отток абонентов")
    for hit in hits:
        assert hit.score == pytest.approx(sum(1 / (60 + r) for r in hit.ranks.values()))
    assert hits == sorted(hits, key=lambda h: -h.score)


def test_filters_kinds_and_case(index):
    assert set(ids(index.search("тариф", kinds=["methodology"]))) <= {"m-retention", "m-pricing"}
    in_c1 = ids(index.search("отток тариф", k=10, case_id="c1"))
    assert "c2-churn" not in in_c1  # чужой кейс не виден
    assert "c1-tariffs" in in_c1 and "m-pricing" in in_c1  # общая методика видна
    assert index.search("   ") == []


def test_links_neighbors_and_graph_rank(index):
    index.link("m-retention", "m-pricing", "mentions")
    index.link("m-retention", "c2-churn", "supports")
    index.link("m-retention", "m-pricing", "mentions")  # повтор не дублируется
    assert index.neighbors("m-retention") == ["m-pricing", "c2-churn"]
    assert index.neighbors("m-retention", "supports") == ["c2-churn"]
    assert index.neighbors("m-pricing") == []
    hits = {h.doc_id: h for h in index.search("удержание когорты", k=4)}
    assert hits["m-pricing"].ranks.get("graph") is not None  # сосед лучшего кандидата


def test_upsert_updates_in_place_and_reembeds_only_changed(tmp_path, clock):
    emb = CountingEmbeddings()
    idx = SqliteKnowledgeIndex(tmp_path / "k.db", emb, clock)
    idx.upsert(DOCS)
    assert len(emb.texts) == len(DOCS)
    idx.upsert(DOCS)  # тексты не менялись — эмбеддинги не пересчитываются
    assert len(emb.texts) == len(DOCS)
    changed = DOCS[0].model_copy(update={"text": "Удержание: новая формулировка про возвращаемость."})
    idx.upsert([changed])
    assert len(emb.texts) == len(DOCS) + 1
    assert idx.get("m-retention").text.startswith("Удержание: новая")
    assert idx.search("возвращаемость", k=1)[0].doc_id == "m-retention"
    assert "fts" not in {h.doc_id: h for h in idx.search("когортам", k=10)}["m-retention"].ranks


def test_index_persists_between_instances(tmp_path, clock):
    path = tmp_path / "persist.db"
    SqliteKnowledgeIndex(path, HashEmbeddings(), clock).upsert(DOCS)
    again = SqliteKnowledgeIndex(path, HashEmbeddings(), clock)
    assert again.search("ценообразование", k=1)[0].doc_id == "m-pricing"


# --- Методика и пересборка -----------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    knowledge = tmp_path / "knowledge"
    (knowledge / "methodology" / "frameworks").mkdir(parents=True)
    (knowledge / "methodology" / "jtbd.md").write_text(
        "---\ntitle: Jobs To Be Done\nversion: 2\n---\n# JTBD\n\nРабота клиента; см. [[pricing]] и [[нет-такой]].\n",
        encoding="utf-8",
    )
    (knowledge / "methodology" / "frameworks" / "pricing.md").write_text(
        "# Ценообразование\n\nЦеновая лестница. Связано с [[Jobs To Be Done]].\n", encoding="utf-8"
    )
    skill = tmp_path / ".agents" / "skills" / "playbook-retention"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: playbook-retention\ndescription: Плейбук удержания абонентов\n---\n"
        "# Удержание\n\nШаги: когорты, [[frameworks/pricing]], [[jtbd.md]].\n",
        encoding="utf-8",
    )
    return tmp_path


def test_load_methodology_docs(project):
    docs = {d.id: d for d in load_methodology_docs(project / "knowledge", project / ".agents" / "skills")}
    assert set(docs) == {
        "knowledge/methodology/jtbd.md",
        "knowledge/methodology/frameworks/pricing.md",
        ".agents/skills/playbook-retention/SKILL.md",
    }
    jtbd = docs["knowledge/methodology/jtbd.md"]
    assert jtbd.kind == "methodology" and jtbd.title == "Jobs To Be Done" and jtbd.case_id is None
    assert "title:" not in jtbd.text and jtbd.meta["version"] == 2
    assert jtbd.meta["mentions"] == ["knowledge/methodology/frameworks/pricing.md"]
    assert jtbd.meta["unresolved_mentions"] == ["нет-такой"]
    skill = docs[".agents/skills/playbook-retention/SKILL.md"]
    assert skill.title == "playbook-retention" and skill.text.startswith("Плейбук удержания абонентов")
    assert skill.meta["mentions"] == [
        "knowledge/methodology/frameworks/pricing.md",
        "knowledge/methodology/jtbd.md",
    ]
    assert docs["knowledge/methodology/frameworks/pricing.md"].meta["mentions"] == [
        "knowledge/methodology/jtbd.md"
    ]


def test_rebuild_from_files(project, tmp_path, clock):
    emb = CountingEmbeddings()
    idx = SqliteKnowledgeIndex(tmp_path / "rebuild.db", emb, clock)
    idx.upsert([DOCS[1]])  # материал кейса
    mem = MemoryRecord(
        id="MEM-001",
        text="Руководитель предпочитает семейные тарифы",
        origin=Origin.USER_STATED,
        recorded_at=clock.now(),
    )
    idx.remember(mem)
    docs = load_methodology_docs(project / "knowledge", project / ".agents" / "skills")
    idx.rebuild(docs, kinds=["methodology"])
    assert idx.neighbors("knowledge/methodology/jtbd.md", "mentions") == [
        "knowledge/methodology/frameworks/pricing.md"
    ]
    assert idx.search("плейбук удержания", k=1)[0].doc_id == ".agents/skills/playbook-retention/SKILL.md"
    assert idx.get("c1-tariffs") is not None  # kinds=["methodology"] не трогает материалы

    embedded = len(emb.texts)
    (project / "knowledge" / "methodology" / "jtbd.md").unlink()
    idx.rebuild(load_methodology_docs(project / "knowledge", project / ".agents" / "skills"))
    assert len(emb.texts) == embedded  # неизменённые тексты — векторы из кеша, без пересчёта
    assert idx.get("knowledge/methodology/jtbd.md") is None
    assert idx.get("c1-tariffs") is None  # полная пересборка
    assert idx.neighbors("knowledge/methodology/jtbd.md", "mentions") == []
    assert idx.memories() == [mem]  # память не удаляется никогда
    assert idx.search("семейные тарифы", kinds=["memory"])[0].doc_id == "MEM-001"
    assert idx.search("лестница", k=1)[0].ranks.get("vector") is not None


# --- Память (адаптер) ----------------------------------------------------------------------------


def record(
    clock, n: int, origin=Origin.USER_STATED, case_id="c1", text="Бюджет пилота — 3 млн ₽"
) -> MemoryRecord:
    return MemoryRecord(id=f"MEM-{n:03d}", case_id=case_id, text=text, origin=origin, recorded_at=clock.now())


def test_memory_lifecycle_in_adapter(index, clock):
    index.remember(record(clock, 1))
    index.remember(record(clock, 2, Origin.AGENT_INFERENCE, text="Вероятно, отток вызван ценой"))
    index.remember(record(clock, 3, case_id="c2"))
    with pytest.raises(ValueError, match="уже есть"):
        index.remember(record(clock, 1))
    assert [m.id for m in index.memories("c1")] == ["MEM-001", "MEM-002"]
    assert len(index.memories()) == 3

    clock.advance(hours=1)
    index.retract("MEM-001", "бюджет пересмотрен", clock.now())
    assert [m.id for m in index.memories("c1")] == ["MEM-002"]
    retracted = next(m for m in index.memories("c1", include_inactive=True) if m.id == "MEM-001")
    assert retracted.status == MemoryStatus.RETRACTED and retracted.reason == "бюджет пересмотрен"

    index.correct("MEM-002", record(clock, 4, Origin.AGENT_INFERENCE, text="Отток вызван качеством сети"))
    by_id = {m.id: m for m in index.memories(include_inactive=True)}
    assert by_id["MEM-002"].status == MemoryStatus.CORRECTED and by_id["MEM-002"].superseded_by == "MEM-004"
    assert by_id["MEM-004"].status == MemoryStatus.ACTIVE
    with pytest.raises(ValueError, match="MEM-004"):
        index.correct("MEM-002", record(clock, 5))

    index.approve("MEM-004", clock.now())
    approved = next(m for m in index.memories("c1") if m.id == "MEM-004")
    assert approved.approved_by_user and approved.approved_at == clock.now()
    with pytest.raises(ValueError, match="не активна"):
        index.approve("MEM-001", clock.now())
    with pytest.raises(ValueError, match="Нет записи"):
        index.retract("MEM-999", "x", clock.now())

    assert len(index.memories(include_inactive=True)) == 4  # ничего не удалено физически
    assert [h["action"] for h in index.memory_history("MEM-002")] == ["recorded", "corrected"]
    assert [h["action"] for h in index.memory_history("MEM-004")] == ["recorded", "approved"]


# --- Use case памяти -----------------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path, clock):
    knowledge = SqliteKnowledgeIndex(tmp_path / "ctx.db", HashEmbeddings(), clock)
    return make_context(clock=clock, knowledge=knowledge)


def test_remember_ids_and_status(ctx):
    a = remember(ctx, "c1", "Руководитель хочет запуск до марта", Origin.USER_STATED)
    b = remember(ctx, "c1", "Похоже, сегмент семей недооценён", Origin.AGENT_INFERENCE)
    assert (a.id, b.id) == ("MEM-001", "MEM-002")
    assert a.epistemic_status == "verified" and b.epistemic_status == "unverified"
    assert a.recorded_at == ctx.clock.now()
    assert len(ctx.events.of("memory.recorded")) == 2
    with pytest.raises(ValueError):
        remember(ctx, "c1", "  ", Origin.USER_STATED)


def test_retracted_record_is_not_evidence(ctx):
    rec = remember(ctx, "c1", "Бюджет пилота — 3 млн ₽", Origin.USER_STATED)
    assert [m.id for m in usable_as_evidence(ctx, "c1")] == [rec.id]
    retract_memory(ctx, rec.id, "бюджет урезан")
    assert usable_as_evidence(ctx, "c1") == []
    found = [i for i in recall(ctx, "бюджет пилота", case_id="c1") if i.hit.doc_id == rec.id]
    assert found and found[0].evidence_blocked and "отозвана: бюджет урезан" in found[0].reason
    assert ctx.events.of("memory.retracted")[0].data["memory_id"] == rec.id


def test_agent_inference_needs_user_approval(ctx):
    rec = remember(ctx, "c1", "Отток вызван ценой, а не сетью", Origin.AGENT_INFERENCE)
    assert usable_as_evidence(ctx, "c1") == []
    item = next(i for i in recall(ctx, "отток вызван ценой", case_id="c1") if i.hit.doc_id == rec.id)
    assert item.evidence_blocked and "без одобрения" in item.reason

    ctx.clock.advance(minutes=5)
    approve_memory(ctx, rec.id)
    usable = usable_as_evidence(ctx, "c1")
    assert [m.id for m in usable] == [rec.id]
    assert usable[0].approved_by_user and usable[0].approved_at == ctx.clock.now()
    events = ctx.events.of("memory.approved")
    assert len(events) == 1 and events[0].data["memory_id"] == rec.id and events[0].case_id == "c1"
    item = next(i for i in recall(ctx, "отток вызван ценой", case_id="c1") if i.hit.doc_id == rec.id)
    assert not item.evidence_blocked and item.memory is not None


def test_correction_replaces_record(ctx):
    old = remember(ctx, "c1", "Запуск в марте", Origin.USER_STATED, valid_from=date(2026, 1, 1))
    new = correct_memory(ctx, old.id, "Запуск перенесён на апрель")
    assert new.id == "MEM-002" and new.valid_from == date(2026, 1, 1)
    assert [m.id for m in usable_as_evidence(ctx, "c1")] == [new.id]
    stale = next(i for i in recall(ctx, "запуск", case_id="c1") if i.hit.doc_id == old.id)
    assert stale.evidence_blocked and "MEM-002" in stale.reason
    with pytest.raises(ValueError):
        correct_memory(ctx, old.id, "ещё раз")
    with pytest.raises(ValueError):
        approve_memory(ctx, old.id)
    assert ctx.events.of("memory.corrected")[0].data == {"memory_id": old.id, "superseded_by": new.id}


def test_corrected_agent_inference_needs_new_approval(ctx):
    rec = remember(ctx, "c1", "Сегмент семей — 18%", Origin.AGENT_INFERENCE)
    approve_memory(ctx, rec.id)
    fixed = correct_memory(ctx, rec.id, "Сегмент семей — 21%")
    assert fixed.origin == Origin.AGENT_INFERENCE
    assert usable_as_evidence(ctx, "c1") == []
    by_user = correct_memory(ctx, fixed.id, "Сегмент семей — 20% (данные отдела)", origin=Origin.USER_STATED)
    assert [m.id for m in usable_as_evidence(ctx, "c1")] == [by_user.id]


def test_validity_window(ctx):
    today = ctx.clock.today()
    remember(
        ctx,
        "c1",
        "Акция действовала в прошлом году",
        Origin.USER_STATED,
        valid_until=today - timedelta(days=1),
    )
    remember(
        ctx, "c1", "Новый тариф с будущего месяца", Origin.USER_STATED, valid_from=today + timedelta(days=30)
    )
    current = remember(ctx, "c1", "Текущий ARPU — 450 ₽", Origin.SOURCE_DOCUMENT)
    assert [m.id for m in usable_as_evidence(ctx, "c1")] == [current.id]
    with pytest.raises(ValueError):
        remember(ctx, "c1", "x", Origin.USER_STATED, valid_from=today, valid_until=today - timedelta(days=1))


def test_recall_marks_only_memory_and_respects_case(ctx):
    ctx.knowledge.upsert(DOCS)
    remember(ctx, "c2", "В кейсе c2 отток считали иначе", Origin.USER_STATED)
    items = recall(ctx, "отток абонентов", case_id="c1")
    assert all(i.hit.doc_id not in ("c2-churn", "MEM-001") for i in items)  # чужой кейс не виден
    assert items and all(not i.evidence_blocked for i in items if i.hit.kind != "memory")
