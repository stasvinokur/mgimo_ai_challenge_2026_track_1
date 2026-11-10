"""Правило «нет строки реестра — нет предложения»: поиск предложений без ссылок на ID."""

from __future__ import annotations

import re

_PREFIXES = "CL|EV|SRC|OPT|UNK|EXP|FEAT|PRM|OBJ|DEC|FC|TRG"
REF_RE = re.compile(rf"\[((?:{_PREFIXES})-\d+)\]")
_RANGE_RE = re.compile(rf"(\[?)({_PREFIXES})-(\d+)\s*[–—-]\s*(?:\2-)?(\d+)(\]?)")
_MAX_RANGE = 200


def expand_ranges(text: str) -> str:
    """«[CL-075–CL-088]» → «[CL-075][CL-076]…[CL-088]»: модель ссылается на диапазон строк реестра."""

    def repl(m: re.Match[str]) -> str:
        open_, prefix, start, end, close = m.groups()
        a, b = int(start), int(end)
        if not 0 < b - a <= _MAX_RANGE:
            return m.group(0)
        width = len(start)
        ids = [f"{prefix}-{n:0{width}d}" for n in range(a, b + 1)]
        return "".join(f"[{i}]" for i in ids) if open_ and close else " ".join(ids)

    return _RANGE_RE.sub(repl, text or "")


_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZА-ЯЁ0-9«\"(])")


def referenced_ids(text: str) -> set[str]:
    return set(REF_RE.findall(expand_ranges(text)))


def split_sentences(text: str) -> list[str]:
    parts: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("|"):
            continue
        parts.extend(s.strip() for s in _SENTENCE_RE.split(line) if s.strip())
    return parts


def uncited_sentences(text: str, min_length: int = 40) -> list[str]:
    """Длинные утвердительные предложения без единой ссылки [CL-…]/[OPT-…] и т. п."""
    return [s for s in split_sentences(text) if len(s) >= min_length and not REF_RE.search(expand_ranges(s))]


def unknown_references(text: str, known_ids: set[str]) -> set[str]:
    return referenced_ids(text) - known_ids


BARE_REF_RE = re.compile(rf"\b((?:{_PREFIXES})-\d+)\b")
NUMBER_RE = re.compile(r"(?<![\w.,-])(\d+(?:[.,]\d+)?)\s*(%|п\.\s?п\.|₽|руб)")


def numbers_in(text: str) -> list[float]:
    """Проценты, процентные пункты и суммы в рублях — числа, на которых держатся выводы."""
    return [value for value, _ in _numbers_with_precision(text)]


def _numbers_with_precision(text: str) -> list[tuple[float, float]]:
    """Число и половина единицы его последнего знака: «13%» допускает 12,5–13,5, «13,4%» — 13,35–13,45."""
    clean = BARE_REF_RE.sub(" ", text or "")
    found = []
    for m in NUMBER_RE.finditer(clean):
        raw = m.group(1).replace(",", ".")
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        found.append((float(raw), 0.5 * 10**-decimals))
    return found


def number_mismatches(
    text: str, claim_texts: dict[str, str], tolerance: float = 0.051, allowed: list[float] | None = None
) -> list[str]:
    """Предложения, где число не совпадает ни с одним числом из строк реестра, на которые оно ссылается.

    Модель пишет повествование сама и может перепутать числа (M1 вместо M2): ссылка на строку реестра
    есть, а числа в ней другие. Проверка детерминированная и дешёвая — ошибка становится видимой.
    """
    problems = []
    for sentence in split_sentences(text):
        ids = set(BARE_REF_RE.findall(expand_ranges(sentence)))
        cited = [t for i, t in claim_texts.items() if i in ids]
        if not cited:
            continue
        cited_numbers = [n for t in cited for n in numbers_in(t)] + list(allowed or [])
        for number, rounding in _numbers_with_precision(sentence):
            # Округление законно: «13%» при опоре 13,4% — не ошибка; допуск — по точности числа в тексте
            limit = max(tolerance, rounding + 1e-9)
            if not any(abs(number - c) <= limit for c in cited_numbers):
                problems.append(f"{number:g} — {sentence[:200]}")
    return problems
