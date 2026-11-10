"""Слепое попарное сравнение двух подходов на одном брифе (по мотивам GDPval).

Смещения LLM-судей учтены: оформление унифицировано (служебные разделы агента — реестр и чек стоимости —
вырезаются, ссылки и якоря снимаются), длина нормирована (у обоих текстов один бюджет символов, который
делится между всеми разделами — заголовки сохраняются, длинные блоки сокращаются с конца; обрезка «с начала
до лимита» прятала у длинного документа варианты, стратегию и план проверки), каждая
пара оценивается в двух порядках, судьи — модели других семейств, чем у агента и чата (config/models.yaml
→ judges). Победитель засчитывается, только если вердикт совпал в обоих порядках; иначе — «ничья/неустойчиво».

`--full` — полные документы без сокращения (каталог `{a}-vs-{b}-full`): нормирование длины прятало ~80% документа
агента. Модель может молча обрезать слишком длинный вход по контексту, поэтому у каждого вызова записывается,
сколько токенов модель реально прочитала; вердикт по обрезанному входу не засчитывается и показывается отдельно.

    uv run python evals/judge.py --brief subscription-retention --a baseline --b agent
    uv run python evals/judge.py --all --a baseline --b agent
    uv run python evals/judge.py --all --a baseline --b agent --full
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (
    RESULTS_DIR,
    ROOT,
    chat,
    extract_json,
    list_briefs,
    load_brief,
    models_config,
    ollama_host,
    write_json,
)

RUBRIC = (ROOT / "knowledge" / "rubrics" / "strategy_rubric.md").read_text(encoding="utf-8")
LIMIT = 20000
PRIORITY_CAP = 4000
NUM_CTX = 65536
NUM_CTX_FULL = 131072
# Русский текст у судей стенда — около 2,5 символа на токен у Kimi и 3,2 у Mistral (замер на полных документах
# 06.11.2026). Если модель прочитала меньше токенов, чем prompt_chars / 4, или упёрлась в контекст, вход обрезан
# и вердикт не засчитывается.
MAX_CHARS_PER_TOKEN = 4.0
CTX_FILL = 0.98
CRITERIA = [
    "evidence",  # связь выводов с проверяемыми источниками
    "epistemics",  # факты отдельно от гипотез и прогнозов, честные пробелы
    "options",  # содержательно разные варианты, включая «оставить» и «закрыть», сильные возражения
    "strategy",  # согласованность продуктовой и коммуникационной стратегий, обещания = продукт
    "validation",  # план проверки: baseline, срок, guardrails, kill-критерий
    "decision_usefulness",  # пригодность для решения руководителя
]


class CriterionVerdict(BaseModel):
    criterion: str
    winner: str = Field(description="X | Y | tie")
    reason: str


class Verdict(BaseModel):
    criteria: list[CriterionVerdict]
    overall: str = Field(description="X | Y | tie")
    reason: str


def fit_sections(text: str, limit: int = LIMIT) -> str:
    """Один бюджет на документ, поровну между блоками (## и ###) с перераспределением остатка: короткие блоки
    целиком, длинные — сокращаются с конца по границе строки. Документ короче лимита не меняется."""
    if len(text) <= limit:
        return text
    blocks = re.split(r"(?m)^(?=#{2,3} )", text)
    sizes = [len(b) for b in blocks]
    budget, alloc = limit, [0] * len(blocks)
    # Выжимка решения и заголовок документа — первыми и почти целиком: это то, что читает руководитель
    for i, block in enumerate(blocks):
        if i == 0 or block.startswith("## Решение на одной странице"):
            alloc[i] = min(sizes[i], PRIORITY_CAP)
            budget -= alloc[i]
    pending = [i for i in range(len(blocks)) if not alloc[i]]
    while pending:
        share = budget // len(pending)
        small = [i for i in pending if sizes[i] <= share]
        if not small:
            for i in pending:
                alloc[i] = share
            break
        for i in small:
            alloc[i] = sizes[i]
            budget -= sizes[i]
        pending = [i for i in pending if i not in small]
    out = []
    for block, size, room in zip(blocks, sizes, alloc, strict=True):
        if size <= room:
            out.append(block)
            continue
        cut = block[:room]
        cut = cut[: cut.rfind("\n")] if "\n" in cut[40:] else cut
        out.append(cut.rstrip() + "\n[…сокращено для равной длины]\n\n")
    return "".join(out)


def normalize(text: str, limit: int | None = LIMIT) -> str:
    """limit=None — служебные разделы и якоря снимаются, но текст не сокращается (полные документы)."""
    text = re.split(r"\n## (?:Приложение: реестр утверждений|Чек стоимости)", text)[0]
    text = re.sub(r"<a id=\"[^\"]+\"></a>", "", text)
    text = re.sub(r"\[([A-Z]+-\d+)\]\(#\1\)", r"[\1]", text)
    return fit_sections(text, limit) if limit else text


def _prompt(brief_text: str, x: str, y: str) -> list[dict[str, str]]:
    system = (
        "Ты эксперт по продуктовой стратегии и маркетингу, оцениваешь вслепую два документа X и Y, подготовленных "
        "для руководителя продукта по одному запросу и одним материалам. Не учитывай длину и оформление; "
        "оценивай содержание по рубрике. Ответь JSON по схеме: criteria (по каждому критерию из списка "
        f"{CRITERIA} — winner X|Y|tie и причина), overall X|Y|tie, reason.\n\nРубрика:\n{RUBRIC[:6000]}"
    )
    user = f"Запрос руководителя:\n{brief_text}\n\n=== Документ X ===\n{x}\n\n=== Документ Y ===\n{y}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def truncated_input(prompt_chars: int, prompt_tokens: int, num_ctx: int) -> bool:
    """Модель прочитала заметно меньше токенов, чем символов во входе, или заполнила контекст до предела."""
    if prompt_tokens <= 0:
        return False  # нет данных об использовании (ошибка вызова) — решает поле error
    return prompt_tokens < prompt_chars / MAX_CHARS_PER_TOKEN or prompt_tokens >= CTX_FILL * num_ctx


def _judge_once(model: str, brief_text: str, x: str, y: str, host: str, num_ctx: int = NUM_CTX) -> dict:
    messages = _prompt(brief_text, x, y)
    result = chat(model, messages, host=host, options={"temperature": 0.0, "num_ctx": num_ctx})
    usage = {
        "prompt_chars": sum(len(m["content"]) for m in messages),
        "prompt_tokens": result.prompt_tokens,
        "output_tokens": result.output_tokens,
        "num_ctx": num_ctx,
    }
    usage["truncated"] = truncated_input(usage["prompt_chars"], result.prompt_tokens, num_ctx)
    if not result.ok:
        return {"model": model, "error": "; ".join(result.errors)[:300], **usage}
    try:
        data = extract_json(result.content)
        if isinstance(data, dict) and isinstance(data.get("criteria"), dict):  # {критерий: {winner, reason}}
            data["criteria"] = [
                {"criterion": k, **(v if isinstance(v, dict) else {"winner": str(v), "reason": ""})}
                for k, v in data["criteria"].items()
            ]
        verdict = Verdict.model_validate(data)
    except Exception as exc:
        return {"model": model, "error": f"{type(exc).__name__}: {str(exc)[:200]}", **usage}
    return {
        "model": model,
        "verdict": verdict.model_dump(),
        "tokens": result.prompt_tokens + result.output_tokens,
        **usage,
    }


def out_dir(a: str, b: str, full: bool = False) -> Path:
    return RESULTS_DIR / "comparison" / f"{a}-vs-{b}{'-full' if full else ''}"


def summarize_runs(runs: list[dict], a: str, b: str) -> tuple[dict[str, dict[str, str]], list[dict]]:
    """Итог по судьям. Судья не засчитывается, если хоть в одном порядке нет вердикта или вход обрезан."""

    def resolve(forward: str, reverse: str) -> str:
        f = {"X": a, "Y": b}.get(forward, "tie")
        r = {"X": b, "Y": a}.get(reverse, "tie")
        return f if f == r else "tie/unstable"

    summary: dict[str, dict[str, str]] = {}
    excluded: list[dict] = []
    for run in runs:
        cut = [o for o in ("forward", "reverse") if run[o].get("truncated")]
        if cut:
            excluded.append({"model": run["model"], "reason": "truncated", "orders": cut})
            continue
        fv, rv = run["forward"].get("verdict"), run["reverse"].get("verdict")
        if not fv or not rv:
            excluded.append({"model": run["model"], "reason": "error"})
            continue
        per = {c["criterion"]: c["winner"] for c in fv["criteria"]}
        per_r = {c["criterion"]: c["winner"] for c in rv["criteria"]}
        summary[run["model"]] = {
            "overall": resolve(fv["overall"], rv["overall"]),
            **{c: resolve(per.get(c, "tie"), per_r.get(c, "tie")) for c in CRITERIA},
        }
    return summary, excluded


def judge_brief(brief_id: str, a: str, b: str, *, full: bool = False, num_ctx: int | None = None) -> dict:
    cfg = models_config()
    host = ollama_host(cfg)
    brief = load_brief(brief_id)
    limit = None if full else LIMIT
    ctx = num_ctx or (NUM_CTX_FULL if full else NUM_CTX)
    text_a = normalize((RESULTS_DIR / a / brief_id / "output.md").read_text(encoding="utf-8"), limit)
    text_b = normalize((RESULTS_DIR / b / brief_id / "output.md").read_text(encoding="utf-8"), limit)
    judges = cfg.get("judges", [])
    runs = []
    for model in judges:
        forward = _judge_once(model, brief.request, text_a, text_b, host, ctx)  # X = a, Y = b
        reverse = _judge_once(model, brief.request, text_b, text_a, host, ctx)  # X = b, Y = a
        runs.append({"model": model, "forward": forward, "reverse": reverse})
    summary, excluded = summarize_runs(runs, a, b)
    out = {
        "brief_id": brief_id,
        "a": a,
        "b": b,
        "full": full,
        "chars": {a: len(text_a), b: len(text_b)},
        "judges": judges,
        "summary": summary,
        "excluded": excluded,
        "runs": runs,
    }
    write_json(out_dir(a, b, full) / f"{brief_id}.json", out)
    return out


def write_summary(results: list[dict], a: str, b: str, full: bool = False) -> Path:
    lines = [
        f"# Слепое сравнение: {a} против {b}" + (" (полные документы)" if full else ""),
        "",
        "Победа засчитывается, только если вердикт совпал в обоих "
        "порядках предъявления; иначе — «ничья/неустойчиво».",
        "",
    ]
    if full:
        sizes = [r["chars"] for r in results if r.get("chars")]
        if sizes:
            span = {k: (min(s[k] for s in sizes), max(s[k] for s in sizes)) for k in (a, b)}
            lines += [
                "Документы целиком, без сокращения до общего бюджета: служебные разделы и якоря сняты. Длина, тыс. "
                f"символов: {a} — {span[a][0] / 1000:.0f}–{span[a][1] / 1000:.0f}, {b} — "
                f"{span[b][0] / 1000:.0f}–{span[b][1] / 1000:.0f}. Вердикт по входу, который модель обрезала "
                "по контексту, не засчитывается.",
                "",
            ]
    header = "| Бриф | Судья | Итог | " + " | ".join(CRITERIA) + " |"
    lines += [header, "|" + "---|" * (3 + len(CRITERIA))]
    wins = {a: 0, b: 0, "tie/unstable": 0}
    excluded = []
    for r in results:
        for judge, s in r["summary"].items():
            lines.append(
                f"| {r['brief_id']} | {judge} | {s['overall']} | " + " | ".join(s[c] for c in CRITERIA) + " |"
            )
            # «tie» (оба порядка — ничья) и «tie/unstable» (порядки разошлись) считаются вместе
            key = s["overall"] if s["overall"] in (a, b) else "tie/unstable"
            wins[key] += 1
        excluded += [(r["brief_id"], e) for e in r.get("excluded", [])]
    lines += [
        "",
        f"Итог по всем вердиктам: {a} — {wins.get(a, 0)}, {b} — {wins.get(b, 0)}, "
        f"ничья/неустойчиво — {wins.get('tie/unstable', 0)}.",
    ]
    if excluded:
        why = {"truncated": "модель обрезала вход", "error": "нет вердикта в одном из порядков"}
        lines += [
            "",
            f"Не засчитано вердиктов: {len(excluded)} — "
            + "; ".join(f"{bid}, {e['model']}: {why[e['reason']]}" for bid, e in excluded)
            + ".",
        ]
    path = out_dir(a, b, full) / "SUMMARY.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--brief")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--a", default="baseline")
    parser.add_argument("--b", default="agent")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument(
        "--full", action="store_true", help="полные документы без сокращения до общего бюджета"
    )
    parser.add_argument(
        "--num-ctx", type=int, help=f"контекст судьи (по умолчанию {NUM_CTX}, с --full — {NUM_CTX_FULL})"
    )
    args = parser.parse_args(argv)
    briefs = [
        bid
        for bid in (list_briefs() if args.all else [args.brief])
        if (RESULTS_DIR / args.a / bid / "output.md").exists()
        and (RESULTS_DIR / args.b / bid / "output.md").exists()
    ]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(
            pool.map(
                lambda bid: judge_brief(bid, args.a, args.b, full=args.full, num_ctx=args.num_ctx), briefs
            )
        )
    if args.brief:  # один бриф — сводка по всем уже оценённым брифам этой пары
        results = [
            json.loads(p.read_text(encoding="utf-8"))
            for bid in list_briefs()
            if (p := out_dir(args.a, args.b, args.full) / f"{bid}.json").exists()
        ]
    print(write_summary(results, args.a, args.b, args.full).read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
