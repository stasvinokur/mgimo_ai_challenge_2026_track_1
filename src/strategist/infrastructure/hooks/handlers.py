"""Хуки — действия по событиям шины (EventBus). Подписываются в composition root.

| Событие | Хук | Что делает |
|---|---|---|
| material.registered, ingest.* | — | пишется в журнал событий кейса (SqliteEventLog) |
| injection.detected, pii.masked, web.blocked | security_log | строка в cases/<id>/state/security.jsonl |
| route.misrouted, route.fallback, route.escalated | routing_log | строка в cases/<id>/state/routing.jsonl — данные для перекалибровки порога decide() |
| decision.committed, report.built | git_commit (опц.) | git-коммит каталога out/ кейса, если STRATEGIST_GIT_COMMIT=1 |
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from strategist.application.context import AppContext
from strategist.application.ports import Event

SECURITY_EVENTS = ("injection.detected", "pii.masked", "web.blocked", "integrity.failed", "tool.blocked")
ROUTING_EVENTS = ("route.misrouted", "route.fallback", "route.escalated", "route.playbook")


class JsonlHook:
    def __init__(self, cases_dir: Path, filename: str) -> None:
        self.cases_dir = Path(cases_dir)
        self.filename = filename

    def __call__(self, event: Event) -> None:
        if not event.case_id:
            return
        path = self.cases_dir / event.case_id / "state" / self.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"type": event.type, "at": event.at.isoformat(), **event.data},
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )


class GitCommitHook:
    """Коммит артефактов кейса после фиксации решения (контрольная точка вне IDE)."""

    def __init__(self, root: Path, cases_dir: Path) -> None:
        self.root = Path(root)
        self.cases_dir = Path(cases_dir)

    def __call__(self, event: Event) -> None:
        if not event.case_id:
            return
        out_dir = self.cases_dir / event.case_id / "out"
        if not out_dir.exists():
            return
        rel = out_dir.relative_to(self.root) if self.root in out_dir.parents else out_dir
        subprocess.run(["git", "add", str(rel)], cwd=self.root, capture_output=True, check=False)
        subprocess.run(
            ["git", "commit", "-m", f"strategist: {event.case_id} — {event.type}", "--", str(rel)],
            cwd=self.root,
            capture_output=True,
            check=False,
        )


def register_hooks(ctx: AppContext, settings: Any) -> None:
    cases_dir = settings.paths.cases_dir
    security = JsonlHook(cases_dir, "security.jsonl")
    routing = JsonlHook(cases_dir, "routing.jsonl")
    for name in SECURITY_EVENTS:
        ctx.events.subscribe(name, security)
    for name in ROUTING_EVENTS:
        ctx.events.subscribe(name, routing)
    if os.environ.get("STRATEGIST_GIT_COMMIT") == "1":
        hook = GitCommitHook(settings.paths.root, cases_dir)
        ctx.events.subscribe("decision.committed", hook)
        ctx.events.subscribe("report.built", hook)
