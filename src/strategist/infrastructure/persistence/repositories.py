"""SQLite-адаптеры: кейс (версии снимков), реестр, журнал шагов, кэш ответов модели, события."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from strategist.application.errors import CaseNotFound
from strategist.application.ports import (
    CaseSummary,
    CaseVersion,
    Event,
    JournalEntry,
    JournalStatus,
    LedgerEntry,
)
from strategist.domain.entities import DecisionCase
from strategist.infrastructure.persistence.db import CaseDatabase


class SqliteCaseRepository:
    """Каждое сохранение — новая версия снимка агрегата («Decision Git» без потери истории)."""

    def __init__(self, db: CaseDatabase) -> None:
        self.db = db

    def exists(self, case_id: str) -> bool:
        if not self.db.exists(case_id):
            return False
        with self.db.connect(case_id) as conn:
            return conn.execute("SELECT 1 FROM versions LIMIT 1").fetchone() is not None

    def create(self, case: DecisionCase) -> None:
        case.version = 1
        with self.db.transaction(case.id) as conn:
            if conn.execute("SELECT 1 FROM versions LIMIT 1").fetchone():
                raise ValueError(f"Кейс {case.id} уже существует")
            conn.execute(
                "INSERT INTO versions(version, at, message, stage, data) VALUES (1, ?, ?, ?, ?)",
                (case.updated_at.isoformat(), "create", case.stage.value, case.model_dump_json()),
            )

    def load(self, case_id: str, version: int | None = None) -> DecisionCase:
        if not self.db.exists(case_id):
            raise CaseNotFound(f"Кейс {case_id} не найден в {self.db.cases_dir}")
        with self.db.connect(case_id) as conn:
            if version is None:
                row = conn.execute("SELECT data FROM versions ORDER BY version DESC LIMIT 1").fetchone()
            else:
                row = conn.execute("SELECT data FROM versions WHERE version = ?", (version,)).fetchone()
        if row is None:
            raise CaseNotFound(f"Кейс {case_id}: нет версии {version or 'последней'}")
        return DecisionCase.model_validate_json(row["data"])

    def save(self, case: DecisionCase, message: str) -> int:
        with self.db.transaction(case.id) as conn:
            top = conn.execute("SELECT MAX(version) AS v FROM versions").fetchone()["v"] or 0
            case.version = top + 1
            conn.execute(
                "INSERT INTO versions(version, at, message, stage, data) VALUES (?, ?, ?, ?, ?)",
                (
                    case.version,
                    case.updated_at.isoformat(),
                    message,
                    case.stage.value,
                    case.model_dump_json(),
                ),
            )
        return case.version

    def versions(self, case_id: str) -> list[CaseVersion]:
        if not self.db.exists(case_id):
            return []
        with self.db.connect(case_id) as conn:
            rows = conn.execute("SELECT version, at, message FROM versions ORDER BY version").fetchall()
        return [
            CaseVersion(version=r["version"], at=datetime.fromisoformat(r["at"]), message=r["message"])
            for r in rows
        ]

    def list_cases(self) -> list[CaseSummary]:
        out: list[CaseSummary] = []
        if not self.db.cases_dir.exists():
            return out
        for case_dir in sorted(p for p in self.db.cases_dir.iterdir() if p.is_dir()):
            if not (case_dir / "state" / "case.db").exists():
                continue
            try:
                case = self.load(case_dir.name)
            except CaseNotFound:
                continue
            out.append(
                CaseSummary(
                    id=case.id,
                    title=case.title,
                    stage=case.stage.value,
                    version=case.version,
                    updated_at=case.updated_at,
                )
            )
        return out


class SqliteLedger:
    def __init__(self, db: CaseDatabase) -> None:
        self.db = db

    def append(self, entries: list[LedgerEntry]) -> None:
        by_case: dict[str, list[LedgerEntry]] = {}
        for e in entries:
            by_case.setdefault(e.case_id, []).append(e)
        for case_id, items in by_case.items():
            with self.db.transaction(case_id) as conn:
                conn.executemany(
                    "INSERT INTO ledger(claim_id, action, at, actor, snapshot, note) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            e.claim_id,
                            e.action,
                            e.at.isoformat(),
                            e.actor,
                            json.dumps(e.snapshot, ensure_ascii=False, default=str),
                            e.note,
                        )
                        for e in items
                    ],
                )

    def history(self, case_id: str, claim_id: str | None = None) -> list[LedgerEntry]:
        if not self.db.exists(case_id):
            return []
        with self.db.connect(case_id) as conn:
            if claim_id:
                rows = conn.execute(
                    "SELECT * FROM ledger WHERE claim_id = ? ORDER BY seq", (claim_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM ledger ORDER BY seq").fetchall()
        return [
            LedgerEntry(
                case_id=case_id,
                claim_id=r["claim_id"],
                action=r["action"],
                at=datetime.fromisoformat(r["at"]),
                actor=r["actor"],
                snapshot=json.loads(r["snapshot"]),
                note=r["note"],
            )
            for r in rows
        ]


def _journal_entry(case_id: str, r: Any) -> JournalEntry:
    return JournalEntry(
        case_id=case_id,
        run_id=r["run_id"],
        step=r["step"],
        status=JournalStatus(r["status"]),
        input_hash=r["input_hash"],
        output_hash=r["output_hash"],
        payload=json.loads(r["payload"]),
        side_effect=bool(r["side_effect"]),
        error=r["error"],
        at=datetime.fromisoformat(r["at"]),
        attempt=r["attempt"],
    )


class SqliteJournal:
    """Журнал упреждающей записи: intent до шага, done после; завершённые шаги не повторяются."""

    def __init__(self, db: CaseDatabase) -> None:
        self.db = db

    def get(self, case_id: str, run_id: str, step: str) -> JournalEntry | None:
        if not self.db.exists(case_id):
            return None
        with self.db.connect(case_id) as conn:
            row = conn.execute(
                "SELECT * FROM journal WHERE run_id = ? AND step = ?", (run_id, step)
            ).fetchone()
        return _journal_entry(case_id, row) if row else None

    def _log(self, conn: Any, entry: JournalEntry) -> None:
        conn.execute(
            "INSERT INTO journal_log(run_id, step, status, input_hash, output_hash, error, at) VALUES (?,?,?,?,?,?,?)",
            (
                entry.run_id,
                entry.step,
                entry.status.value,
                entry.input_hash,
                entry.output_hash,
                entry.error,
                entry.at.isoformat(),
            ),
        )

    def intent(
        self, case_id: str, run_id: str, step: str, input_hash: str, *, side_effect: bool, at: datetime
    ) -> JournalEntry:
        with self.db.transaction(case_id) as conn:
            prev = conn.execute(
                "SELECT attempt FROM journal WHERE run_id = ? AND step = ?", (run_id, step)
            ).fetchone()
            attempt = (prev["attempt"] + 1) if prev else 1
            conn.execute(
                "INSERT OR REPLACE INTO journal(run_id, step, status, input_hash, output_hash, payload, side_effect, "
                "error, at, attempt) VALUES (?, ?, 'intent', ?, '', '{}', ?, '', ?, ?)",
                (run_id, step, input_hash, int(side_effect), at.isoformat(), attempt),
            )
            entry = JournalEntry(
                case_id=case_id,
                run_id=run_id,
                step=step,
                status=JournalStatus.INTENT,
                input_hash=input_hash,
                side_effect=side_effect,
                at=at,
                attempt=attempt,
            )
            self._log(conn, entry)
        return entry

    def done(
        self, case_id: str, run_id: str, step: str, output_hash: str, payload: dict[str, Any], at: datetime
    ) -> JournalEntry:
        with self.db.transaction(case_id) as conn:
            conn.execute(
                "UPDATE journal SET status = 'done', output_hash = ?, payload = ?, at = ? WHERE run_id = ? AND step = ?",
                (
                    output_hash,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    at.isoformat(),
                    run_id,
                    step,
                ),
            )
            row = conn.execute(
                "SELECT * FROM journal WHERE run_id = ? AND step = ?", (run_id, step)
            ).fetchone()
            entry = _journal_entry(case_id, row)
            self._log(conn, entry)
        return entry

    def fail(self, case_id: str, run_id: str, step: str, error: str, at: datetime) -> JournalEntry:
        with self.db.transaction(case_id) as conn:
            conn.execute(
                "UPDATE journal SET status = 'failed', error = ?, at = ? WHERE run_id = ? AND step = ?",
                (error[:2000], at.isoformat(), run_id, step),
            )
            row = conn.execute(
                "SELECT * FROM journal WHERE run_id = ? AND step = ?", (run_id, step)
            ).fetchone()
            entry = _journal_entry(case_id, row)
            self._log(conn, entry)
        return entry

    def entries(self, case_id: str, run_id: str | None = None) -> list[JournalEntry]:
        if not self.db.exists(case_id):
            return []
        with self.db.connect(case_id) as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM journal WHERE run_id = ? ORDER BY at", (run_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM journal ORDER BY at").fetchall()
        return [_journal_entry(case_id, r) for r in rows]


class SqliteResponseCache:
    """Кэш ответов модели по хешу запроса: повтор прерванного шага не вызывает модель снова."""

    def __init__(self, db: CaseDatabase) -> None:
        self.db = db

    def get(self, case_id: str, key: str) -> dict[str, Any] | None:
        if not self.db.exists(case_id):
            return None
        with self.db.connect(case_id) as conn:
            row = conn.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row["response"]) if row else None

    def put(self, case_id: str, key: str, model: str, response: dict[str, Any], at: datetime) -> None:
        with self.db.transaction(case_id) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache(key, model, response, at) VALUES (?, ?, ?, ?)",
                (key, model, json.dumps(response, ensure_ascii=False, default=str), at.isoformat()),
            )


class SqliteEventLog:
    """Подписчик шины событий: пишет события кейса в его базу (прозрачность: что агент сделал)."""

    def __init__(self, db: CaseDatabase) -> None:
        self.db = db

    def __call__(self, event: Event) -> None:
        if not event.case_id or not self.db.exists(event.case_id):
            return
        with self.db.transaction(event.case_id) as conn:
            conn.execute(
                "INSERT INTO events(type, at, data) VALUES (?, ?, ?)",
                (event.type, event.at.isoformat(), json.dumps(event.data, ensure_ascii=False, default=str)),
            )

    def read(self, case_id: str, limit: int = 200) -> list[Event]:
        if not self.db.exists(case_id):
            return []
        with self.db.connect(case_id) as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [
            Event(
                type=r["type"],
                case_id=case_id,
                at=datetime.fromisoformat(r["at"]),
                data=json.loads(r["data"]),
            )
            for r in reversed(rows)
        ]


def state_dir(cases_dir: Path, case_id: str) -> Path:
    return Path(cases_dir) / case_id / "state"
