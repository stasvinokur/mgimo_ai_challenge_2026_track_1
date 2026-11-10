"""Проверка оснований: на чём держится решение и что потеряет опору, если одно из оснований неверно.

Руководитель видит несущие утверждения (сначала опоры доводов выжимки, затем уязвимые, затем самые неуверенные),
по каждому — цитату и адрес, «за» и «против», и может посмотреть, что изменится, если считать строку неверной.
Всё считается по графу зависимостей (dependency_graph.label с extra_out) — без моделей и без изменения кейса.
Узлы решения, вариантов и стратегий держатся на всех опорах сразу («И»), поэтому формулировка — «лишается
обязательной опоры k из n», а не «решение неверно»: оставшиеся опоры видны.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field

from strategist.domain.common import Entity
from strategist.domain.entities.case import DecisionCase
from strategist.domain.entities.claims import ClaimType
from strategist.domain.services.citations import BARE_REF_RE, expand_ranges
from strategist.domain.services.dependency_graph import build_graph

KIND_RU = {
    "claim": "строка реестра",
    "evidence": "цитата",
    "source": "источник",
    "option": "вариант",
    "objection": "возражение",
    "product": "продуктовая стратегия",
    "feature": "функция",
    "comms": "коммуникационная стратегия",
    "promise": "обещание",
    "experiment": "проверка",
    "decision": "решение",
    "trigger": "сигнал пересмотра",
}
_ASSERTIVE = (
    ClaimType.FACT,
    ClaimType.DERIVED_METRIC,
    ClaimType.HYPOTHESIS,
    ClaimType.ASSUMPTION,
    ClaimType.FORECAST,
)


class SourceRef(Entity):
    """Откуда строка реестра: цитата из источника или расчёт по таблице."""

    source_id: str
    title: str
    synthetic: bool = False
    quote: str = ""  # для цитаты
    locator: str = ""
    cells: list[str] = Field(default_factory=list)  # для расчёта
    period: str | None = None
    rows: int | None = None
    is_calculation: bool = False


def claim_sources(case: DecisionCase, claim_id: str, limit: int = 2) -> list[SourceRef]:
    claim = case.claim(claim_id)
    if claim is None:
        return []
    out: list[SourceRef] = []
    for ev_id in claim.evidence_ids[:limit]:
        ev = case.evidence_item(ev_id)
        src = case.source(ev.source_id) if ev else None
        if ev and src:
            out.append(
                SourceRef(
                    source_id=src.id,
                    title=src.title,
                    synthetic=src.synthetic,
                    quote=ev.quote,
                    locator=ev.locator.render(),
                )
            )
    comp = claim.computation
    src = case.source(comp.source_id) if comp and comp.source_id else None
    if not claim.evidence_ids and comp and src:
        out.append(
            SourceRef(
                source_id=src.id,
                title=src.title,
                synthetic=src.synthetic,
                cells=list(comp.cells),
                period=comp.period,
                rows=comp.rows,
                is_calculation=True,
            )
        )
    return out


class AuditRow(Entity):
    claim_id: str
    text: str
    type_label: str
    support: str
    reliability: str
    why_first: str  # почему строка в списке: опора довода выжимки, уязвимое основание, низкая уверенность
    sources: list[SourceRef] = Field(default_factory=list)
    pro: list[str] = Field(default_factory=list)
    con: list[str] = Field(default_factory=list)
    dependents: int = 0  # сколько узлов (вариантов, стратегий, решений…) держится на строке, прямо и косвенно


def _executive_refs(case: DecisionCase) -> list[str]:
    if not case.executive:
        return []
    ids: list[str] = []
    for b in case.executive.basis:
        ids += b.claim_ids
    for reason in case.executive.reasons:
        ids += BARE_REF_RE.findall(expand_ranges(reason))
    return list(dict.fromkeys(i for i in ids if i.startswith("CL-")))


def rank_load_bearing(case: DecisionCase) -> list[tuple[str, str]]:
    """Несущие утверждения по порядку: опоры доводов выжимки, уязвимые, затем по 1 − уверенность.

    В список попадают активные строки, на которых держится хотя бы один узел, и все опоры выжимки."""
    g = build_graph(case)
    decision = case.active_decision()
    vulnerable = set(decision.vulnerable_claim_ids if decision else []) | {
        c.id for c in case.claims if c.vulnerable
    }
    active = {c.id: c for c in case.active_claims()}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cid in _executive_refs(case):
        if cid in active and cid not in seen:
            out.append((cid, "опора довода выжимки"))
            seen.add(cid)
    for cid in sorted(vulnerable):
        if cid in active and cid not in seen:
            out.append((cid, "уязвимое основание решения"))
            seen.add(cid)
    rest = [c for c in active.values() if c.id not in seen and c.type in _ASSERTIVE and g.dependents(c.id)]
    rest.sort(key=lambda c: (c.confidence if c.confidence is not None else 0.5, c.id))
    out += [(c.id, "низкая уверенность" if (c.confidence or 0.5) < 0.6 else "держит выводы") for c in rest]
    return out


def audit_rows(case: DecisionCase, limit: int = 12) -> list[AuditRow]:
    g = build_graph(case)
    rows: list[AuditRow] = []
    for cid, why in rank_load_bearing(case)[:limit]:
        claim = case.claim(cid)
        if claim is None:
            continue
        sources = claim_sources(case, cid, limit=5)
        pro: list[str] = []
        quotes = [s for s in sources if not s.is_calculation]
        if quotes:
            pro.append(f"цитат {len(quotes)}, источников {len({s.source_id for s in quotes})}")
        comp = claim.computation
        if comp is not None and comp.consistent:
            pro.append(f"расчёт двумя движками ({comp.primary_engine} и {comp.check_engine}) совпал")
        if claim.verified_by:
            pro.append(f"проверено: {claim.verified_by}")
        con: list[str] = []
        for obj in case.objections:
            if cid in obj.claim_ids:
                con.append(f"возражение ({obj.kind.value}, сила {obj.strength or '?'}/5): {obj.text[:140]}")
        con += [f"опровергатель ({d.kind.value}): {d.description[:140]}" for d in claim.defeaters]
        if "Внимание:" in claim.text:
            con.append("предупреждение расчёта: " + claim.text.split("Внимание:", 1)[1].strip()[:160])
        if claim.what_would_change:
            con.append(f"изменит вывод: {claim.what_would_change[:160]}")
        if any(s.synthetic for s in sources):
            con.append("опора — синтетика: не доказывает спрос")
        rows.append(
            AuditRow(
                claim_id=cid,
                text=claim.text,
                type_label=claim.type.label_ru,
                support=claim.support.value,
                reliability=claim.reliability.label_ru,
                why_first=why,
                sources=sources,
                pro=pro,
                con=con[:6],
                dependents=len(g.downstream([cid])),
            )
        )
    return rows


class RemovalImpact(Entity):
    node_id: str
    kind: str  # вид узла по-русски
    label: str
    effect: str  # «теряет опору», «лишается обязательной опоры 1 из 3», «держится на EV-004»
    direct: bool  # прямо опирается на оспоренную строку


class ChallengePreview(Entity):
    removed: list[str]
    unknown: list[str] = Field(default_factory=list)  # ID, которых нет в кейсе
    lost: list[RemovalImpact] = Field(default_factory=list)  # узлы, которые потеряют опору
    holding: list[RemovalImpact] = Field(default_factory=list)  # прямые зависимые, которые устоят
    summary: str = ""


def _and_node(g, node_id: str) -> bool:
    return g.nodes[node_id].kind not in ("claim", "evidence")


def preview_removal(case: DecisionCase, removed: Iterable[str]) -> ChallengePreview:
    """Что потеряет опору, если считать строки неверными. Кейс не меняется; порядок ID не влияет на результат."""
    g = build_graph(case)
    ids = sorted(dict.fromkeys(removed))
    known = [i for i in ids if i in g.nodes]
    unknown = [i for i in ids if i not in g.nodes]
    before = g.label()
    after = g.label(extra_out=known)
    direct = {d for i in known for d in g.dependents(i)}
    lost: list[RemovalImpact] = []
    for node_id in sorted(g.nodes, key=lambda n: (n not in direct, n)):
        if node_id in known or not before.get(node_id) or after.get(node_id):
            continue
        node = g.nodes[node_id]
        if _and_node(g, node_id):
            justs = g.justifications.get(node_id) or [[]]
            best = max(justs, key=lambda j: sum(after.get(a, False) for a in j))
            gone = [a for a in best if not after.get(a, False)]
            effect = f"лишается обязательной опоры {len(gone)} из {len(best)} ({', '.join(gone[:4])})"
        else:
            effect = "теряет опору"
        lost.append(
            RemovalImpact(
                node_id=node_id,
                kind=KIND_RU.get(node.kind, node.kind),
                label=node.label,
                effect=effect,
                direct=node_id in direct,
            )
        )
    holding: list[RemovalImpact] = []
    for node_id in sorted(direct):
        if node_id in known or not after.get(node_id):
            continue
        node = g.nodes[node_id]
        alive = [j for j in g.justifications.get(node_id, []) if all(after.get(a, False) for a in j)]
        rest = sorted({a for j in alive for a in j} - set(known))
        holding.append(
            RemovalImpact(
                node_id=node_id,
                kind=KIND_RU.get(node.kind, node.kind),
                label=node.label,
                effect=f"держится на {', '.join(rest[:4])}" if rest else "держится",
                direct=True,
            )
        )
    counts: dict[str, int] = {}
    for item in lost:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    if lost:
        summary = (
            "Если считать неверными "
            + ", ".join(known)
            + ": потеряют опору — "
            + "; ".join(f"{kind}: {n}" for kind, n in counts.items())
        )
        decisions = [x for x in lost if x.kind == "решение"]
        if decisions:
            summary += ". Решение лишается части обязательных опор — его стоит пересмотреть, остальные опоры видны ниже"
    elif known:
        summary = (
            "Если считать неверными "
            + ", ".join(known)
            + ": ничего не теряет опору — выводы держатся на других основаниях"
        )
    else:
        summary = "Таких строк в кейсе нет"
    return ChallengePreview(removed=known, unknown=unknown, lost=lost, holding=holding, summary=summary + ".")
