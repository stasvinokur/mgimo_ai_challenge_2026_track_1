"""Прогон агента на брифах стенда — так же, как простой чат: те же материалы, автоответы на шлюзах.

Агент запускается через свой CLI (`strategist new … --run --auto`) в отдельном каталоге кейсов,
отчёт копируется в evals/results/agent/<brief>/output.md, а run.json получает время, вызовы, токены и
стоимость из журнала стоимости кейса. Стенд не импортирует код агента — сравнение остаётся честным.

    uv run python evals/runners/agent_run.py --brief subscription-retention
    uv run python evals/runners/agent_run.py --all --jobs 2
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import BRIEFS_DIR, RESULTS_DIR, ROOT, list_briefs, load_brief, now_iso, write_json

OUT_ROOT = RESULTS_DIR / "agent"
CASES_DIR = OUT_ROOT / "cases"


def _costs(case_db: Path) -> dict:
    if not case_db.exists():
        return {}
    conn = sqlite3.connect(case_db)
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(usd),0),"
            " COALESCE(SUM(latency_ms),0) FROM costs"
        ).fetchone()
        models = conn.execute(
            "SELECT model, COUNT(*), SUM(input_tokens + output_tokens) FROM costs GROUP BY model ORDER BY 3 DESC"
        ).fetchall()
    finally:
        conn.close()
    return {
        "calls": row[0],
        "input_tokens": row[1],
        "output_tokens": row[2],
        "usd_list_price": round(row[3], 6),
        "model_latency_s": round(row[4] / 1000, 1),
        "by_model": {m: {"calls": c, "tokens": t} for m, c, t in models},
    }


SERVICE_SECTIONS = ("## Панель самоаудита", "## Чек стоимости", "## Приложение: реестр утверждений")


def strategy_document(report: str) -> str:
    """Для честного сравнения с чатом — только стратегический документ, без служебных разделов агента
    (самоаудит, чек стоимости, приложение-реестр): их числа описывают работу агента, а не материалы."""
    out, skip = [], False
    for line in report.splitlines():
        if line.startswith("## "):
            skip = line.strip() in SERVICE_SECTIONS
        if not skip:
            out.append(line)
    return "\n".join(out) + "\n"


def run_one(
    brief_id: str,
    *,
    force: bool = False,
    label: str = "agent",
    profile: str | None = None,
    quality_off: str | None = None,
) -> dict:
    """label — каталог результатов (evals/results/<label>); profile — профиль моделей (config/models.<profile>.yaml);
    quality_off — выключить улучшения v0.2.0 («all» или флаги через запятую, config/settings.yaml → quality)."""
    out_root = RESULTS_DIR / label
    cases_dir = out_root / "cases"
    brief = load_brief(brief_id)
    out_dir = out_root / brief_id
    if (out_dir / "output.md").exists() and not force:
        return {"brief_id": brief_id, "status": "skipped (exists)"}
    case_id = f"eval-{brief_id}"
    if (cases_dir / case_id).exists():
        shutil.rmtree(cases_dir / case_id)
    env = {**os.environ, "STRATEGIST_CASES_DIR": str(cases_dir)}
    if profile:
        env["STRATEGIST_MODELS_PROFILE"] = profile
        if profile == "local":
            env["STRATEGIST_DECIDE_BACKEND"] = "llm"  # .env не должен вернуть облачный decide()
    if quality_off:
        env["STRATEGIST_QUALITY_OFF"] = quality_off
    started = now_iso()
    t0 = time.monotonic()
    proc = subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "strategist",
            "new",
            case_id,
            "-t",
            brief_id,
            "--brief-file",
            str(BRIEFS_DIR / brief_id / "brief.md"),
            "--materials-dir",
            str(BRIEFS_DIR / brief_id / "materials"),
            "--run",
            "--auto",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    duration = round(time.monotonic() - t0, 1)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = (proc.stdout[-200000:] + "\n" + proc.stderr[-20000:]).replace(f"{ROOT}/", "")  # без локальных путей
    (out_dir / "agent.log").write_text(log, encoding="utf-8")
    report = cases_dir / case_id / "out" / "report.md"
    status = "ok" if proc.returncode == 0 and report.exists() else f"error (exit {proc.returncode})"
    if report.exists():
        full = report.read_text(encoding="utf-8")
        (out_dir / "report_full.md").write_text(full, encoding="utf-8")
        (out_dir / "output.md").write_text(strategy_document(full), encoding="utf-8")
    run = {
        "brief_id": brief_id,
        "approach": "agent",
        "status": status,
        "started_at": started,
        "finished_at": now_iso(),
        "duration_s": duration,
        "materials": [m.path.name for m in brief.materials],
        "quality_off": quality_off or "",
        "costs": _costs(cases_dir / case_id / "state" / "case.db"),
    }
    write_json(out_dir / "run.json", run)
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--brief")
    group.add_argument("--all", action="store_true")
    group.add_argument("--briefs", default=None, help="список брифов через запятую")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--label", default="agent", help="каталог результатов: evals/results/<label>")
    parser.add_argument("--profile", default=None, help="профиль моделей, например local (дешёвый маршрут)")
    parser.add_argument(
        "--quality-off",
        default=None,
        help="выключить улучшения v0.2.0: all или флаги через запятую (абляция)",
    )
    args = parser.parse_args(argv)
    briefs = list_briefs() if args.all else [args.brief]
    if args.briefs:
        briefs = [b.strip() for b in args.briefs.split(",") if b.strip()]
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for run in pool.map(
            lambda b: run_one(
                b, force=args.force, label=args.label, profile=args.profile, quality_off=args.quality_off
            ),
            briefs,
        ):
            costs = run.get("costs", {})
            print(
                f"{run['brief_id']}: {run['status']} · {run.get('duration_s', '—')} с · "
                f"{costs.get('calls', '—')} вызовов · ${costs.get('usd_list_price', '—')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
