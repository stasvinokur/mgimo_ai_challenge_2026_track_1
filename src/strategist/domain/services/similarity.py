"""Различимость вариантов: варианты-клоны отсекаются по косинусу эмбеддингов обоснований."""

from __future__ import annotations

import math
from itertools import combinations


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        raise ValueError("векторы разной длины или пустые")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def find_clone_pairs(vectors: dict[str, list[float]], threshold: float) -> list[tuple[str, str, float]]:
    pairs = []
    for (ida, va), (idb, vb) in combinations(sorted(vectors.items()), 2):
        sim = cosine(va, vb)
        if sim >= threshold:
            pairs.append((ida, idb, round(sim, 4)))
    return sorted(pairs, key=lambda p: -p[2])
