"""Облегчённая система поддержания истинности (JTMS-подобная) поверх DecisionCase.

Опоры каждого узла хранятся как «ИЛИ из И»: узел жив (IN), пока у него есть хотя бы одно полное
обоснование. Узлы без обоснований — посылки (premise): живы, пока не отозваны.
Пересмотр: обход в ширину от изменённых узлов помечает зависимые «под подозрением»; LLM вызывается
только на них — это ответ на «инерцию убеждений» и «сопутствующий ущерб» из AGM-Bench.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from strategist.domain.common import Entity
from strategist.domain.entities.case import DecisionCase
from strategist.domain.entities.claims import ClaimStatus

PRODUCT_NODE = "PRODUCT"
COMMS_NODE = "COMMS"

# виды узлов графа — для отчёта и панели («почему изменилось»)
NODE_KIND_RU = {
    "claim": "факт",
    "comms": "коммуникация",
    "decision": "решение",
    "evidence": "основание",
    "experiment": "эксперимент",
    "feature": "функция продукта",
    "objection": "возражение",
    "option": "вариант",
    "product": "продуктовая стратегия",
    "promise": "обещание",
    "source": "источник",
    "trigger": "сигнал пересмотра",
}


class GraphNode(Entity):
    id: str
    kind: str
    label: str = ""


class DependencyGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.justifications: dict[str, list[list[str]]] = {}
        self.forced_out: set[str] = set()

    def add_node(self, node_id: str, kind: str, label: str = "", *, out: bool = False) -> None:
        self.nodes[node_id] = GraphNode(id=node_id, kind=kind, label=label[:120])
        self.justifications.setdefault(node_id, [])
        if out:
            self.forced_out.add(node_id)

    def justify(self, node_id: str, antecedents: Iterable[str]) -> None:
        ants = [a for a in antecedents if a]
        if ants:
            self.justifications.setdefault(node_id, []).append(ants)

    def is_premise(self, node_id: str) -> bool:
        return not self.justifications.get(node_id)

    def dependents(self, node_id: str) -> set[str]:
        return {n for n, justs in self.justifications.items() if any(node_id in j for j in justs)}

    def antecedents(self, node_id: str) -> set[str]:
        return {a for j in self.justifications.get(node_id, []) for a in j}

    def downstream(self, changed: Iterable[str], max_depth: int = 6) -> list[str]:
        """Зависимые узлы в порядке обхода в ширину (без самих изменённых)."""
        start = [c for c in changed if c in self.nodes]
        seen = set(start)
        order: list[str] = []
        queue = deque((c, 0) for c in start)
        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for dep in sorted(self.dependents(node)):
                if dep not in seen:
                    seen.add(dep)
                    order.append(dep)
                    queue.append((dep, depth + 1))
        return order

    def label(self, extra_out: Iterable[str] = ()) -> dict[str, bool]:
        """Разметка IN/OUT до неподвижной точки (обоснованные посылки IN, остальные — по опорам)."""
        out = set(self.forced_out) | set(extra_out)
        status = {n: (n not in out) if self.is_premise(n) else False for n in self.nodes}
        changed = True
        while changed:
            changed = False
            for node, justs in self.justifications.items():
                if not justs or node in out:
                    continue
                alive = any(all(status.get(a, False) for a in j) for j in justs)
                if alive != status[node]:
                    status[node] = alive
                    changed = True
        return status


def build_graph(case: DecisionCase) -> DependencyGraph:
    g = DependencyGraph()
    for src in case.sources:
        g.add_node(src.id, "source", src.title, out=not src.is_valid)
    for ev in case.evidence:
        g.add_node(ev.id, "evidence", ev.quote)
        g.justify(ev.id, [ev.source_id])
    for claim in case.claims:
        out = claim.status in (ClaimStatus.RETRACTED, ClaimStatus.SUPERSEDED)
        g.add_node(claim.id, "claim", claim.text, out=out)
    for claim in case.claims:
        if claim.evidence_ids:
            for ev_id in claim.evidence_ids:
                g.justify(claim.id, [ev_id, *claim.depends_on])
        elif claim.computation and claim.computation.source_id:
            g.justify(claim.id, [claim.computation.source_id, *claim.depends_on])
        elif claim.depends_on:
            g.justify(claim.id, claim.depends_on)
    for option in case.options:
        g.add_node(option.id, "option", option.title)
        g.justify(option.id, [*option.supporting_claim_ids, *option.assumption_claim_ids])
    for obj in case.objections:
        g.add_node(obj.id, "objection", obj.text)
        g.justify(obj.id, [obj.option_id, *obj.claim_ids])
    if case.product:
        g.add_node(PRODUCT_NODE, "product", case.product.direction)
        g.justify(PRODUCT_NODE, [case.product.option_id, *case.product.claim_ids])
        for feature in case.product.features:
            g.add_node(feature.id, "feature", feature.name)
            g.justify(feature.id, [PRODUCT_NODE, *feature.claim_ids])
    if case.comms:
        g.add_node(COMMS_NODE, "comms", case.comms.key_message)
        g.justify(COMMS_NODE, [PRODUCT_NODE if case.product else "", *case.comms.proof_claim_ids])
        for promise in case.comms.promises:
            g.add_node(promise.id, "promise", promise.text)
            supports = [*promise.feature_ids, *promise.claim_ids]
            if supports:
                for support in supports:
                    g.justify(promise.id, [COMMS_NODE, support])
            else:
                g.justify(promise.id, [COMMS_NODE])
    for exp in case.experiments:
        g.add_node(exp.id, "experiment", exp.hypothesis)
        g.justify(exp.id, exp.assumption_claim_ids)
    for decision in case.decisions:
        g.add_node(decision.id, "decision", decision.rationale, out=decision.status.value == "superseded")
        g.justify(decision.id, [decision.selected_option_id, *decision.load_bearing_claim_ids])
        for trig in decision.triggers:
            g.add_node(trig.id, "trigger", trig.description)
            g.justify(trig.id, [decision.id, *trig.assumption_claim_ids])
    # Ссылки на несуществующие узлы считаем OUT: опора, которой нет, не держит вывод.
    return g


def affected_nodes(case: DecisionCase, changed_ids: Iterable[str], max_depth: int = 6) -> list[str]:
    return build_graph(case).downstream(changed_ids, max_depth=max_depth)


def unaffected_nodes(case: DecisionCase, affected: Iterable[str], kinds: Iterable[str]) -> list[str]:
    g = build_graph(case)
    affected_set = set(affected)
    wanted = set(kinds)
    return [n for n, node in g.nodes.items() if node.kind in wanted and n not in affected_set]
