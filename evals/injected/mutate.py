"""Подложенные числовые ошибки и безобидные контрпримеры для проверки сверки чисел.

Без людей единственный способ узнать, что сверка ловит ошибки, — подложить их кодом в тексты, которые сверку
проходят, и посчитать, сколько поймано (полнота), а безобидные правки — сколько ложно помечено. Здесь только
чистые функции: предложение + опорные числа → изменённое предложение или None (правка неприменима).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SPACES = "    "
QTY_RE = re.compile(
    rf"(?<![\w.,−-])(?P<sign>[−-])?(?P<int>\d{{1,3}}(?:[{SPACES}]\d{{3}})+|\d+)(?:(?P<sep>[.,])(?P<frac>\d+))?"
    r"(?P<space>[  ]?)(?P<unit>%|п\.\s?п\.|₽|руб\.?|месяц\w*|мес\.?(?!\w)|день|дня|дней|дни|дн\.|недел\w*)"
)
REF_RE = re.compile(r"\[(?:CL|EV|SRC|OPT|UNK|EXP|FEAT|PRM|OBJ|DEC|FC|TRG)-\d+\]")
COHORT_RE = re.compile(r"\b([MМ])(\d{1,2})\b")
RETENTION_RE = re.compile(r"\b(удержани\w*|удерживаемост\w*|retention)\b", re.I)
TOLERANCE = 0.051

MUTANTS = (
    "value_shift",  # число сдвинуто: 31,4% → 40,8%
    "pct_pp",  # % ↔ п.п.
    "rub_pct",  # ₽ → %
    "days_months",  # месяцы ↔ дни
    "cohort_swap",  # M1 ↔ M3
    "lost_minus",  # −3,2 п.п. → 3,2 п.п.
    "no_ref",  # ссылка на строку реестра убрана
    "wrong_ref",  # ссылка на чужую строку
    "churn_label",  # «удержание 84%» → «отток 84%»
)
BENIGN = (
    "original",  # исходное предложение, сверку v1 проходит
    "rounding",  # 13,4% → 13%
    "thousands",  # 1234 ₽ → 1 234 ₽
    "derived_diff",  # разность двух опорных чисел: 479 ₽ и 399 ₽ → «разница 80 ₽»
    "derived_ratio",  # отношение: (479 − 399) / 399 → «на 20,1% выше»
    "brief_number",  # число из запроса руководителя
    "target",  # цель или порог: «цель — не ниже 50%»
    "date_year",  # год и дата
    "id_codes",  # коды элементов: OPT-003, SRC-005, EXP-001
)


@dataclass(frozen=True)
class Qty:
    start: int
    end: int
    value: float
    decimals: int
    sep: str
    unit: str
    space: str
    thousands: str  # разделитель тысяч в записи числа ("" — без разделителя)
    negative: bool

    @property
    def kind(self) -> str:
        u = self.unit
        if u == "%":
            return "pct"
        if u.startswith("п."):
            return "pp"
        if u in ("₽",) or u.startswith("руб"):
            return "rub"
        if u.startswith("мес"):
            return "month"
        if u.startswith(("дн", "ден")):
            return "day"
        return "week"


def quantities(text: str) -> list[Qty]:
    out = []
    for m in QTY_RE.finditer(text):
        raw_int = m.group("int")
        thousands = next((ch for ch in raw_int if ch in SPACES), "")
        digits = "".join(ch for ch in raw_int if ch.isdigit())
        frac = m.group("frac") or ""
        value = float(f"{digits}.{frac}" if frac else digits) * (-1 if m.group("sign") else 1)
        out.append(
            Qty(
                start=m.start(),
                end=m.end(),
                value=value,
                decimals=len(frac),
                sep=m.group("sep") or ",",
                unit=m.group("unit"),
                space=m.group("space"),
                thousands=thousands,
                negative=bool(m.group("sign")),
            )
        )
    return out


def fmt(value: float, decimals: int, sep: str = ",", thousands: str = "") -> str:
    sign = "−" if value < 0 else ""
    text = f"{abs(value):.{decimals}f}"
    whole, _, frac = text.partition(".")
    if thousands and len(whole) > 3:
        groups = []
        while whole:
            groups.insert(0, whole[-3:])
            whole = whole[:-3]
        whole = thousands.join(groups)
    return sign + whole + (sep + frac if frac else "")


def matches(value: float, numbers: list[float], decimals: int = 1) -> bool:
    limit = max(TOLERANCE, 0.5 * 10**-decimals + 1e-9)
    return any(abs(abs(value) - abs(c)) <= limit for c in numbers)


def supported_quantity(sentence: str, cited_numbers: list[float]) -> Qty | None:
    """Первое число с единицей, у которого есть опора в процитированных строках: его и портим."""
    return next((q for q in quantities(sentence) if matches(q.value, cited_numbers, q.decimals)), None)


def _replace(sentence: str, q: Qty, new: str) -> str:
    return sentence[: q.start] + new + sentence[q.end :]


def _number(q: Qty, value: float) -> str:
    return fmt(value, q.decimals, q.sep, q.thousands)


def _insert_before_end(sentence: str, clause: str) -> str:
    """Добавить оборот в то же предложение (до финальной точки), чтобы сверка видела его со ссылкой."""
    body = sentence.rstrip()
    end = body[-1] if body and body[-1] in ".!?…" else ""
    out = (body[:-1] if end else body) + clause
    return out if out.endswith(".") else out + (end or ".")


# ----------------------------------------------------------------------------- ошибки


def value_shift(sentence: str, q: Qty, cited: list[float]) -> str | None:
    for factor in (0.7, 1.3, 1.6) if abs(q.value) > 60 else (1.3, 1.6, 0.7):
        new = round(q.value * factor, q.decimals)
        if new != 0 and not matches(new, cited, q.decimals):
            return _replace(sentence, q, f"{_number(q, new)}{q.space}{q.unit}")
    return None


def pct_pp(sentence: str, q: Qty) -> str | None:
    if q.value == 0:  # «0 ₽» и «0%» — одно и то же «ничего», это не ошибка единицы
        return None
    if q.kind == "pct":
        return _replace(sentence, q, f"{_number(q, q.value)} п.п.")
    if q.kind == "pp":
        return _replace(sentence, q, f"{_number(q, q.value)}%")
    return None


def rub_pct(sentence: str, q: Qty) -> str | None:
    if q.value == 0:  # «0 ₽» и «0%» — одно и то же «ничего», это не ошибка единицы
        return None
    return _replace(sentence, q, f"{_number(q, q.value)}%") if q.kind == "rub" else None


def days_months(sentence: str, q: Qty) -> str | None:
    if q.value == 0:  # «0 ₽» и «0%» — одно и то же «ничего», это не ошибка единицы
        return None
    if q.kind == "month":
        return _replace(sentence, q, f"{_number(q, q.value)} дня")
    if q.kind == "day":
        return _replace(sentence, q, f"{_number(q, q.value)} мес.")
    return None


def cohort_swap(sentence: str) -> str | None:
    m = COHORT_RE.search(sentence)
    if not m:
        return None
    n = int(m.group(2))
    swapped = n + 2 if n <= 2 else n - 2
    return sentence[: m.start()] + f"{m.group(1)}{swapped}" + sentence[m.end() :]


def lost_minus(sentence: str, q: Qty) -> str | None:
    return _replace(sentence, q, f"{_number(q, -q.value)}{q.space}{q.unit}") if q.negative else None


def no_ref(sentence: str) -> str | None:
    stripped = re.sub(r"\s{2,}", " ", REF_RE.sub("", sentence)).strip()
    return stripped if stripped != sentence else None


def wrong_ref(sentence: str, q: Qty, registry: dict[str, str], cited_ids: list[str]) -> str | None:
    """Ссылка заменена на строку реестра, где этого числа нет вовсе (ошибка, а не другая строка с тем же числом)."""
    for cid in sorted(registry):
        values = [x.value for x in quantities(registry[cid])]
        if cid in cited_ids or not values or matches(q.value, values, q.decimals):
            continue
        out = sentence
        for old in cited_ids:
            out = out.replace(f"[{old}]", f"[{cid}]")
        return out if out != sentence else None
    return None


def churn_label(sentence: str, q: Qty) -> str | None:
    if q.kind != "pct":
        return None
    m = RETENTION_RE.search(sentence[max(0, q.start - 60) : q.end])
    if not m:
        return None
    start = max(0, q.start - 60) + m.start()
    word = m.group(1)
    new = "отток" if word.lower().startswith(("удержание", "retention", "удерживаемость")) else "оттока"
    return sentence[:start] + new + sentence[start + len(word) :]


# ----------------------------------------------------------------------------- безобидные правки


def rounding(sentence: str, q: Qty) -> str | None:
    if q.decimals == 0:
        return None
    d = q.decimals - 1
    return _replace(sentence, q, f"{fmt(round(q.value, d), d, q.sep, q.thousands)}{q.space}{q.unit}")


def thousands(sentence: str, q: Qty) -> str | None:
    """Та же сумма с разделителем тысяч и без: «1234 ₽» ↔ «1 234 ₽»."""
    if abs(q.value) < 1000:
        return None
    return _replace(
        sentence, q, f"{fmt(q.value, q.decimals, q.sep, '' if q.thousands else ' ')}{q.space}{q.unit}"
    )


def _same_unit_pair(qs: list[Qty]) -> tuple[Qty, Qty] | None:
    for i, a in enumerate(qs):
        for b in qs[i + 1 :]:
            if a.kind == b.kind and a.kind in ("pct", "rub") and abs(a.value - b.value) > 0.5:
                return (a, b) if a.value > b.value else (b, a)
    return None


def derived_diff(sentence: str, cited_qty: list[Qty]) -> str | None:
    pair = _same_unit_pair(cited_qty)
    if pair is None:
        return None
    a, b = pair
    decimals = max(a.decimals, b.decimals)
    unit = " п.п." if a.kind == "pct" else " ₽"
    return _insert_before_end(sentence, f" — разница {fmt(a.value - b.value, decimals)}{unit}")


def derived_ratio(sentence: str, cited_qty: list[Qty]) -> str | None:
    pair = _same_unit_pair(cited_qty)
    if pair is None or pair[0].kind != "rub" or pair[1].value <= 0:
        return None
    a, b = pair
    ratio = (a.value - b.value) / b.value * 100
    if not 1 <= ratio <= 300:  # правдоподобное «выше на N%», а не отношение несопоставимых сумм
        return None
    return _insert_before_end(sentence, f", то есть на {fmt(ratio, 1)}% выше")


def brief_number(sentence: str, brief_qty: list[Qty], brief_text: str) -> str | None:
    if not brief_qty:
        return None
    q = brief_qty[0]
    return _insert_before_end(sentence, f" (в запросе — {brief_text[q.start : q.end]})")


def target(sentence: str, q: Qty, cited: list[float]) -> str | None:
    for goal in (50, 60, 40, 75, 30):
        if not matches(goal, cited, 0):
            return _insert_before_end(sentence, f"; цель — не ниже {goal}%")
    return None


def date_year(sentence: str) -> str | None:
    return _insert_before_end(sentence, "; проверить до 15.03.2027, повтор — в 2028 году")


def id_codes(sentence: str) -> str | None:
    return _insert_before_end(sentence, "; подробнее — OPT-003, SRC-005 и EXP-001")
