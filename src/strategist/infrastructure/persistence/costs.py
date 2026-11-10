"""Чек стоимости: каждый вызов модели с атрибутами OTel GenAI, минуты человека отдельно."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

from strategist.application.ports import CostRecord, CostSummary, HumanTimeRecord
from strategist.infrastructure.persistence.db import CaseDatabase
from strategist.infrastructure.pricing import Pricing


class SqliteCostLedger:
    def __init__(self, db: CaseDatabase, pricing: Pricing) -> None:
        self.db = db
        self.pricing = pricing

    def record(self, record: CostRecord) -> None:
        if not record.case_id:
            return
        if record.backend != "replay" and record.usd == 0 and (record.input_tokens or record.output_tokens):
            usd, estimated = self.pricing.usd(record.model, record.input_tokens, record.output_tokens)
            record = record.model_copy(update={"usd": usd, "usd_estimated": estimated})
        if record.rub == 0 and record.usd:
            record = record.model_copy(update={"rub": self.pricing.rub(record.usd)})
        attrs = {
            "gen_ai.operation.name": "chat" if record.backend != "jev" else "evaluate",
            "gen_ai.system": record.backend,
            "gen_ai.request.model": record.model,
            "gen_ai.usage.input_tokens": record.input_tokens,
            "gen_ai.usage.output_tokens": record.output_tokens,
            "strategist.task": record.task,
            "strategist.step": record.step,
            **record.attributes,
        }
        with self.db.transaction(record.case_id) as conn:
            conn.execute(
                "INSERT INTO costs(run_id, step, task, backend, model, input_tokens, output_tokens, tokens_estimated,"
                " usd, usd_estimated, rub, latency_ms, at, attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.run_id,
                    record.step,
                    record.task,
                    record.backend,
                    record.model,
                    record.input_tokens,
                    record.output_tokens,
                    int(record.tokens_estimated),
                    record.usd,
                    int(record.usd_estimated),
                    record.rub,
                    record.latency_ms,
                    record.at.isoformat(),
                    json.dumps(attrs, ensure_ascii=False, default=str),
                ),
            )

    def record_human(self, record: HumanTimeRecord) -> None:
        with self.db.transaction(record.case_id) as conn:
            conn.execute(
                "INSERT INTO human_time(minutes, note, at) VALUES (?, ?, ?)",
                (record.minutes, record.note, record.at.isoformat()),
            )

    def records(self, case_id: str) -> list[CostRecord]:
        if not self.db.exists(case_id):
            return []
        with self.db.connect(case_id) as conn:
            rows = conn.execute("SELECT * FROM costs ORDER BY seq").fetchall()
        return [
            CostRecord(
                case_id=case_id,
                run_id=r["run_id"],
                step=r["step"],
                task=r["task"],
                backend=r["backend"],
                model=r["model"],
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                tokens_estimated=bool(r["tokens_estimated"]),
                usd=r["usd"],
                usd_estimated=bool(r["usd_estimated"]),
                rub=r["rub"],
                latency_ms=r["latency_ms"],
                at=datetime.fromisoformat(r["at"]),
                attributes=json.loads(r["attributes"]),
            )
            for r in rows
        ]

    def summary(self, case_id: str) -> CostSummary:
        recs = self.records(case_id)
        human_minutes = 0.0
        if self.db.exists(case_id):
            with self.db.connect(case_id) as conn:
                human_minutes = conn.execute(
                    "SELECT COALESCE(SUM(minutes), 0) AS m FROM human_time"
                ).fetchone()["m"]
        by_step: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        by_model: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for r in recs:
            for bucket in (by_step[r.step or r.task or "—"], by_model[r.model]):
                bucket["calls"] += 1
                bucket["input_tokens"] += r.input_tokens
                bucket["output_tokens"] += r.output_tokens
                bucket["usd"] += r.usd
                bucket["latency_s"] += r.latency_ms / 1000
        usd = sum(r.usd for r in recs)
        uses_cloud = any(r.backend in ("ollama", "systemone") and "cloud" in r.model for r in recs)
        human_rub = self.pricing.human_rub(human_minutes)
        return CostSummary(
            calls=len(recs),
            input_tokens=sum(r.input_tokens for r in recs),
            output_tokens=sum(r.output_tokens for r in recs),
            estimated_tokens_calls=sum(1 for r in recs if r.tokens_estimated),
            usd=round(usd, 6),
            rub=round(self.pricing.rub(usd), 4),
            usd_estimated=any(r.usd_estimated for r in recs) if recs else True,
            subscription_share_usd=self.pricing.subscription_cost_usd(usd) if uses_cloud else 0.0,
            human_minutes=round(human_minutes, 2),
            human_rub=human_rub,
            total_rub=round(self.pricing.rub(usd) + human_rub, 2),
            model_latency_seconds=round(sum(r.latency_ms for r in recs) / 1000, 2),
            by_step={k: dict(v) for k, v in by_step.items()},
            by_model={k: dict(v) for k, v in by_model.items()},
        )
