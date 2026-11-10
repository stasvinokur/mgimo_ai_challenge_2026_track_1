"""SQLite-хранилище состояния кейса: cases/<id>/state/case.db (WAL, соединение на операцию)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    message TEXT NOT NULL,
    stage TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    action TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    snapshot TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ledger_claim ON ledger(claim_id);
CREATE TABLE IF NOT EXISTS journal (
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    side_effect INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (run_id, step)
);
CREATE TABLE IF NOT EXISTS journal_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS costs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL DEFAULT '',
    step TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    backend TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    tokens_estimated INTEGER NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0,
    usd_estimated INTEGER NOT NULL DEFAULT 1,
    rub REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS human_time (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    minutes REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    response TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    at TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
"""


class CaseDatabase:
    """Фабрика соединений к базе состояния одного кейса."""

    def __init__(self, cases_dir: Path) -> None:
        self.cases_dir = Path(cases_dir)
        self._initialized: set[Path] = set()

    def state_dir(self, case_id: str) -> Path:
        return self.cases_dir / case_id / "state"

    def path(self, case_id: str) -> Path:
        return self.state_dir(case_id) / "case.db"

    def exists(self, case_id: str) -> bool:
        return self.path(case_id).exists()

    @contextmanager
    def connect(self, case_id: str) -> Iterator[sqlite3.Connection]:
        path = self.path(case_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            if path not in self._initialized:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                self._initialized.add(path)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, case_id: str) -> Iterator[sqlite3.Connection]:
        with self.connect(case_id) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
