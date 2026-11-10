"""Тест смены решения по выжимке (только стенд): ведёт ли выжимка к тому же выбору, что полный документ.

    uv run python evals/summary_fidelity.py --label agent-v2           # → evals/results/summary_fidelity/<label>.json

Clef (System One) детерминирован: на одном и том же входе отвечает одинаково. Поэтому он выбирает рекомендованный
вариант из трёх текстов одного брифа:
- полный документ стратегии (output.md без служебных разделов);
- наша выжимка («Решение на одной странице» из отчёта);
- выжимка одним проходом: рабочая модель сжимает полный документ без нашей проверки чисел и опор.
Шумовой порог — смысловые перестановки: тот же полный документ с другим порядком вариантов в вопросе. Если Clef
меняет выбор от порядка, расхождение выжимки с документом в пределах этого шума ничего не доказывает. Документы
больше 65 тыс. токенов (≈ 160 тыс. символов) идут через запасной путь (LLM-решение). Результат — иллюстрация
(n = числу брифов), а не доказательство.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import RESULTS_DIR, chat, models_config, ollama_host, role, write_json  # noqa: E402

MAX_CLEF_CHARS = 160_000  # ≈ 65 тыс. токенов русского текста
QUESTION = (
    "Ниже — документ для руководителя продукта. Какой вариант документ рекомендует выбрать? Отвечай по тому, что "
    "рекомендует документ, а не по своему мнению."
)
ONE_PASS = (
    "Сожми документ ниже в выжимку на одну страницу для руководителя: прямой ответ, три довода с числами, "
    "следующие шаги, когда пересматривать. Пиши по-русски, только по документу."
)


def executive_section(report: str) -> str:
    m = re.search(r"## Решение на одной странице\n(.*?)(?=\n## )", report, re.S)
    return m.group(1).strip() if m else ""


def _decide(app: Any, case: Any, text: str, options: dict[str, str]) -> dict[str, Any]:
    from strategist.application.ports import DecisionKind, DecisionQuestion

    question = DecisionQuestion(
        id=f"fidelity-{case.id}",
        kind=DecisionKind.CHOICE,
        instructions=QUESTION,
        state=text,
        options=options,
        task="summary_fidelity",
    )
    decider = app.ctx.decision
    path = "clef"
    if len(text) > MAX_CLEF_CHARS and getattr(decider, "fallback", None) is not None:
        decider, path = decider.fallback, "fallback"
    result = decider.decide(question)
    return {
        "choice": result.choice,
        "confidence": round(result.confidence, 3),
        "path": path,
        "chars": len(text),
    }


def one_pass_summary(document: str) -> str:
    cfg = models_config()
    res = chat(
        role(cfg, "workhorse")["model"],
        [{"role": "user", "content": f"{ONE_PASS}\n\n<<<\n{document}\n>>>"}],
        host=ollama_host(cfg),
        options={"temperature": 0},
    )
    return res.content


def run(label: str, briefs: list[str] | None = None) -> dict[str, Any]:
    from strategist import bootstrap
    from strategist.domain.entities import OptionStatus

    cases_dir = RESULTS_DIR / label / "cases"
    app = bootstrap.build_app(mode="live", cases_dir=cases_dir, interactive=False)
    rows = []
    for case_dir in sorted(cases_dir.glob("eval-*")):
        brief = case_dir.name.removeprefix("eval-")
        if brief == "reversal" or (briefs and brief not in briefs):
            continue
        out_dir = RESULTS_DIR / label / brief
        if not (out_dir / "output.md").exists():
            continue
        case = app.ctx.cases.load(case_dir.name)
        options = {
            o.id: f"{o.title} — {o.summary[:240]}" for o in case.options if o.status != OptionStatus.CLONE
        }
        document = (out_dir / "output.md").read_text(encoding="utf-8")
        report = (out_dir / "report_full.md").read_text(encoding="utf-8")
        ours = executive_section(report)
        single = one_pass_summary(document)
        reversed_options = dict(reversed(list(options.items())))
        rotated = dict(list(options.items())[1:] + list(options.items())[:1])
        row = {
            "brief": brief,
            "selected": case.selected_option_id,
            "full": _decide(app, case, document, options),
            "full_reversed": _decide(app, case, document, reversed_options),
            "full_rotated": _decide(app, case, document, rotated),
            "ours": _decide(app, case, ours, options) if ours else None,
            "one_pass": _decide(app, case, single, options) if single else None,
        }
        perms = {row["full"]["choice"], row["full_reversed"]["choice"], row["full_rotated"]["choice"]}
        row["order_sensitive"] = len(perms) > 1
        rows.append(row)
        print(
            brief,
            json.dumps(
                {k: (v["choice"] if isinstance(v, dict) else v) for k, v in row.items()}, ensure_ascii=False
            ),
        )
    n = len(rows)

    def agree(key: str) -> int:
        return sum(1 for r in rows if r[key] and r[key]["choice"] == r["full"]["choice"])

    summary = {
        "label": label,
        "n": n,
        "order_sensitive": sum(r["order_sensitive"] for r in rows),
        "ours_matches_full": agree("ours"),
        "one_pass_matches_full": agree("one_pass"),
        "full_matches_selected": sum(r["full"]["choice"] == r["selected"] for r in rows),
        "ours_matches_selected": sum(1 for r in rows if r["ours"] and r["ours"]["choice"] == r["selected"]),
        "one_pass_matches_selected": sum(
            1 for r in rows if r["one_pass"] and r["one_pass"]["choice"] == r["selected"]
        ),
    }
    data = {"summary": summary, "rows": rows}
    write_json(RESULTS_DIR / "summary_fidelity" / f"{label}.json", data)
    return data


def section(data: dict[str, Any]) -> list[str]:
    s = data["summary"]
    n = s["n"]
    return [
        "",
        f"## Тест смены решения по выжимке (`{s['label']}`, n = {n})",
        "",
        "Clef выбирает рекомендованный вариант по полному документу, по нашей выжимке и по выжимке одним проходом",
        "(рабочая модель без проверки чисел и опор). Шум — перестановки порядка вариантов на полном документе.",
        "",
        "| Сравнение | Совпало |",
        "|---|---|",
        f"| Полный документ: выбор не зависит от порядка вариантов | {n - s['order_sensitive']} из {n} |",
        f"| Наша выжимка → тот же выбор, что по документу | {s['ours_matches_full']} из {n} |",
        f"| Выжимка одним проходом → тот же выбор, что по документу | {s['one_pass_matches_full']} из {n} |",
        f"| Полный документ → вариант, выбранный в кейсе | {s['full_matches_selected']} из {n} |",
        f"| Наша выжимка → вариант, выбранный в кейсе | {s['ours_matches_selected']} из {n} |",
        f"| Выжимка одним проходом → вариант, выбранный в кейсе | {s['one_pass_matches_selected']} из {n} |",
        *_confidence_lines(data.get("rows") or []),
        "",
        f"Иллюстрация, а не доказательство: n = {n}, один судья решений (Clef). Тест проверяет, что выжимка передаёт",
        "ту же рекомендацию, что документ: Clef спрашивают, что рекомендует текст, а рекомендация в нём названа прямо.",
        "Поэтому высокое совпадение ожидаемо и не значит, что по выжимке руководитель сам пришёл бы к тому же",
        "выбору; искажение рекомендации тест поймал бы.",
    ]


def _confidence_lines(rows: list[dict[str, Any]]) -> list[str]:
    """Средняя уверенность Clef по каждому тексту: где выбор совпал, она показывает, насколько ясно текст его даёт."""
    if not rows:
        return []
    out = ["", "| Текст | Средняя уверенность Clef | Наименьшая |", "|---|---|---|"]
    for key, name in (
        ("full", "Полный документ"),
        ("ours", "Наша выжимка"),
        ("one_pass", "Выжимка одним проходом"),
    ):
        conf = [r[key]["confidence"] for r in rows if r.get(key)]
        if conf:
            out.append(f"| {name} | {sum(conf) / len(conf):.2f} | {min(conf):.2f} |".replace(".", ","))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", default="agent-v2")
    parser.add_argument("--briefs", help="через запятую (по умолчанию все с документом)")
    args = parser.parse_args(argv)
    data = run(args.label, args.briefs.split(",") if args.briefs else None)
    print("\n".join(section(data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
