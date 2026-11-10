"""Сверка чисел v2: у числа есть единица, а у вывода — допустимые способы получиться из опоры.

Сверка v1 (citations.number_mismatches) теряет единицу (13% и 13 п.п. для неё одно и то же), ломается на
разделителях тысяч («1 234 ₽» читается как 234), минусах и диапазонах, помечает выведенные числа (разность
479 − 399 = 80 ₽) и цели («не ниже 50%»). На подложенных ошибках dev она ловит 21% ошибок и ложно помечает 28%
безобидных правок. Здесь у каждого числа предложения — вердикт: опора есть (совпадает, округлено, выведено из
процитированных строк, взято из запроса, это цель), опоры нет или она не та (нет ссылки, число в другой строке,
другая единица, без опоры). Только вторая группа показывается как ошибка.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from strategist.domain.services.citations import BARE_REF_RE, expand_ranges, split_sentences

SPACES = "    "
_NUM = rf"(?P<sign{{i}}>[−-]|\+)?(?P<int{{i}}>\d{{{{1,3}}}}(?:[{SPACES}]\d{{{{3}}}})+|\d+)(?:[.,](?P<frac{{i}}>\d+))?"
_UNIT = (
    r"(?P<scale>тыс\.?|млн\.?|млрд\.?)?\s?"
    r"(?P<unit>%|п\.\s?п\.?|₽|руб(?:л\w*|\.)?|месяц\w*|мес\.?(?!\w)|день|дня|дней|дни|дн\.?(?!\w)|недел\w*|нед\.?(?!\w)|"
    r"час\w*|ч\.?(?!\w))?"  # годы не сверяются: «детям до 14 лет» — возраст, а не длительность
)
QTY_RE = re.compile(
    rf"(?<![\w.,−-]){_NUM.format(i=1)}(?:\s?[–—-]\s?{_NUM.format(i=2)})?[  ]?{_UNIT}",
)
_DATE_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b|\b(?:19|20)\d\d-\d\d(?:-\d\d)?\b")
_COHORT_RE = re.compile(r"\b[MМ]\d{1,2}\b")
_SCALE = {"тыс": 1e3, "млн": 1e6, "млрд": 1e9}
_UNIT_CLASS = (
    ("pct", re.compile(r"%")),
    ("pp", re.compile(r"п\.\s?п\.?")),
    ("rub", re.compile(r"₽|руб", re.I)),
    ("month", re.compile(r"мес", re.I)),
    ("day", re.compile(r"ден|дн", re.I)),
    ("week", re.compile(r"нед", re.I)),
    ("hour", re.compile(r"час|^ч", re.I)),
)
TARGET_RE = re.compile(
    r"(цел[ьиейю]\w*|целев\w*|не ниже|не выше|не менее|не более|не меньше|не больше|порог\w*|до уровня|kill|"
    r"минимум|максимум|как минимум|хотя бы|ожида\w*|план\w*|прогноз\w*|≥|≤|>|<|довести до|поднять до|"
    r"снизить до|удержать на|если[^.;]{0,50})\s*$",
    re.I,
)
# Условие, горизонт или порог: «если … менее 20%», «через 12 месяцев», «выше 30%» — не утверждение о данных
_CONDITION_RE = re.compile(r"\bесли\b[^.;]*$|\b(через|в течение|за следующие|горизонт\w*)\s*$", re.I)
_COMPARATOR_RE = re.compile(r"\b(менее|более|меньше|больше|свыше|ниже|выше)\s*[~≈]?\s*$", re.I)
_CHURN_WORD = re.compile(r"отток|ушедш|ушл|отмен|churn", re.I)
_RETAIN_WORD = re.compile(r"удержан|оставш|продлен|продлил|retention|retain|active_m|активн", re.I)
_DECREASE_RE = re.compile(r"сниз|упал|паден|сократ|уменьш|потер|ниже|минус|отрицат|ухудш", re.I)


class Quantity(BaseModel):
    value: float  # в базовых единицах (с учётом тыс./млн), со знаком
    unit: str  # класс единицы: pct, pp, rub, month, day, week, hour, count
    tolerance: float  # половина последнего знака записи — допуск на округление
    raw: str
    start: int = 0
    end: int = 0
    signed: bool = False  # знак записан явно (+ или −)


def _unit_class(unit: str | None, scale: str | None) -> str | None:
    if unit:
        return next(cls for cls, rx in _UNIT_CLASS if rx.search(unit))
    return "count" if scale else None


def _value(sign: str | None, integer: str, frac: str | None, scale: float) -> tuple[float, float]:
    digits = "".join(ch for ch in integer if ch.isdigit())
    value = float(f"{digits}.{frac}" if frac else digits) * scale
    tol = 0.5 * 10 ** -(len(frac) if frac else 0) * scale
    return (-value if sign in ("−", "-") else value), tol


_FROM_TO_RE = re.compile(
    rf"\b(?:с|от)\s+(?P<sign>[−-])?(?P<int>\d{{1,3}}(?:[{SPACES}]\d{{3}})+|\d+)(?:[.,](?P<frac>\d+))?\s+до\s+",
    re.I,
)
_BARE_RE = re.compile(rf"(?<![\w.,−-])[−-]?(?:\d{{1,3}}(?:[{SPACES}]\d{{3}})+|\d+)(?:[.,]\d+)?(?![\w%₽])")
_APPROX_RE = re.compile(r"(около|примерно|почти|порядка|приблизительно|~|≈)\s*$", re.I)


def _blank(text: str) -> str:
    clean = BARE_REF_RE.sub(lambda m: " " * len(m.group(0)), text or "")
    clean = _DATE_RE.sub(lambda m: " " * len(m.group(0)), clean)
    return _COHORT_RE.sub(lambda m: " " * len(m.group(0)), clean)


def bare_numbers(text: str) -> list[float]:
    """Числа без единицы (строка реестра «…: 39.6; 75.4») — запасная опора, если единицу не записал расчёт."""
    clean = _blank(text)
    for q in extract_quantities(text):  # числа с единицей — не «голые»
        clean = clean[: q.start] + " " * (q.end - q.start) + clean[q.end :]
    out = []
    for m in _BARE_RE.finditer(clean):
        raw = m.group(0).replace("−", "-")
        digits = "".join(ch for ch in raw if ch.isdigit() or ch in ".,-").replace(",", ".")
        try:
            out.append(abs(float(digits)))
        except ValueError:
            continue
    return out


def extract_quantities(text: str) -> list[Quantity]:
    """Числа с единицей или множителем (тыс., млн): «1 234 ₽», «−3,2 п.п.», «53–60%», «6,8 тыс.», «1,34 мес.».

    Пропускаются коды строк (CL-012), даты (15.03.2027, 2026-01), когорты (M1–M12), годы и возраст."""
    clean = _blank(text)
    out: list[Quantity] = []
    for m in QTY_RE.finditer(clean):
        cls = _unit_class(m.group("unit"), m.group("scale"))
        if cls is None:
            continue
        scale = _SCALE.get((m.group("scale") or "").rstrip("."), 1.0)
        ends = [(m.group("sign1"), m.group("int1"), m.group("frac1"))]
        if m.group("int2"):
            ends.append((m.group("sign2"), m.group("int2"), m.group("frac2")))
        for sign, integer, frac in ends:
            value, tol = _value(sign, integer, frac, scale)
            out.append(
                Quantity(
                    value=value,
                    unit=cls,
                    tolerance=tol,
                    raw=m.group(0).strip(),
                    start=m.start(),
                    end=m.end(),
                    signed=bool(sign),
                )
            )
    for m in _FROM_TO_RE.finditer(clean):  # «с 399 до 479 ₽»: у 399 единица числа после «до»
        nxt = next((q for q in out if q.start == m.end()), None)
        if nxt is None:
            continue
        value, tol = _value(m.group("sign"), m.group("int"), m.group("frac"), 1.0)
        out.append(
            Quantity(
                value=value,
                unit=nxt.unit,
                tolerance=tol,
                raw=m.group(0).strip(),
                start=m.start(),
                end=m.end(),
            )
        )
    return sorted(out, key=lambda q: q.start)


class Verdict(StrEnum):
    OK = "ok"  # совпадает с процитированной строкой
    ROUNDED = "rounded"  # совпадает с точностью записи
    DERIVED = "derived"  # разность, сумма, отношение или изменение чисел процитированных строк
    BRIEF = "brief"  # число из запроса руководителя
    TARGET = "target"  # цель, порог, условие или горизонт, а не факт
    ELSEWHERE = "elsewhere"  # число есть в реестре, но в другой строке, чем процитирована
    NO_REF = "no_ref"  # число есть в реестре, но в предложении нет ссылки
    UNIT_MISMATCH = "unit_mismatch"  # то же значение, но другая единица, знак или «отток» над удержанием
    UNSUPPORTED = "unsupported"  # опоры нет


FLAGGED = frozenset({Verdict.ELSEWHERE, Verdict.NO_REF, Verdict.UNIT_MISMATCH, Verdict.UNSUPPORTED})
VERDICT_RU = {
    Verdict.UNSUPPORTED: "нет опоры",
    Verdict.UNIT_MISMATCH: "другая единица, знак или подпись",
    Verdict.ELSEWHERE: "число есть, но в другой строке реестра",
    Verdict.NO_REF: "число есть в реестре, но без ссылки",
}


class NumberFinding(BaseModel):
    sentence: str
    number: str
    verdict: Verdict
    cited_ids: list[str] = Field(default_factory=list)
    other_ids: list[str] = Field(default_factory=list)  # строки, где число есть (ELSEWHERE, NO_REF)

    @property
    def flagged(self) -> bool:
        return self.verdict in FLAGGED


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol + 1e-9


def _same(q: Quantity, c: Quantity) -> bool:
    return q.unit == c.unit and _close(abs(q.value), abs(c.value), max(q.tolerance, c.tolerance))


def _rounded(q: Quantity, c: Quantity) -> bool:
    return q.unit == c.unit and _close(abs(q.value), abs(c.value), q.tolerance)


def _derived(q: Quantity, pool: list[Quantity]) -> bool:
    """Разность и сумма в той же единице (для % — разность в п.п.), отношение и относительное изменение — в %."""
    tol = max(q.tolerance, 0.005 * abs(q.value))
    v = abs(q.value)
    for i, a in enumerate(pool):
        for b in pool[i + 1 :]:
            if a.unit != b.unit:
                continue
            x, y = abs(a.value), abs(b.value)
            if (
                q.unit == a.unit
                and a.unit not in ("pct", "pp")
                and (_close(v, x + y, tol) or _close(v, abs(x - y), tol))
            ):
                return True
            if q.unit == "pp" and a.unit in ("pct", "pp") and _close(v, abs(x - y), tol):
                return True
            if q.unit == "pct":
                lo, hi = min(x, y), max(x, y)
                if lo > 0 and (_close(v, (hi - lo) / lo * 100, tol) or _close(v, (hi - lo) / hi * 100, tol)):
                    return True
                if a.unit not in ("pct", "pp") and hi > 0 and _close(v, lo / hi * 100, tol):
                    return True
    return False


def _sign_conflict(q: Quantity, c: Quantity, sentence: str) -> bool:
    """В строке реестра число отрицательное, в тексте — без минуса и без слов о снижении."""
    return c.value < 0 and not q.signed and q.value > 0 and not _DECREASE_RE.search(sentence)


def _is_target(before: str) -> bool:
    clause = before.split(";")[-1]
    return bool(
        TARGET_RE.search(clause) or _CONDITION_RE.search(clause) or _COMPARATOR_RE.search(clause[-16:])
    )


def _churn_over_retention(before: str, matched_claims: list[str]) -> bool:
    """«Отток 84,1%», а в строке реестра это число — доля оставшихся (удержание), и об оттоке она не говорит."""
    clause = re.split(r"[;,(]", before)[-1]
    return (
        bool(_CHURN_WORD.search(clause))
        and bool(matched_claims)
        and all(_RETAIN_WORD.search(t) and not _CHURN_WORD.search(t) for t in matched_claims)
    )


def check_sentence(
    sentence: str,
    claims: dict[str, str],
    *,
    brief: str = "",
    support_ids: list[str] | None = None,
) -> list[NumberFinding]:
    refs = [i for i in dict.fromkeys(BARE_REF_RE.findall(expand_ranges(sentence))) if i in claims]
    cited_ids = list(dict.fromkeys([*refs, *[i for i in (support_ids or []) if i in claims]]))
    cited = [c for cid in cited_ids for c in extract_quantities(claims[cid])]
    short = [c for cid in cited_ids if len(qs := extract_quantities(claims[cid])) <= 6 for c in qs]
    own = extract_quantities(sentence)
    brief_qty = extract_quantities(brief)
    cited_bare = [b for cid in cited_ids for b in bare_numbers(claims[cid])]
    findings: list[NumberFinding] = []
    for q in own:
        before = sentence[max(0, q.start - 200) : q.start]
        verdict: Verdict
        others: list[str] = []
        approx = bool(_APPROX_RE.search(before))
        if approx:  # «около 60%» при 61,7%: округление грубее записи
            q = q.model_copy(
                update={"tolerance": max(q.tolerance, 0.05 * abs(q.value), 1.0 if q.unit == "pct" else 0)}
            )
        matched = [c for c in cited if _same(q, c)]
        if matched:
            exact = any(_rounded(c, q) for c in matched)
            holders = [
                claims[cid] for cid in cited_ids if any(_same(q, c) for c in extract_quantities(claims[cid]))
            ]
            if any(_sign_conflict(q, c, sentence) for c in matched) and not any(c.value > 0 for c in matched):
                verdict = Verdict.UNIT_MISMATCH
            elif q.unit == "pct" and _churn_over_retention(before, holders):
                verdict = Verdict.UNIT_MISMATCH  # подпись: «отток» над долей оставшихся
            else:
                verdict = Verdict.OK if exact else Verdict.ROUNDED
        elif any(_same(q, b) for b in brief_qty):
            verdict = Verdict.BRIEF
        elif _is_target(before):
            verdict = Verdict.TARGET
        elif _derived(q, [*short, *brief_qty, *[x for x in own if x is not q]]):
            verdict = Verdict.DERIVED
        elif any(
            c.unit != q.unit and _close(abs(q.value), abs(c.value), max(q.tolerance, c.tolerance))
            for c in cited
        ):
            verdict = Verdict.UNIT_MISMATCH
        elif (abs(q.value) >= 10 or q.tolerance < 0.5) and any(
            _close(abs(q.value), b, q.tolerance) for b in cited_bare
        ):
            verdict = Verdict.ROUNDED  # в строке реестра число без единицы (единицу не записал расчёт)
        else:
            others = [
                cid
                for cid, text in claims.items()
                if cid not in cited_ids and any(_same(q, c) for c in extract_quantities(text))
            ]
            verdict = (Verdict.ELSEWHERE if cited_ids else Verdict.NO_REF) if others else Verdict.UNSUPPORTED
        findings.append(
            NumberFinding(
                sentence=sentence, number=q.raw, verdict=verdict, cited_ids=cited_ids, other_ids=others[:5]
            )
        )
    return findings


def check_text(
    text: str, claims: dict[str, str], *, brief: str = "", support_ids: list[str] | None = None
) -> list[NumberFinding]:
    """Все числа с единицей во всех предложениях текста — с вердиктом (и верные, чтобы считать проверенные)."""
    out: list[NumberFinding] = []
    for sentence in split_sentences(text):
        out += check_sentence(sentence, claims, brief=brief, support_ids=support_ids)
    return out
