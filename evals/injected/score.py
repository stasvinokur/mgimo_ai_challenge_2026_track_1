"""Полнота и ложные срабатывания сверки чисел на подложенных ошибках (evals/injected/data).

    uv run python evals/injected/score.py --checker v1                    # dev
    uv run python evals/injected/score.py --checker v1 --split holdout    # только после заморозки правил

Holdout без явного `--split holdout` не считается: правила сверки настраиваются только на dev.
Результат — evals/results/injected/<checker>-<split>.json и таблица в stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import mutate as mu  # noqa: E402

DATA = HERE / "data"
OUT = ROOT / "evals" / "results" / "injected"


def checker_v1(text: str, item: dict[str, Any], data: dict[str, Any]) -> set[str]:
    """Сверка v0.1.1 так, как её вызывает отчёт (report.narrative_number_mismatches): все строки реестра,
    числа запроса и опора элемента разрешены. Возвращает помеченные числа."""
    from strategist.domain.services.citations import number_mismatches, numbers_in

    claims = {cid: c["text"] for cid, c in data["claims"].items()} | data["key_facts"]
    allowed = numbers_in(data["case_brief"]) + [
        n for cid in item.get("support_ids", []) for n in numbers_in(claims.get(cid, ""))
    ]
    return {m.split(" — ")[0] for m in number_mismatches(text, claims, allowed=allowed)}


def checker_v2(text: str, item: dict[str, Any], data: dict[str, Any]) -> set[str]:
    """Сверка v2 так, как её вызывает отчёт с numbers_v2: только активные строки, опора элемента, запрос."""
    from strategist.domain.services.numbers import check_text

    active = {cid: c["text"] for cid, c in data["claims"].items() if c.get("status", "active") == "active"}
    claims = active | data["key_facts"]
    findings = check_text(text, claims, brief=data["case_brief"], support_ids=item.get("support_ids", []))
    return {f"{f.number} ({f.verdict.value})" for f in findings if f.flagged}


Checker = Callable[[str, dict[str, Any], dict[str, Any]], set[str]]
CHECKERS: dict[str, Checker] = {"v1": checker_v1, "v2": checker_v2}


def load(split: str) -> list[dict[str, Any]]:
    return [
        d
        for path in sorted(DATA.glob("*.json"))
        if (d := json.loads(path.read_text(encoding="utf-8")))["split"] == split
    ]


def score(checker: Checker, datasets: list[dict[str, Any]]) -> dict[str, Any]:
    """Дифференциально: правка помечена, если добавила пометку, которой не было у исходного предложения.
    Пометки на самих исходниках — отдельный счёт: это пробелы, найденные в настоящих текстах агента."""
    by_kind: dict[str, dict[str, int]] = {}
    misses: list[dict[str, str]] = []
    gaps: list[dict[str, Any]] = []
    for data in datasets:
        for item in data["items"]:
            before = checker(item["original"], item, data)
            if item["kind"] == "original":
                if before:
                    gaps.append({"id": item["id"], "flags": sorted(before), "text": item["text"][:300]})
                flagged = False
            else:
                flagged = bool(checker(item["text"], item, data) - before)
            k = by_kind.setdefault(item["kind"], {"n": 0, "flagged": 0})
            k["n"] += 1
            k["flagged"] += flagged
            if flagged != item["expect_flag"] and len(misses) < 400:
                misses.append({"id": item["id"], "kind": item["kind"], "text": item["text"][:300]})
    mut = [k for k in mu.MUTANTS if k in by_kind]
    ben = [k for k in mu.BENIGN if k in by_kind]
    caught = sum(by_kind[k]["flagged"] for k in mut)
    total_mut = sum(by_kind[k]["n"] for k in mut)
    false_pos = sum(by_kind[k]["flagged"] for k in ben)
    total_ben = sum(by_kind[k]["n"] for k in ben)
    return {
        "by_kind": by_kind,
        "recall": round(caught / total_mut, 3) if total_mut else None,
        "false_positive_rate": round(false_pos / total_ben, 3) if total_ben else None,
        "mutants": total_mut,
        "benign": total_ben,
        "original_gaps": gaps,
        "errors": misses,
    }


def table(result: dict[str, Any]) -> str:
    rows = ["| Вид | Ожидание | n | Помечено | Доля |", "|---|---|---|---|---|"]
    for kinds, expect in ((mu.MUTANTS, "ошибка — пометить"), (mu.BENIGN, "безобидно — не трогать")):
        for k in kinds:
            v = result["by_kind"].get(k)
            if v:
                rows.append(f"| {k} | {expect} | {v['n']} | {v['flagged']} | {v['flagged'] / v['n']:.0%} |")
            else:
                rows.append(f"| {k} | {expect} | 0 | — | — |")
    originals = result["by_kind"].get("original", {}).get("n", 0)
    rows += [
        "",
        f"Полнота на ошибках: {result['recall']:.0%} из {result['mutants']}; ложные срабатывания на безобидных: "
        f"{result['false_positive_rate']:.0%} из {result['benign']}. Правка засчитывается, если добавила пометку, "
        f"которой не было у исходного предложения. Исходники с найденными пробелами: "
        f"{len(result['original_gaps'])} из {originals}.",
    ]
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--checker", choices=sorted(CHECKERS), default="v1")
    parser.add_argument(
        "--split", choices=["dev", "holdout"], default="dev", help="holdout — только после заморозки правил"
    )
    args = parser.parse_args(argv)
    datasets = load(args.split)
    if not datasets:
        print(f"Нет данных {args.split}: сначала uv run python evals/injected/build.py")
        return 1
    result = score(CHECKERS[args.checker], datasets)
    result.update({"checker": args.checker, "split": args.split, "briefs": [d["brief"] for d in datasets]})
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{args.checker}-{args.split}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Сверка {args.checker}, {args.split} ({', '.join(result['briefs'])}):\n")
    print(table(result))
    print(f"\nПодробности: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
