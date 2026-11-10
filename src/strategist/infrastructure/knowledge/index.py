"""База знаний в одном SQLite: FTS5 (формулировки) + sqlite-vec (смысл) + таблица связей (граф) + память.

Ранги трёх списков сводятся Reciprocal Rank Fusion (k=60): score = Σ 1/(60 + место). Граф — третий
список: соседи лучших кандидатов первых двух. Источник правды для методики и материалов — файлы,
индекс пересобирается `rebuild()`. Память хранится с происхождением и историей изменений; ничего не
удаляется физически: отзыв и исправление — смена статуса и запись в журнал memory_log.

Если эмбеддингов нет или sqlite-vec не загрузился — работает только FTS (без падения).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from strategist.application.ports import (
    Clock,
    EmbeddingPort,
    KnowledgeDoc,
    MemoryRecord,
    MemoryStatus,
    SearchHit,
)
from strategist.infrastructure.tools.parsers import read_text_file, split_front_matter

try:  # расширение необязательно: без него остаётся полнотекстовый поиск
    import sqlite_vec
except ImportError:  # pragma: no cover - зависимость объявлена в pyproject
    sqlite_vec = None

log = logging.getLogger(__name__)

RRF_K = 60
MENTIONS = "mentions"
MEMORY_KIND = "memory"
EMBED_CHARS = 2000
EMBED_BATCH = 32
GRAPH_SEEDS = 5
MAX_KNN = 4096
SNIPPET_CHARS = 240

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS docs (
    rid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    case_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    sha256 TEXT NOT NULL,
    embedded_sha256 TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS docs_kind_case ON docs(kind, case_id);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, text, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    relation TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (src, dst, relation)
);
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
CREATE TABLE IF NOT EXISTS memories (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    case_id TEXT,
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_case ON memories(case_id, status);
CREATE TABLE IF NOT EXISTS memory_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    action TEXT NOT NULL,
    at TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
"""

_WORD = re.compile(r"[^\W_]+")
_STOPWORDS_TEXT = (
    "и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было "
    "вот от меня еще нет о из ему когда даже ну ли если уже или ни быть был него до вас там потом себя "
    "ей может они тут где есть надо ней для мы тебя их чем была сам без чего раз тоже себе под будет "
    "ж тогда кто этот того потому этого какой ним здесь этом один мой тем чтобы нее сейчас были куда "
    "зачем всех можно при об другой после над больше тот через эти нас про всего них какая много эту "
    "моя свою этой перед том такой им более всегда всю между это "
    "the a an of and or to in is are for on with by at from as be it this that"
)
_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


def fold(text: str) -> str:
    """ё→е (unicode61 снимает диакритику только у латиницы). Замена 1:1 — позиции символов сохраняются."""
    return text.replace("ё", "е").replace("Ё", "Е")


def query_terms(query: str) -> list[str]:
    """Слова запроса без стоп-слов; длинные — с грубым «стеммингом» (префикс без окончания)."""
    terms: list[str] = []
    for word in _WORD.findall(fold(query.lower())):
        if word in _STOPWORDS or (len(word) < 2 and not word.isdigit()):
            continue
        term = word[: max(4, len(word) - 2)] if len(word) >= 5 else word
        if term not in terms:
            terms.append(term)
    return terms


def fts_query(query: str) -> str:
    """Свободный текст → безопасный запрос FTS5: каждое слово в кавычках, префиксный поиск, через OR."""
    return " OR ".join(f'"{t}"*' for t in query_terms(query))


def reciprocal_rank_fusion(
    ranked: dict[str, list[str]], k: int = RRF_K
) -> list[tuple[str, float, dict[str, int]]]:
    """RRF: score(d) = Σ_списков 1/(k + место d в списке). Возвращает (id, score, места) по убыванию."""
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for name, ids in ranked.items():
        for position, doc_id in enumerate(ids, start=1):
            if name in ranks.get(doc_id, {}):
                continue
            ranks.setdefault(doc_id, {})[name] = position
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + position)
    order = sorted(scores, key=lambda d: (-scores[d], min(ranks[d].values()), d))
    return [(d, scores[d], ranks[d]) for d in order]


def _doc_hash(doc: KnowledgeDoc) -> str:
    return hashlib.sha256(f"{doc.title}\x00{doc.text}".encode()).hexdigest()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str, sort_keys=True)


def memory_doc(record: MemoryRecord) -> KnowledgeDoc:
    """Запись памяти как документ базы знаний: ищется наравне с методикой, но с пометкой происхождения."""
    return KnowledgeDoc(
        id=record.id,
        text=record.text,
        title=f"Память {record.id}",
        kind=MEMORY_KIND,
        case_id=record.case_id,
        meta={
            "origin": record.origin.value,
            "status": record.status.value,
            "approved_by_user": record.approved_by_user,
            "epistemic_status": record.epistemic_status,
        },
    )


class SqliteKnowledgeIndex:
    """KnowledgeIndex в одном файле SQLite (WAL, соединение на операцию)."""

    def __init__(
        self,
        db_path: Path,
        embeddings: EmbeddingPort | None,
        clock: Clock,
        *,
        max_vector_distance: float | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.embeddings = embeddings
        self.clock = clock
        # Порог косинусного расстояния для «смыслового» списка (None — без отсечения: решает RRF).
        self.max_vector_distance = max_vector_distance
        self._lock = threading.RLock()
        self._initialized = False
        self._vec_failed = embeddings is None or sqlite_vec is None

    # соединение -------------------------------------------------------------------------------

    @property
    def vector_enabled(self) -> bool:
        return not self._vec_failed

    def _load_vec(self, conn: sqlite3.Connection) -> bool:
        if self._vec_failed:
            return False
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            return True
        except Exception as exc:  # нет поддержки расширений в сборке Python, битый бинарник и т. п.
            log.warning("sqlite-vec не загружен (%s): поиск только по FTS", exc)
            self._vec_failed = True
            return False

    @contextmanager
    def _connect(self) -> Iterator[tuple[sqlite3.Connection, bool]]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            vec = self._load_vec(conn)
            if not self._initialized:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                self._initialized = True
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn, vec
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[tuple[sqlite3.Connection, bool]]:
        with self._lock, self._connect() as (conn, vec):
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn, vec
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _kv(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _vec_table_ready(self, conn: sqlite3.Connection, vec: bool) -> bool:
        return vec and self._kv(conn, "vec_dims") is not None

    def _ensure_vec(self, conn: sqlite3.Connection, dims: int) -> None:
        current = self._kv(conn, "vec_dims")
        if current is not None and int(current) == dims:
            return
        if current is not None:  # модель эмбеддингов сменилась — старые векторы несопоставимы
            log.warning("Размерность эмбеддингов %s → %s: векторный индекс пересоздаётся", current, dims)
            conn.execute("DROP TABLE IF EXISTS docs_vec")
            conn.execute("UPDATE docs SET embedded_sha256 = NULL")
        conn.execute(
            f"CREATE VIRTUAL TABLE docs_vec USING vec0(embedding float[{dims}] distance_metric=cosine)"
        )
        conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('vec_dims', ?)", (str(dims),))

    # эмбеддинги -------------------------------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        if self.embeddings is None or self._vec_failed or not texts:
            return None
        vectors: list[list[float]] = []
        try:
            for i in range(0, len(texts), EMBED_BATCH):
                vectors.extend(self.embeddings.embed(texts[i : i + EMBED_BATCH]))
        except Exception as exc:  # модель недоступна — индекс работает без смысла, а не падает
            log.warning("Эмбеддинги недоступны (%s): документы проиндексированы только для FTS", exc)
            return None
        if len(vectors) != len(texts) or any(not v for v in vectors):
            return None
        return vectors

    # документы --------------------------------------------------------------------------------

    def upsert(self, docs: list[KnowledgeDoc]) -> None:
        self._index(docs, {})

    def _index(self, docs: list[KnowledgeDoc], cached: dict[str, bytes]) -> None:
        if not docs:
            return
        unique = list({d.id: d for d in docs}.values())
        hashes = {d.id: _doc_hash(d) for d in unique}
        with self._connect() as (conn, vec):
            existing = {
                row["id"]: row["embedded_sha256"]
                for row in conn.execute(
                    f"SELECT id, embedded_sha256 FROM docs WHERE id IN ({', '.join('?' for _ in unique)})",
                    [d.id for d in unique],
                )
            }
        to_embed = [d for d in unique if existing.get(d.id) != hashes[d.id] and hashes[d.id] not in cached]
        vectors: dict[str, bytes] = {}
        dims: int | None = None
        if vec and to_embed:
            embedded = self._embed([f"{d.title}\n{d.text}"[:EMBED_CHARS] for d in to_embed])
            if embedded is not None:
                dims = len(embedded[0])
                vectors = {
                    d.id: sqlite_vec.serialize_float32(v)
                    for d, v in zip(to_embed, embedded, strict=True)
                    if len(v) == dims
                }
        now = self.clock.now().isoformat()
        with self._tx() as (conn, vec):
            if vec and dims is not None:
                self._ensure_vec(conn, dims)
            vec_ready = self._vec_table_ready(conn, vec)
            for doc in unique:
                sha = hashes[doc.id]
                row = conn.execute("SELECT rid, embedded_sha256 FROM docs WHERE id = ?", (doc.id,)).fetchone()
                if row is None:
                    cur = conn.execute(
                        "INSERT INTO docs(id, kind, case_id, title, text, meta, sha256, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (doc.id, doc.kind, doc.case_id, doc.title, doc.text, _json(doc.meta), sha, now),
                    )
                    rid, embedded_sha = cur.lastrowid, None
                else:
                    rid, embedded_sha = row["rid"], row["embedded_sha256"]
                    conn.execute(
                        "UPDATE docs SET kind = ?, case_id = ?, title = ?, text = ?, meta = ?, sha256 = ?, "
                        "updated_at = ? WHERE rid = ?",
                        (doc.kind, doc.case_id, doc.title, doc.text, _json(doc.meta), sha, now, rid),
                    )
                    conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (rid,))
                conn.execute(
                    "INSERT INTO docs_fts(rowid, title, text) VALUES (?, ?, ?)",
                    (rid, fold(doc.title), fold(doc.text)),
                )
                if vec_ready:
                    blob = vectors.get(doc.id) or cached.get(sha)
                    if blob is not None:
                        conn.execute("DELETE FROM docs_vec WHERE rowid = ?", (rid,))
                        conn.execute("INSERT INTO docs_vec(rowid, embedding) VALUES (?, ?)", (rid, blob))
                        conn.execute("UPDATE docs SET embedded_sha256 = ? WHERE rid = ?", (sha, rid))
                    elif embedded_sha != sha:  # текст изменился, а нового вектора нет — старый неверен
                        conn.execute("DELETE FROM docs_vec WHERE rowid = ?", (rid,))
                        conn.execute("UPDATE docs SET embedded_sha256 = NULL WHERE rid = ?", (rid,))
                if MENTIONS in doc.meta:
                    conn.execute("DELETE FROM links WHERE src = ? AND relation = ?", (doc.id, MENTIONS))
                    for dst in doc.meta.get(MENTIONS) or []:
                        conn.execute(
                            "INSERT OR IGNORE INTO links(src, dst, relation, at) VALUES (?, ?, ?, ?)",
                            (doc.id, str(dst), MENTIONS, now),
                        )

    def rebuild(self, docs: list[KnowledgeDoc], kinds: list[str] | None = None) -> None:
        """Полная пересборка индекса документов из файлов (источник правды).

        kinds=None — удаляются все документы; иначе только документы указанных видов (например,
        ["methodology"], чтобы не трогать проиндексированные материалы кейсов). Записи памяти не
        удаляются никогда: их документы восстанавливаются из таблицы memories. Векторы неизменённых
        текстов переиспользуются (по хешу), чтобы не пересчитывать эмбеддинги.
        """
        with self._tx() as (conn, vec):
            where, params = (
                ("", [])
                if kinds is None
                else (
                    f" WHERE kind IN ({', '.join('?' for _ in kinds)})",
                    list(kinds),
                )
            )
            rows = conn.execute(
                f"SELECT rid, id, kind, sha256, embedded_sha256 FROM docs{where}", params
            ).fetchall()
            cached: dict[str, bytes] = {}
            if self._vec_table_ready(conn, vec):
                for row in rows:
                    if row["embedded_sha256"] == row["sha256"]:
                        hit = conn.execute(
                            "SELECT embedding FROM docs_vec WHERE rowid = ?", (row["rid"],)
                        ).fetchone()
                        if hit is not None:
                            cached[row["sha256"]] = bytes(hit[0])
                for row in rows:
                    conn.execute("DELETE FROM docs_vec WHERE rowid = ?", (row["rid"],))
            for row in rows:
                conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (row["rid"],))
                conn.execute("DELETE FROM links WHERE src = ? AND relation = ?", (row["id"], MENTIONS))
                conn.execute("DELETE FROM docs WHERE rid = ?", (row["rid"],))
            restore_memory = kinds is None or MEMORY_KIND in kinds
        memory_docs = [memory_doc(m) for m in self.memories(include_inactive=True)] if restore_memory else []
        self._index([*docs, *memory_docs], cached)

    # поиск ------------------------------------------------------------------------------------

    @staticmethod
    def _filters(kinds: list[str] | None, case_id: str | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if kinds:
            clauses.append(f"d.kind IN ({', '.join('?' for _ in kinds)})")
            params.extend(kinds)
        if case_id is not None:  # общие документы (методика) + документы этого кейса, но не чужих
            clauses.append("(d.case_id IS NULL OR d.case_id = ?)")
            params.append(case_id)
        return ("".join(f" AND {c}" for c in clauses)), params

    def _fts_ranked(
        self, conn: sqlite3.Connection, query: str, limit: int, filt: tuple[str, list[Any]]
    ) -> list[str]:
        match = fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT d.id FROM docs_fts JOIN docs d ON d.rid = docs_fts.rowid "
            f"WHERE docs_fts MATCH ?{filt[0]} ORDER BY bm25(docs_fts, 2.0, 1.0), d.id LIMIT ?"
        )
        try:
            return [row[0] for row in conn.execute(sql, [match, *filt[1], limit])]
        except sqlite3.OperationalError as exc:
            log.warning("FTS-запрос «%s» не выполнен: %s", match, exc)
            return []

    def _vector_ranked(
        self, conn: sqlite3.Connection, vec: bool, query: str, limit: int, filt: tuple[str, list[Any]]
    ) -> list[str]:
        if not self._vec_table_ready(conn, vec):
            return []
        embedded = self._embed([query[:EMBED_CHARS]])
        if not embedded:
            return []
        dims = int(self._kv(conn, "vec_dims") or 0)
        if len(embedded[0]) != dims:
            return []
        total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        knn = min(MAX_KNN, max(limit, total if filt[1] else limit))
        sql = (
            "WITH knn AS (SELECT rowid, distance FROM docs_vec WHERE embedding MATCH ? AND k = ?) "
            f"SELECT d.id, knn.distance FROM knn JOIN docs d ON d.rid = knn.rowid WHERE 1 = 1{filt[0]} "
            "ORDER BY knn.distance, d.id LIMIT ?"
        )
        try:
            rows = conn.execute(sql, [sqlite_vec.serialize_float32(embedded[0]), knn, *filt[1], limit])
            return [
                row[0]
                for row in rows
                if self.max_vector_distance is None or row[1] <= self.max_vector_distance
            ]
        except sqlite3.OperationalError as exc:
            log.warning("Векторный поиск не выполнен: %s", exc)
            return []

    def _graph_ranked(
        self, conn: sqlite3.Connection, seeds: list[str], filt: tuple[str, list[Any]]
    ) -> list[str]:
        """Соседи лучших кандидатов (в обе стороны связи) в порядке мест самих кандидатов."""
        out: list[str] = []
        for seed in seeds:
            rows = conn.execute(
                "SELECT l.dst AS other FROM links l WHERE l.src = ? "
                "UNION SELECT l.src AS other FROM links l WHERE l.dst = ? ORDER BY other",
                (seed, seed),
            ).fetchall()
            others = [r[0] for r in rows if r[0] != seed and r[0] not in out]
            if not others:
                continue
            placeholders = ", ".join("?" for _ in others)
            present = {
                r[0]
                for r in conn.execute(
                    f"SELECT d.id FROM docs d WHERE d.id IN ({placeholders}){filt[0]}", [*others, *filt[1]]
                )
            }
            out.extend(o for o in others if o in present)
        return out

    @staticmethod
    def _snippet(text: str, terms: list[str]) -> str:
        folded = fold(text.lower())
        positions = [p for p in (folded.find(t) for t in terms) if p >= 0]
        if not positions:
            return text[:SNIPPET_CHARS]
        start = max(0, min(positions) - SNIPPET_CHARS // 3)
        prefix = "…" if start else ""
        end = start + SNIPPET_CHARS
        return prefix + text[start:end] + ("…" if end < len(text) else "")

    def search(
        self, query: str, k: int = 8, kinds: list[str] | None = None, case_id: str | None = None
    ) -> list[SearchHit]:
        if not query.strip() or k <= 0:
            return []
        pool = max(k * 4, 32)
        filt = self._filters(kinds, case_id)
        with self._connect() as (conn, vec):
            ranked: dict[str, list[str]] = {
                "fts": self._fts_ranked(conn, query, pool, filt),
                "vector": self._vector_ranked(conn, vec, query, pool, filt),
            }
            preliminary = reciprocal_rank_fusion(ranked)
            ranked["graph"] = self._graph_ranked(conn, [d for d, _, _ in preliminary[:GRAPH_SEEDS]], filt)
            fused = reciprocal_rank_fusion({name: ids for name, ids in ranked.items() if ids})[:k]
            if not fused:
                return []
            ids = [d for d, _, _ in fused]
            rows = {
                r["id"]: r
                for r in conn.execute(
                    f"SELECT id, title, kind, text FROM docs WHERE id IN ({', '.join('?' for _ in ids)})", ids
                )
            }
        terms = query_terms(query)
        return [
            SearchHit(
                doc_id=doc_id,
                title=rows[doc_id]["title"],
                kind=rows[doc_id]["kind"],
                snippet=self._snippet(rows[doc_id]["text"], terms),
                score=score,
                ranks=ranks,
            )
            for doc_id, score, ranks in fused
            if doc_id in rows
        ]

    def get(self, doc_id: str) -> KnowledgeDoc | None:
        with self._connect() as (conn, _):
            row = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            return None
        return KnowledgeDoc(
            id=row["id"],
            text=row["text"],
            title=row["title"],
            kind=row["kind"],
            case_id=row["case_id"],
            meta=json.loads(row["meta"]),
        )

    # связи ------------------------------------------------------------------------------------

    def link(self, src_id: str, dst_id: str, relation: str) -> None:
        with self._tx() as (conn, _):
            conn.execute(
                "INSERT OR IGNORE INTO links(src, dst, relation, at) VALUES (?, ?, ?, ?)",
                (src_id, dst_id, relation, self.clock.now().isoformat()),
            )

    def neighbors(self, doc_id: str, relation: str | None = None) -> list[str]:
        """Исходящие связи документа (src → dst), в порядке добавления."""
        sql = (
            "SELECT dst FROM links WHERE src = ?"
            + (" AND relation = ?" if relation else "")
            + " ORDER BY rowid"
        )
        params = (doc_id, relation) if relation else (doc_id,)
        with self._connect() as (conn, _):
            out: list[str] = []
            for row in conn.execute(sql, params):
                if row[0] not in out:
                    out.append(row[0])
            return out

    # память -----------------------------------------------------------------------------------

    def _load_memory(self, conn: sqlite3.Connection, memory_id: str) -> MemoryRecord:
        row = conn.execute("SELECT data FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise ValueError(f"Нет записи памяти {memory_id}")
        return MemoryRecord.model_validate_json(row[0])

    def _write_memory(self, conn: sqlite3.Connection, record: MemoryRecord) -> None:
        conn.execute(
            "UPDATE memories SET status = ?, data = ? WHERE id = ?",
            (record.status.value, record.model_dump_json(), record.id),
        )

    @staticmethod
    def _log(conn: sqlite3.Connection, memory_id: str, action: str, at: datetime, **data: Any) -> None:
        conn.execute(
            "INSERT INTO memory_log(memory_id, action, at, data) VALUES (?, ?, ?, ?)",
            (memory_id, action, at.isoformat(), _json(data)),
        )

    def _insert_memory(self, conn: sqlite3.Connection, record: MemoryRecord) -> None:
        if conn.execute("SELECT 1 FROM memories WHERE id = ?", (record.id,)).fetchone():
            raise ValueError(f"Запись памяти {record.id} уже есть — для изменения используйте correct()")
        conn.execute(
            "INSERT INTO memories(id, case_id, status, origin, recorded_at, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.case_id,
                record.status.value,
                record.origin.value,
                record.recorded_at.isoformat(),
                record.model_dump_json(),
            ),
        )

    def remember(self, record: MemoryRecord) -> None:
        with self._tx() as (conn, _):
            self._insert_memory(conn, record)
            self._log(conn, record.id, "recorded", record.recorded_at, origin=record.origin.value)
        self._index([memory_doc(record)], {})

    def memories(self, case_id: str | None = None, include_inactive: bool = False) -> list[MemoryRecord]:
        clauses, params = [], []
        if case_id is not None:
            clauses.append("case_id = ?")
            params.append(case_id)
        if not include_inactive:
            clauses.append("status = ?")
            params.append(MemoryStatus.ACTIVE.value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as (conn, _):
            rows = conn.execute(f"SELECT data FROM memories{where} ORDER BY seq", params).fetchall()
        return [MemoryRecord.model_validate_json(r[0]) for r in rows]

    def memory_history(self, memory_id: str) -> list[dict[str, Any]]:
        """Журнал изменений записи памяти (вторая ось времени: когда что узнали и изменили)."""
        with self._connect() as (conn, _):
            rows = conn.execute(
                "SELECT action, at, data FROM memory_log WHERE memory_id = ? ORDER BY seq", (memory_id,)
            ).fetchall()
        return [{"action": r["action"], "at": r["at"], **json.loads(r["data"])} for r in rows]

    def retract(self, memory_id: str, reason: str, at: datetime) -> None:
        with self._tx() as (conn, _):
            record = self._load_memory(conn, memory_id)
            if record.status == MemoryStatus.RETRACTED:
                return
            record = record.model_copy(update={"status": MemoryStatus.RETRACTED, "reason": reason})
            self._write_memory(conn, record)
            self._log(conn, memory_id, "retracted", at, reason=reason)
        self._index([memory_doc(record)], {})

    def correct(self, memory_id: str, new_record: MemoryRecord) -> None:
        with self._tx() as (conn, _):
            old = self._load_memory(conn, memory_id)
            if old.status != MemoryStatus.ACTIVE:
                hint = f"; актуальная версия — {old.superseded_by}" if old.superseded_by else ""
                raise ValueError(f"Запись {memory_id} не активна ({old.status.value}){hint}")
            if new_record.id == memory_id:
                raise ValueError("Исправление должно быть новой записью с новым ID")
            new_record = new_record.model_copy(update={"status": MemoryStatus.ACTIVE, "superseded_by": None})
            self._insert_memory(conn, new_record)
            old = old.model_copy(update={"status": MemoryStatus.CORRECTED, "superseded_by": new_record.id})
            self._write_memory(conn, old)
            at = new_record.recorded_at
            self._log(conn, memory_id, "corrected", at, superseded_by=new_record.id)
            self._log(conn, new_record.id, "recorded", at, origin=new_record.origin.value, corrects=memory_id)
        self._index([memory_doc(old), memory_doc(new_record)], {})

    def approve(self, memory_id: str, at: datetime) -> None:
        with self._tx() as (conn, _):
            record = self._load_memory(conn, memory_id)
            if record.status != MemoryStatus.ACTIVE:
                raise ValueError(f"Запись {memory_id} не активна ({record.status.value}) — одобрять нечего")
            record = record.model_copy(update={"approved_by_user": True, "approved_at": at})
            self._write_memory(conn, record)
            self._log(conn, memory_id, "approved", at)
        self._index([memory_doc(record)], {})


# --- Методика -----------------------------------------------------------------------------------

_WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")


def _alias(value: str) -> str:
    value = value.split("|", 1)[0].split("#", 1)[0].strip().lower()
    return value.removesuffix(".md")


def load_methodology_docs(knowledge_dir: Path, skills_dir: Path) -> list[KnowledgeDoc]:
    """Markdown из knowledge/methodology/** и .agents/skills/*/SKILL.md → документы kind="methodology".

    id — путь относительно общего корня каталогов (обычно корень проекта): «knowledge/methodology/x.md»,
    «.agents/skills/y/SKILL.md». Ссылки [[имя]] разрешаются по пути, имени файла, заголовку или имени
    навыка и попадают в meta["mentions"] → связи relation="mentions" при индексации.
    """
    knowledge_dir, skills_dir = Path(knowledge_dir), Path(skills_dir)
    methodology_dir = knowledge_dir / "methodology"
    if not methodology_dir.is_dir():
        methodology_dir = knowledge_dir
    files: list[tuple[Path, str]] = []
    if methodology_dir.is_dir():
        files += [(p, "methodology") for p in sorted(methodology_dir.rglob("*.md")) if p.is_file()]
    if skills_dir.is_dir():
        files += [(p, "skill") for p in sorted(skills_dir.glob("*/SKILL.md")) if p.is_file()]
    try:
        base = Path(os.path.commonpath([knowledge_dir.resolve(), skills_dir.resolve()]))
    except ValueError:
        base = knowledge_dir.resolve().parent

    drafts: list[tuple[KnowledgeDoc, list[str]]] = []
    aliases: dict[str, str] = {}
    for path, source in files:
        resolved = path.resolve()
        rel = resolved.relative_to(base).as_posix() if resolved.is_relative_to(base) else path.name
        lines = read_text_file(path).splitlines()
        front, start = split_front_matter(lines)
        body = "\n".join(lines[start:]).strip()
        heading = next((ln[2:].strip() for ln in lines[start:] if ln.startswith("# ")), "")
        skill_name = path.parent.name if source == "skill" else ""
        title = str(front.get("title") or front.get("name") or heading or skill_name or path.stem)
        description = str(front.get("description") or "").strip()
        text = f"{description}\n\n{body}" if description and description not in body else body
        meta: dict[str, Any] = {"path": rel, "source": source}
        for key in ("version", "updated", "tags"):
            if key in front:
                meta[key] = front[key]
        doc = KnowledgeDoc(id=rel, text=text, title=title, kind="methodology", meta=meta)
        drafts.append((doc, _WIKILINK.findall(body)))
        local = path.relative_to(methodology_dir).as_posix() if source == "methodology" else ""
        for alias in (rel, local, path.stem, title, skill_name, str(front.get("name") or "")):
            if alias:
                aliases.setdefault(_alias(alias), rel)

    docs: list[KnowledgeDoc] = []
    for doc, targets in drafts:
        mentions: list[str] = []
        unresolved: list[str] = []
        for target in targets:
            resolved_id = aliases.get(_alias(target))
            if resolved_id and resolved_id != doc.id:
                if resolved_id not in mentions:
                    mentions.append(resolved_id)
            elif not resolved_id and target not in unresolved:
                unresolved.append(target)
        doc.meta[MENTIONS] = mentions
        if unresolved:
            doc.meta["unresolved_mentions"] = unresolved
        docs.append(doc)
    return docs
