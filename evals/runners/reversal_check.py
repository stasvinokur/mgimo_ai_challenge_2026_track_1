"""Decision Reversal Challenge: решение, принятое на данных брифа A, и решающий документ из брифа B.

Сценарий управляемый. Агент ведёт кейс по брифу A через настоящие шлюзы. Руководитель утверждает план, а на
шлюзе выбора берёт вариант, который судья другого семейства считает ближайшим к правильному ответу для данных A,
и фиксирует решение. Затем приходит решающий файл из B. Без такого старта проверка ничего не различает: если
агент на A сам выбрал ответ B, менять решение не на что.

Успех: решение открыто заново, рекомендация до пересмотра НЕ совпадала с правильным ответом B, после пересмотра
совпала, и часть узлов не тронута. Совпадение оценивает тот же чек-лист, что у метрики «выбор как у эксперта»
(evals/metrics.py).

    uv run python evals/runners/reversal_check.py      # около 5–7 минут и $0,15 на облачных моделях
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_run import strategy_document
from common import (
    BRIEFS_DIR,
    RESULTS_DIR,
    ROOT,
    load_brief,
    load_yaml,
    models_config,
    now_iso,
    ollama_host,
    role,
    write_json,
)
from metrics import DEFAULT_SEED, Judge, run_checklist

CASES_DIR = RESULTS_DIR / "agent" / "cases"
CASE_ID = "eval-reversal"


class OptionPick(BaseModel):
    option_id: str
    why: str = ""


def _judge() -> Judge:
    cfg = models_config()
    return Judge(
        list(cfg.get("judges") or []), ollama_host(cfg), role(cfg, "reasoning")["model"], DEFAULT_SEED
    )


def judge_choice(report_text: str, brief_id: str) -> dict:
    """Совпадает ли рекомендация документа с ожидаемой для брифа (yes / partial / no)."""
    judge = _judge()
    errors: list[str] = []
    parsed = run_checklist(judge, load_brief(brief_id), strategy_document(report_text), errors)
    if parsed is None:
        return {"expected_match": None, "error": "; ".join(errors)}
    return {
        "expected_match": parsed.expected_match,
        "recommended_option": parsed.recommended_option,
        "comment": parsed.expected_match_comment,
        "judge": judge.model,
    }


def pick_option(options: list[dict], target: str) -> OptionPick | None:
    """Руководитель выбирает вариант, ближайший к правильному ответу для данных A (по мнению судьи)."""
    listing = "\n".join(f"{o['id']}: {o['title']} — {o['summary'][:400]}" for o in options)
    prompt = (
        "Какой из вариантов ближе всего к рекомендации эксперта? Верни option_id точно как в списке.\n\n"
        f"Рекомендация эксперта: {target}\n\nВарианты:\n{listing}"
    )
    parsed = _judge().ask("reversal:pick", prompt, OptionPick)
    return parsed if isinstance(parsed, OptionPick) else None


def _materials(brief_id: str) -> list[str]:
    folder = BRIEFS_DIR / brief_id / "materials"
    return [str(p) for p in sorted(folder.iterdir()) if p.is_file() and not p.name.endswith(".meta.yaml")]


def main() -> int:
    from strategist import bootstrap
    from strategist.application.ports import GateAction, GateKind, GateResponse
    from strategist.application.use_cases.start_case import start_case

    meta_a = load_yaml(BRIEFS_DIR / "price-increase-a" / "meta.yaml")
    meta_b = load_yaml(BRIEFS_DIR / "price-increase-b" / "meta.yaml")
    decisive = BRIEFS_DIR / "price-increase-b" / meta_b["flags"]["reversal_pair"]["decisive_file"]
    target_a = str(meta_a["flags"]["reversal_pair"]["correct_here"]).strip()
    if (CASES_DIR / CASE_ID).exists():
        shutil.rmtree(CASES_DIR / CASE_ID)

    app = bootstrap.build_app(interactive=True, cases_dir=CASES_DIR)
    brief_a = load_brief("price-increase-a")
    start_case(
        app.ctx,
        case_id=CASE_ID,
        title="Decision Reversal: price-increase",
        brief=brief_a.request,
        material_paths=_materials(brief_a.id),
    )
    status = app.runner.start(CASE_ID)
    picked: OptionPick | None = None
    for _ in range(12):
        if status.finished or status.waiting_gate is None:
            break
        gate = status.waiting_gate
        if gate.gate == GateKind.OPTION_CHOICE:
            case = app.ctx.cases.load(CASE_ID)
            options = [
                {"id": o.id, "title": o.title, "summary": o.summary}
                for o in case.options
                if o.id in gate.choices
            ]
            picked = pick_option(options, target_a)
            choice = picked.option_id if picked and picked.option_id in gate.choices else gate.default_choice
            response = GateResponse(choice=choice, comment="выбор руководителя по данным брифа A")
        elif gate.gate == GateKind.DECISION_COMMIT:
            response = GateResponse(action=GateAction.APPROVE, approver="руководитель продукта")
        else:
            response = GateResponse(action=GateAction.APPROVE)
        status = app.runner.resume(CASE_ID, response)
    if not status.finished:
        print(json.dumps({"error": "кейс A не дошёл до конца", "status": str(status)}, ensure_ascii=False))
        return 1

    out = CASES_DIR / CASE_ID / "out"
    shutil.copy(out / "report.md", out / "report_before_revision.md")
    before = judge_choice((out / "report.md").read_text(encoding="utf-8"), "price-increase-b")
    selected_before = app.ctx.cases.load(CASE_ID).selected_option_id

    app.runner.revise(CASE_ID, [str(decisive)])
    case = app.ctx.cases.load(CASE_ID)
    after = judge_choice((out / "report.md").read_text(encoding="utf-8"), "price-increase-b")

    rev = case.revisions[-1] if case.revisions else None
    decision = case.decisions[-1] if case.decisions else None
    options = {o.id: o for o in case.options}
    ignored = [
        e.data for e in app.event_log.read(CASE_ID, limit=500) if e.type == "revision.relation_ignored"
    ]
    changed_needed = before.get("expected_match") != "yes"
    result = {
        "checked_at": now_iso(),
        "decisive_file": str(decisive.relative_to(ROOT)),
        "start_choice": {
            "id": selected_before,
            "title": options[selected_before].title if selected_before in options else None,
            "why": picked.why if picked else "судья не ответил — взят вариант по умолчанию",
            "target_for_a": target_a,
        },
        "choice_before_vs_expected_b": before,
        "choice_after_vs_expected_b": after,
        "invalidated_claims": rev.invalidated_claim_ids if rev else [],
        "suspects": len(rev.suspect_ids) if rev else 0,
        "untouched": len(rev.untouched_ids) if rev else 0,
        "revised_nodes": [r.node_id for r in rev.results if r.verdict.value != "unchanged"] if rev else [],
        "ignored_relations": ignored,
        "decision_status": decision.status.value if decision else None,
        "new_stress_winner": case.stress.base_winner if case.stress else None,
        "reopened_options": [o.title for o in case.options if o.status.value == "reopened"],
        "tokens_used": rev.tokens_used if rev else None,
        "tokens_full_regeneration": rev.tokens_full_regeneration_estimate if rev else None,
        "summary": rev.summary if rev else None,
        "expected_b": str(meta_b["flags"]["reversal_pair"]["correct_here"]).strip(),
        "discriminating": changed_needed,
        "success": bool(
            decision
            and decision.status.value == "reopened"
            and changed_needed
            and after.get("expected_match") == "yes"
            and rev
            and rev.untouched_ids
        ),
    }
    write_json(RESULTS_DIR / "comparison" / "reversal.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
