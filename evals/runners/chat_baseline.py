"""Базовый подход «простой чат»: одна инструкция + бриф + все материалы → reasoning-модель агента.

Запуск из корня репозитория:
    uv run python evals/runners/chat_baseline.py --brief subscription-retention
    uv run python evals/runners/chat_baseline.py --all [--force] [--jobs 2]

Результат: evals/results/baseline/<brief_id>/output.md и run.json (модель, токены, время, параметры, стоимость).
Модель и параметры берутся из config/models.yaml → roles.reasoning (та же модель, что у агента).
Фолбэки роли намеренно не используются: сравнение честное, только если модель та же. Сбой фиксируется в run.json.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import (
    RESULTS_DIR,
    Brief,
    chat,
    estimate_cost,
    list_briefs,
    load_brief,
    models_config,
    ollama_host,
    render_materials,
    role,
    sha256_text,
    write_json,
)

INSTRUCTION = "Сделай продуктовую и коммуникационную стратегию по запросу руководителя. Используй приложенные материалы."
APPROACH = "chat_baseline"


def build_prompt(brief: Brief) -> str:
    return f"{INSTRUCTION}\n\n## Запрос руководителя\n\n{brief.request}\n\n## Материалы\n\n{render_materials(brief)}\n"


def run_one(brief_id: str, *, out_root: Path, force: bool = False) -> dict:
    out_dir = out_root / brief_id
    if (out_dir / "output.md").exists() and not force:
        print(f"[{brief_id}] уже есть {out_dir / 'output.md'} — пропуск (--force, чтобы перезапустить)")
        return {}
    cfg = models_config()
    reasoning = role(cfg, "reasoning")
    model = reasoning["model"]
    params = {
        "think": reasoning.get("think"),
        "temperature": reasoning.get("temperature"),
        "num_ctx": reasoning.get("num_ctx"),
    }
    options = {k: v for k, v in (("temperature", params["temperature"]), ("num_ctx", params["num_ctx"])) if v}
    host = ollama_host(cfg)
    brief = load_brief(brief_id)
    prompt = build_prompt(brief)
    print(f"[{brief_id}] {model}: промпт {len(prompt)} символов, материалов {len(brief.materials)} — запуск")
    res = chat(
        model, [{"role": "user", "content": prompt}], host=host, think=params["think"], options=options
    )
    run = {
        "brief_id": brief_id,
        "approach": APPROACH,
        "status": "ok" if res.ok else "failed",
        "instruction": INSTRUCTION,
        "model": model,
        "model_role": "reasoning",
        "fallbacks_used": False,
        "host": host,
        "params": params,
        "started_at": res.started_at,
        "finished_at": res.finished_at,
        "duration_s": res.duration_s,
        "attempts": res.attempts,
        "done_reason": res.done_reason,
        "tokens": {
            "input": res.prompt_tokens,
            "output": res.output_tokens,
            "total": res.prompt_tokens + res.output_tokens,
            "note": "output включает токены рассуждения (thinking) модели",
        },
        "ollama_durations_s": res.ollama_durations_s,
        "prompt": {
            "chars": len(prompt),
            "sha256": sha256_text(prompt),
            "materials": [
                {"file": m.rel, "sha256": m.sha256, "bytes": m.size, "synthetic": m.synthetic}
                for m in brief.materials
            ],
        },
        "output": {
            "chars": len(res.content),
            "sha256": sha256_text(res.content) if res.content else None,
            "thinking_chars": len(res.thinking),
        },
        "cost_estimate": estimate_cost(model, res.prompt_tokens, res.output_tokens),
        "errors": res.errors,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    if res.ok:
        (out_dir / "output.md").write_text(res.content + "\n", encoding="utf-8")
    write_json(out_dir / "run.json", run)
    status = "ok" if res.ok else f"СБОЙ: {res.errors[-1] if res.errors else '?'}"
    print(
        f"[{brief_id}] {status}; {res.duration_s} с; токены {res.prompt_tokens} → {res.output_tokens}; "
        f"ответ {len(res.content)} символов"
    )
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--brief", help="id брифа (каталог в evals/briefs)")
    group.add_argument("--all", action="store_true", help="все брифы")
    parser.add_argument("--force", action="store_true", help="перезаписать существующий результат")
    parser.add_argument("--jobs", type=int, default=1, help="параллельных прогонов (по умолчанию 1)")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "baseline", help="каталог результатов")
    parser.add_argument("--print-prompt", action="store_true", help="только напечатать промпт и выйти")
    args = parser.parse_args(argv)
    ids = list_briefs() if args.all else [args.brief]
    if args.print_prompt:
        for bid in ids:
            print(build_prompt(load_brief(bid)))
        return 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        runs = list(pool.map(lambda b: run_one(b, out_root=args.out, force=args.force), ids))
    failed = [r["brief_id"] for r in runs if r and r.get("status") != "ok"]
    if failed:
        print(f"Сбои: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
