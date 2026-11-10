"""Панель ролей: оценки в прямом и обратном порядке, голосование и разброс как сигнал уверенности.

Перестановка обязательна: одиночные LLM-оценки бизнес-моделей сохраняют согласованность при смене
порядка лишь в диапазоне от <30% до ~80% (Doshi et al., SMJ 2025).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean, pstdev

from pydantic import Field

from strategist.domain.common import Entity
from strategist.domain.entities.options import RoleScore, ScoreOrder


class OptionRanking(Entity):
    option_id: str
    mean: float
    spread: float
    forward_mean: float | None = None
    reverse_mean: float | None = None
    order_delta: float = 0.0
    votes: int = 0
    rank: int = 0


class PanelResult(Entity):
    rankings: list[OptionRanking] = Field(default_factory=list)
    winner: str | None = None
    order_sensitive: bool = False
    note: str = ""


def aggregate_panel(scores: list[RoleScore]) -> PanelResult:
    if not scores:
        return PanelResult(note="оценок нет")
    by_option: dict[str, list[float]] = defaultdict(list)
    by_option_order: dict[tuple[str, ScoreOrder], list[float]] = defaultdict(list)
    ballots: dict[tuple[str, ScoreOrder], list[RoleScore]] = defaultdict(list)
    for s in scores:
        by_option[s.option_id].append(s.score)
        by_option_order[(s.option_id, s.order)].append(s.score)
        ballots[(s.role, s.order)].append(s)

    votes: Counter[str] = Counter()
    for ballot in ballots.values():
        best = max(ballot, key=lambda s: s.score)
        votes[best.option_id] += 1

    rankings = []
    for opt, values in by_option.items():
        fwd = by_option_order.get((opt, ScoreOrder.FORWARD))
        rev = by_option_order.get((opt, ScoreOrder.REVERSE))
        fwd_mean = mean(fwd) if fwd else None
        rev_mean = mean(rev) if rev else None
        rankings.append(
            OptionRanking(
                option_id=opt,
                mean=round(mean(values), 3),
                spread=round(pstdev(values), 3) if len(values) > 1 else 0.0,
                forward_mean=round(fwd_mean, 3) if fwd_mean is not None else None,
                reverse_mean=round(rev_mean, 3) if rev_mean is not None else None,
                order_delta=round(abs((fwd_mean or 0) - (rev_mean or 0)), 3)
                if fwd_mean is not None and rev_mean is not None
                else 0.0,
                votes=votes.get(opt, 0),
            )
        )
    rankings.sort(key=lambda r: (-r.votes, -r.mean, r.option_id))
    for i, r in enumerate(rankings, start=1):
        r.rank = i

    def top(order: ScoreOrder) -> str | None:
        candidates = [(mean(v), o) for (o, ord_), v in by_option_order.items() if ord_ == order]
        return max(candidates)[1] if candidates else None

    fwd_top, rev_top = top(ScoreOrder.FORWARD), top(ScoreOrder.REVERSE)
    order_sensitive = fwd_top is not None and rev_top is not None and fwd_top != rev_top
    note = (
        "Ранжирование зависит от порядка предъявления — уверенность низкая"
        if order_sensitive
        else "Ранжирование устойчиво к перестановке порядка"
    )
    return PanelResult(
        rankings=rankings, winner=rankings[0].option_id, order_sensitive=order_sensitive, note=note
    )
