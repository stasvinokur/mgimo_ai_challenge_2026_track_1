"""Парная абляция без разброса прогонов: пересборка отчётов из сохранённых кейсов с флагами качества вкл и выкл.

    uv run python evals/rerender.py --label agent                  # evals/results/rerender/agent/{on,off}/<brief>/
    uv run python evals/rerender.py --label agent-cloud --flags model_labels

Модели не вызываются: кейс берётся из evals/results/<label>/cases/eval-<brief>/state/case.db, отчёт собирается
заново тем же кодом. Пересборка показывает вклад того, что меняется при отрисовке: сверка чисел v2
(`numbers_v2`, служебные разделы) и подписи модельных результатов (`model_labels`, видны в документе стратегии).
Контракт расчёта и повтор выжимки меняют сам кейс — их вклад меряют новые прогоны (`agent_run.py --quality-off`).
Документ стратегии (`output.md`) — тот же срез, что у agent_run.py: без служебных разделов.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "runners"))
from agent_run import strategy_document  # noqa: E402
from common import RESULTS_DIR, write_json  # noqa: E402

RENDER_FLAGS = ("numbers_v2", "model_labels")


def rerender(label: str, flags: tuple[str, ...], briefs: list[str] | None = None) -> list[dict]:
    from strategist import bootstrap
    from strategist.application.quality import FLAGS
    from strategist.application.use_cases.report import number_problems, render_markdown

    cases_dir = RESULTS_DIR / label / "cases"
    app = bootstrap.build_app(mode="live", cases_dir=cases_dir, interactive=False)
    ids = sorted(
        p.name for p in cases_dir.iterdir() if p.name.startswith("eval-") and p.name != "eval-reversal"
    )
    if briefs:
        ids = [i for i in ids if i.removeprefix("eval-") in briefs]
    rows = []
    for case_id in ids:
        case = app.ctx.cases.load(case_id)
        brief = case_id.removeprefix("eval-")
        row: dict = {"brief": brief}
        for variant, on in (("off", False), ("on", True)):
            app.ctx.settings["quality"] = {f: (on and f in flags) for f in FLAGS}
            report = render_markdown(app.ctx, case)
            out = RESULTS_DIR / "rerender" / label / variant / brief
            out.mkdir(parents=True, exist_ok=True)
            (out / "report_full.md").write_text(report, encoding="utf-8")
            (out / "output.md").write_text(strategy_document(report), encoding="utf-8")
            problems, findings = number_problems(app.ctx, case)
            row[variant] = {"number_flags": len(problems), "numbers_checked": len(findings) or None}
        rows.append(row)
    write_json(
        RESULTS_DIR / "rerender" / label / "summary.json",
        {"label": label, "flags": list(flags), "rows": rows},
    )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--label", default="agent", help="каталог evals/results/<label> с сохранёнными кейсами"
    )
    parser.add_argument("--flags", default=",".join(RENDER_FLAGS), help="какие флаги включает вариант on")
    parser.add_argument("--briefs", help="брифы через запятую (по умолчанию все)")
    args = parser.parse_args(argv)
    flags = tuple(f for f in args.flags.split(",") if f)
    unknown = [f for f in flags if f not in RENDER_FLAGS]
    if unknown:
        parser.error(
            f"при пересборке действуют только {RENDER_FLAGS}; {unknown} меняют кейс — нужны новые прогоны"
        )
    rows = rerender(args.label, flags, args.briefs.split(",") if args.briefs else None)
    print("| Бриф | Пометок чисел: выкл (v1) | вкл (v2) | Чисел проверено v2 |\n|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['brief']} | {r['off']['number_flags']} | {r['on']['number_flags']} | {r['on']['numbers_checked']} |"
        )
    print(json.dumps({"label": args.label, "flags": flags}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
