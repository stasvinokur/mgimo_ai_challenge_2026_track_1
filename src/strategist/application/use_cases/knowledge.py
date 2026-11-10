"""Индексация кейса в базе знаний: источники, цитаты и строки реестра со связями.

Источник правды — кейс и файлы; индекс можно пересобрать в любой момент (`strategist kb index`).
Строки реестра индексируются с типом и опорой: при поиске видно, что из найденного — факт, а что — вывод.
"""

from __future__ import annotations

from strategist.application.context import AppContext
from strategist.application.ports import KnowledgeDoc
from strategist.domain.entities import DecisionCase


def case_documents(case: DecisionCase) -> tuple[list[KnowledgeDoc], list[tuple[str, str, str]]]:
    docs: list[KnowledgeDoc] = []
    links: list[tuple[str, str, str]] = []
    for s in case.sources:
        docs.append(
            KnowledgeDoc(
                id=f"{case.id}:{s.id}",
                text=f"{s.title}. {s.note}".strip(),
                title=s.title,
                kind="material",
                case_id=case.id,
                meta={"source_id": s.id, "synthetic": s.synthetic, "url": s.url or "", "path": s.path or ""},
            )
        )
    for e in case.evidence:
        src = case.source(e.source_id)
        docs.append(
            KnowledgeDoc(
                id=f"{case.id}:{e.id}",
                text=e.quote,
                title=src.title if src else e.source_id,
                kind="material",
                case_id=case.id,
                meta={"evidence_id": e.id, "source_id": e.source_id, "locator": e.locator.render()},
            )
        )
        links.append((f"{case.id}:{e.id}", f"{case.id}:{e.source_id}", "quoted_from"))
    for c in case.active_claims():
        docs.append(
            KnowledgeDoc(
                id=f"{case.id}:{c.id}",
                text=c.text,
                title=f"{c.id} · {c.type.label_ru}",
                kind="claim",
                case_id=case.id,
                meta={
                    "claim_id": c.id,
                    "type": c.type.value,
                    "support": c.support.value,
                    "origin": c.origin.value,
                },
            )
        )
        links += [(f"{case.id}:{c.id}", f"{case.id}:{ev}", "supported_by") for ev in c.evidence_ids]
    return docs, links


def index_case(ctx: AppContext, case: DecisionCase) -> int:
    docs, links = case_documents(case)
    ctx.knowledge.upsert(docs)
    for src, dst, relation in links:
        ctx.knowledge.link(src, dst, relation)
    ctx.emit("knowledge.indexed", case.id, documents=len(docs), links=len(links))
    return len(docs)
