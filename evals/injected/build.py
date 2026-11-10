"""Набор подложенных числовых ошибок из сохранённых кейсов агента (evals/results/agent/cases) и meta.yaml.

    uv run python evals/injected/build.py            # → evals/injected/data/<brief>.json (детерминированно)

Источники предложений:
- narrative — предложения повествования агента (выжимка, варианты, стратегии, возражения, решение) со ссылкой на
  строку реестра и числом, у которого есть опора в процитированных строках; сверку v1 они проходят;
- claim — текст строки реестра с числом и ссылкой на неё же («…84,1% [CL-007]»): даёт единицы, которых мало в
  повествовании (месяцы и дни, минус, когорты M1–M3, удержание);
- key_fact — ключевые факты эксперта из meta.yaml (отдельная строка CL-9001…, в реестр кейса не входит; формат ссылки — как у реестра, чтобы сверка её видела).

Разбиение зафиксировано до любой работы над сверкой v2: dev — для настройки правил, holdout — только для итоговой
оценки после заморозки правил (бриф МегаФона добавится в holdout, когда выйдет).
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import mutate as mu  # noqa: E402

from strategist.application.use_cases.report import _support_ids, narrative_texts  # noqa: E402
from strategist.domain.services.citations import (  # noqa: E402
    BARE_REF_RE,
    expand_ranges,
    number_mismatches,
    numbers_in,
    split_sentences,
)

DEV = ("family-pack", "feature-sunset", "price-increase-a", "subscription-retention")
HOLDOUT = ("app-onboarding", "b2b-field-service", "price-increase-b")  # + бриф МегаФона, когда выйдет
CASES = ROOT / "evals" / "results" / "agent" / "cases"
BRIEFS = ROOT / "evals" / "briefs"
DATA = HERE / "data"
PER_KIND = 10  # не больше стольких примеров одного вида на бриф


def load_case(brief_id: str, cases_dir: Path = CASES) -> Any:
    from strategist.infrastructure.persistence.db import CaseDatabase
    from strategist.infrastructure.persistence.repositories import SqliteCaseRepository

    return SqliteCaseRepository(CaseDatabase(cases_dir)).load(f"eval-{brief_id}")


def v1_passes(sentence: str, claims: dict[str, str], brief: str, support: list[str]) -> bool:
    allowed = numbers_in(brief) + [n for cid in support for n in numbers_in(claims.get(cid, ""))]
    return not number_mismatches(sentence, claims, allowed=allowed)


def sources(case: Any, key_facts: list[str]) -> list[dict[str, Any]]:
    """Предложения-основы: проходят сверку v1 и содержат число с единицей, у которого есть опора."""
    claims = {c.id: c.text for c in case.claims}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in narrative_texts(case):
        support = _support_ids(case, text)
        for s in split_sentences(text):
            ids = sorted(set(BARE_REF_RE.findall(expand_ranges(s))) & set(claims))
            cited = [x.value for cid in ids for x in mu.quantities(claims[cid])]
            if not ids or s in seen or mu.supported_quantity(s, cited) is None:
                continue
            if v1_passes(s, claims, case.brief, support):
                seen.add(s)
                out.append({"source": "narrative", "text": s, "cited_ids": ids, "support_ids": support})
    for c in sorted(case.claims, key=lambda c: c.id):
        first = (split_sentences(c.text) or [""])[0]  # одно предложение: ссылка стоит в конце именно его
        if mu.quantities(first):
            out.append({"source": "claim", "text": f"{first.rstrip('. ')} [{c.id}].", "cited_ids": [c.id]})
    for i, fact in enumerate(key_facts, start=1):
        fact = (split_sentences(fact) or [""])[0]
        if mu.quantities(fact):
            out.append(
                {
                    "source": "key_fact",
                    "text": f"{fact.rstrip('. ')} [CL-{9000 + i}].",
                    "cited_ids": [f"CL-{9000 + i}"],
                }
            )
    return out


def _apply(kind: str, src: dict[str, Any], registry: dict[str, str], brief: str) -> str | None:
    s = src["text"]
    cited_qty = [x for cid in src["cited_ids"] for x in mu.quantities(registry.get(cid, ""))]
    cited = [x.value for x in cited_qty]
    q = mu.supported_quantity(s, cited)
    if q is None:
        return None
    brief_qty = mu.quantities(brief)
    named = [x for x in mu.quantities(s) if mu.matches(x.value, cited, x.decimals)]
    support = [x.value for cid in src.get("support_ids", []) for x in mu.quantities(registry.get(cid, ""))]
    covered = bool(support) and mu.matches(q.value, support, q.decimals)
    table = {
        "value_shift": lambda: mu.value_shift(s, q, cited),
        "pct_pp": lambda: mu.pct_pp(s, q),
        "rub_pct": lambda: mu.rub_pct(s, q),
        "days_months": lambda: mu.days_months(
            s, next((x for x in mu.quantities(s) if x.kind in ("month", "day")), q)
        ),
        "cohort_swap": lambda: mu.cohort_swap(s),
        "lost_minus": lambda: mu.lost_minus(s, next((x for x in mu.quantities(s) if x.negative), q)),
        # Ошибка, только если опора элемента не покрывает число: иначе число поддержано и без ссылки в тексте
        "no_ref": lambda: None if covered else mu.no_ref(s),
        "wrong_ref": lambda: None if covered else mu.wrong_ref(s, q, registry, src["cited_ids"]),
        "churn_label": lambda: mu.churn_label(s, q),
        "original": lambda: s,
        "rounding": lambda: mu.rounding(s, q),
        "thousands": lambda: mu.thousands(s, next((x for x in mu.quantities(s) if abs(x.value) >= 1000), q)),
        # Выведенное число — из чисел, названных в предложении и имеющих опору (так пишет и аналитик)
        "derived_diff": lambda: mu.derived_diff(s, named),
        "derived_ratio": lambda: mu.derived_ratio(s, named),
        "brief_number": lambda: mu.brief_number(s, brief_qty, brief),
        "target": lambda: mu.target(s, q, cited),
        "date_year": lambda: mu.date_year(s),
        "id_codes": lambda: mu.id_codes(s),
    }
    out = table[kind]()
    return out if (out and out != s) or kind == "original" else None


# Виды, для которых годится только повествование: служебная строка реестра — не текст для руководителя
NARRATIVE_ONLY = {
    "id_codes",
    "original",
    "no_ref",
    "wrong_ref",
    "target",
    "date_year",
    "brief_number",
    "derived_diff",
    "derived_ratio",
}


def build_brief(brief_id: str, cases_dir: Path = CASES) -> dict[str, Any]:
    meta = yaml.safe_load((BRIEFS / brief_id / "meta.yaml").read_text(encoding="utf-8")) or {}
    key_facts = list((meta.get("expected") or {}).get("key_facts") or [])
    return build_from_case(brief_id, load_case(brief_id, cases_dir), key_facts)


def build_from_case(brief_id: str, case: Any, key_facts: list[str]) -> dict[str, Any]:
    registry = {c.id: c.text for c in case.claims}
    extra = {f"CL-{9000 + i}": f for i, f in enumerate(key_facts, start=1)}
    pool = sources(case, key_facts)
    split = "dev" if brief_id in DEV else "holdout"
    items: list[dict[str, Any]] = []
    for kind in (*mu.MUTANTS, *mu.BENIGN):
        rng = random.Random(f"{brief_id}:{kind}")
        candidates = [s for s in pool if kind not in NARRATIVE_ONLY or s["source"] == "narrative"]
        rng.shuffle(candidates)
        n = 0
        for src in candidates:
            if n >= PER_KIND:
                break
            text = _apply(kind, src, {**registry, **extra}, case.brief)
            if text is None:
                continue
            n += 1
            items.append(
                {
                    "id": f"{brief_id}-{kind}-{n:02d}",
                    "brief": brief_id,
                    "split": split,
                    "kind": kind,
                    "expect_flag": kind in mu.MUTANTS,
                    "source": src["source"],
                    "text": text,
                    "original": src["text"],
                    "cited_ids": src["cited_ids"],
                    "support_ids": src.get("support_ids", []),
                }
            )
    return {
        "brief": brief_id,
        "split": split,
        "case_brief": case.brief,
        "claims": {
            c.id: {"text": c.text, "status": str(getattr(c.status, "value", c.status))} for c in case.claims
        },
        "key_facts": extra,
        "items": items,
    }


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    for brief_id in (*DEV, *HOLDOUT):
        data = build_brief(brief_id)
        path = DATA / f"{brief_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        kinds: dict[str, int] = {}
        for it in data["items"]:
            kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
        print(
            f"{brief_id} ({data['split']}): {len(data['items'])} — "
            + ", ".join(f"{k} {v}" for k, v in kinds.items())
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
