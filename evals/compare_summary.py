"""Сводная таблица «простой чат против агента» по метрикам стенда, стоимости и слепому судейству.

uv run python evals/compare_summary.py   → evals/results/comparison/SUMMARY.md
uv run python evals/compare_summary.py --approaches baseline,agent --census baseline,baseline-r2,agent,agent-cloud

Новые разделы (перепись, судьи на полных документах) идут строго после прежних таблиц: видео разбирает строки,
которые начинаются с «| baseline |» и «| agent |», поэтому в новых таблицах метки подходов — в `кавычках`.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RESULTS_DIR, list_briefs

APPROACHES = ("baseline", "agent", "agent-local")  # agent-local — дешёвый маршрут, если прогнан
CENSUS_DIR = RESULTS_DIR / "census"
BOOT = 10000


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _row(approach: str, brief: str) -> dict:
    m = _load(RESULTS_DIR / approach / brief / "metrics.json")
    run = _load(RESULTS_DIR / approach / brief / "run.json")
    claims, options, promises, plan = (
        m.get("claims", {}),
        m.get("options", {}),
        m.get("promises", {}),
        m.get("validation_plan", {}),
    )
    if approach.startswith("agent"):
        costs = run.get("costs", {})
        calls, usd, duration = costs.get("calls"), costs.get("usd_list_price"), run.get("duration_s")
    else:
        calls, usd, duration = 1, (run.get("cost_estimate") or {}).get("usd"), run.get("duration_s")
    return {
        "unsupported": claims.get("unsupported_share"),
        "contradicted": claims.get("contradicted"),
        "status_quo": options.get("has_status_quo"),
        "kill": options.get("has_kill"),
        "unbacked": f"{promises.get('unbacked', '—')} из {promises.get('total', '—')}",
        "baseline": plan.get("baseline"),
        "guardrail": plan.get("guardrail"),
        "kill_date": plan.get("kill_criterion_has_date"),
        "expert": (m.get("expected_choice") or {}).get("expected_match"),
        "calls": calls,
        "usd": usd,
        "duration": duration,
    }


def _fmt(v: object) -> str:
    if v is True:
        return "да"
    if v is False:
        return "нет"
    if isinstance(v, float):
        return f"{v:.0%}" if v <= 1 else f"{v:.1f}"
    return "—" if v is None else str(v)


def _usd(v: float | None) -> str:
    return "—" if v is None else f"{v:.4f}"


def codex_section() -> list[str]:
    """Третий судья (Codex, GPT): итоги из evals/runners/codex_judge.py рядом с вердиктами судей стенда."""
    found = []
    for name, label in (
        ("baseline-vs-agent-codex", "те же сокращённые документы, что у судей стенда"),
        ("baseline-vs-agent-codex-full", "полные документы"),
        ("baseline-vs-agent-v2-codex-full", "полные документы, агент v0.2.0 (`agent-v2`)"),
    ):
        path = RESULTS_DIR / "comparison" / name / "SUMMARY.md"
        if path.exists():
            line = next(
                (x for x in path.read_text(encoding="utf-8").splitlines() if x.startswith("Итог Codex")), ""
            )
            found.append(f"- {label}: {line.removeprefix('Итог Codex: ')} (`{name}/SUMMARY.md`)")
    if not found:
        return []
    audit = RESULTS_DIR / "comparison" / "baseline-vs-agent-codex" / "SUMMARY.md"
    lean = (
        next((x for x in audit.read_text(encoding="utf-8").splitlines() if x.startswith("В чью пользу")), "")
        if audit.exists()
        else ""
    )
    return [
        "",
        "### Третий судья: Codex (GPT-6.1), вместо ручной проверки",
        "",
        "Ручной проверки людьми нет. Судьи стенда видели документ агента сокращённым до общего бюджета длины:",
        "из 71–84 тыс. символов оставалось 15–17 тыс., а ответ чата помещался целиком. Codex оценил те же пары",
        "вслепую в обоих порядках и с материалами брифа:",
        "",
        *found,
        "",
        lean.split(". Направление")[0] + "." if lean else "",
    ]


def cheap_route_section() -> list[str]:
    """Облачный и локальный маршрут агента на одном коде и одних брифах (agent-cloud против agent-local)."""
    a, b = "agent-cloud", "agent-local"
    briefs = [
        x
        for x in list_briefs()
        if (RESULTS_DIR / a / x / "metrics.json").exists() and (RESULTS_DIR / b / x / "metrics.json").exists()
    ]
    if not briefs:
        return []
    out = [
        "",
        "## Дешёвый маршрут: всё на локальной gpt-oss:20b",
        "",
        "Один и тот же код агента, одни брифы: облачные модели ролей (`agent-cloud`) против профиля",
        "`STRATEGIST_MODELS_PROFILE=local` (`agent-local`, все роли и decide() на gpt-oss:20b, без облака).",
        "",
        "| Бриф | Маршрут | Без опоры | «Оставить» | «Закрыть» | Неподкр. обещания | Guardrail | Kill с датой | Как у эксперта | Вызовов | $ | Время, мин |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for x in briefs:
        for label in (a, b):
            r = _row(label, x)
            minutes = f"{r['duration'] / 60:.1f}" if isinstance(r["duration"], (int, float)) else "—"
            out.append(
                f"| {x} | {label} | {_fmt(r['unsupported'])} | {_fmt(r['status_quo'])} | {_fmt(r['kill'])} | "
                f"{r['unbacked']} | {_fmt(r['guardrail'])} | {_fmt(r['kill_date'])} | {_fmt(r['expert'])} | "
                f"{_fmt(r['calls'])} | {_usd(r['usd'])} | {minutes} |"
            )
    judge = RESULTS_DIR / "comparison" / f"{a}-vs-{b}" / "SUMMARY.md"
    if judge.exists():
        out += [
            "",
            "### Слепое попарное судейство облака и локального маршрута",
            "",
            judge.read_text(encoding="utf-8").split("\n", 1)[1].strip(),
        ]
    return out


def group_of(label: str) -> str:
    """Повторные прогоны одного подхода (baseline-r2, agent-v2-r2) — одна группа."""
    return re.sub(r"-r\d+$", "", label)


# В v0.2.0 раздел подписан в отчёте как расчёт модели агента по допущениям, а не данные из материалов
MODEL_SECTIONS = ("Стресс-тест решения",)


def census_counts(label: str, drop: tuple[str, ...] = ()) -> dict[str, tuple[int, int]]:
    """Бриф → (без опоры, проверено) по переписи с seed по умолчанию; `drop` — разделы документа, которые не считать."""
    out = {}
    for path in sorted((CENSUS_DIR / label).glob("*.json")):
        if ".s" in path.stem:  # другие seed — разброс судьи, не основной замер
            continue
        c = json.loads(path.read_text(encoding="utf-8"))["claims"]
        k, n = c["unsupported"], c["verified"]
        for name in drop:
            s = c.get("by_section", {}).get(name)
            if s:
                k, n = k - s["unsupported"], n - s["verified"]
        out[path.stem] = (k, n)
    return out


def pooled(counts: list[tuple[int, int]]) -> float | None:
    n = sum(v for _, v in counts)
    return sum(k for k, _ in counts) / n if n else None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p, z2 = k / n, z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * (p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5 / (1 + z2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def bootstrap_ci(stat, briefs: list[str], seed: int = 0, n_boot: int = BOOT) -> tuple[float, float] | None:
    """95% интервал по брифам: брифы выбираются с возвращением — разброс между задачами, а не внутри документа."""
    if len(briefs) < 2:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(n_boot):
        v = stat([rng.choice(briefs) for _ in briefs])
        if v is not None:
            values.append(v)
    values.sort()
    return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]


def group_counts(labels: list[str], drop: tuple[str, ...] = ()) -> dict[str, dict[str, tuple[int, int]]]:
    """Группа → бриф → (k, n), сложенные по повторным прогонам группы."""
    groups: dict[str, dict[str, tuple[int, int]]] = {}
    for label in labels:
        for bid, (k, n) in census_counts(label, drop).items():
            g = groups.setdefault(group_of(label), {})
            k0, n0 = g.get(bid, (0, 0))
            g[bid] = (k0 + k, n0 + n)
    return groups


def _p(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%".replace(".", ",")


def _pp(v: float | None) -> str:
    """Разность долей в процентных пунктах со знаком."""
    return "—" if v is None else f"{v * 100:+.1f}".replace(".", ",")


def _ci(ci: tuple[float, float] | None) -> str:
    return "—" if ci is None else f"{_p(ci[0])}–{_p(ci[1])}"


def census_section(labels: list[str]) -> list[str]:
    labels = [x for x in labels if census_counts(x)]
    if not labels:
        return []
    out = [
        "",
        "## Перепись утверждений: доля без опоры",
        "",
        "Проверены все утверждения о фактах, а не выборка из 20 (`evals/metrics.py --sample 0 --claims-only`, судья",
        "Mistral, документ режется по разделам). Доля «по сумме» — все утверждения подхода вместе, интервал Уилсона",
        "(утверждения одного документа не независимы, поэтому он узковат). «Средняя по брифам» — каждый бриф с одним",
        "весом, интервал — бутстреп по брифам (10 000 повторов): он учитывает разброс между задачами.",
        "",
        "| Подход | Брифов | Утверждений | Без опоры | Доля по сумме | 95% Уилсон | Средняя по брифам | 95% бутстреп |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label in labels:
        c = census_counts(label)
        briefs = sorted(c)
        k, n = sum(c[b][0] for b in briefs), sum(c[b][1] for b in briefs)

        def mean(sample: list[str], c: dict = c) -> float | None:
            shares = [c[b][0] / c[b][1] for b in sample if c[b][1]]
            return sum(shares) / len(shares) if shares else None

        out.append(
            f"| `{label}` | {len(briefs)} | {n} | {k} | {_p(k / n if n else None)} | {_ci(wilson(k, n))} | "
            f"{_p(mean(briefs))} | {_ci(bootstrap_ci(mean, briefs))} |"
        )
    groups = group_counts(labels)
    chat = groups.get("baseline")
    if chat:
        out += [
            "",
            "Разность с чатом на общих брифах (повторные прогоны чата сложены; бутстреп по брифам, парный). Строки",
            f"«без стресс-теста» не считают раздел «{MODEL_SECTIONS[0]}»: в v0.2.0 он подписан как расчёт модели агента",
            "по допущениям, а не данные из материалов, но судья переписи всё равно считает его числа утверждениями без",
            "опоры. Обе строки — честные: первая — документ целиком, вторая — без раздела, помеченного как модель.",
            "",
            "| Подход | Брифов | Доля подхода | Доля чата | Разность, п.п. | 95% бутстреп | Вывод |",
            "|---|---|---|---|---|---|---|",
        ]
        trimmed = group_counts(labels, MODEL_SECTIONS)
        rows = [(f"`{g}`", c) for g, c in groups.items() if g != "baseline"]
        rows += [(f"`{g}` без стресс-теста", c) for g, c in trimmed.items() if g.startswith("agent")]
        for name, counts in rows:
            common = sorted(set(counts) & set(chat))
            if common:
                out.append(_diff_row(name, counts, chat, common))
    out += _sections_table(labels)
    return out


def _diff_row(
    name: str, counts: dict[str, tuple[int, int]], chat: dict[str, tuple[int, int]], common: list[str]
) -> str:
    def diff(sample: list[str]) -> float | None:
        a, b = pooled([counts[x] for x in sample]), pooled([chat[x] for x in sample])
        return None if a is None or b is None else a - b

    d, ci = diff(common), bootstrap_ci(diff, common)
    verdict = (
        "не различимо"
        if ci is None or ci[0] <= 0 <= ci[1]
        else ("у подхода больше" if ci[0] > 0 else "у подхода меньше")
    )
    return (
        f"| {name} | {len(common)} | {_p(pooled([counts[x] for x in common]))} | "
        f"{_p(pooled([chat[x] for x in common]))} | {_pp(d)} | "
        f"{'—' if ci is None else f'{_pp(ci[0])}…{_pp(ci[1])}'} | {verdict} |"
    )


def _sections_table(labels: list[str]) -> list[str]:
    """Раздел документа → доля без опоры по каждой версии агента (повторные прогоны сложены)."""
    cells: dict[str, dict[str, list[int]]] = {}  # раздел → группа → [проверено, без опоры]
    for label in labels:
        if not label.startswith("agent"):
            continue
        for path in sorted((CENSUS_DIR / label).glob("*.json")):
            if ".s" in path.stem:
                continue
            for name, s in (
                json.loads(path.read_text(encoding="utf-8"))["claims"].get("by_section", {}).items()
            ):
                agg = cells.setdefault(name, {}).setdefault(group_of(label), [0, 0])
                agg[0] += s["verified"]
                agg[1] += s["unsupported"]
    if not cells:
        return []
    agents = sorted({g for per in cells.values() for g in per})
    out = [
        "",
        "Где у агента утверждения без опоры — по разделам документа (доля, без опоры / проверено):",
        "",
        "| Раздел | " + " | ".join(f"`{g}`" for g in agents) + " |",
        "|---|" + "---|" * len(agents),
    ]
    order = sorted(cells, key=lambda s: -sum(v[1] for v in cells[s].values()))
    for name in order:
        row = []
        for g in agents:
            n, k = cells[name].get(g, (0, 0))
            row.append(f"{_p(k / n)} ({k}/{n})" if n else "—")
        out.append(f"| {name} | " + " | ".join(row) + " |")
    return out


def fidelity_section() -> list[str]:
    """Тест смены решения по выжимке (evals/summary_fidelity.py): по разделу на каждый прогон."""
    import summary_fidelity

    out: list[str] = []
    for path in sorted((RESULTS_DIR / "summary_fidelity").glob("*.json")):
        out += summary_fidelity.section(json.loads(path.read_text(encoding="utf-8")))
    return out


def full_judges_section(a: str = "baseline", b: str = "agent", title: str = "") -> list[str]:
    path = RESULTS_DIR / "comparison" / f"{a}-vs-{b}-full" / "SUMMARY.md"
    if not path.exists():
        return []
    body = path.read_text(encoding="utf-8").split("\n", 1)[1].strip()
    return ["", f"## Судьи стенда на полных документах{title}", "", body]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--approaches", default=",".join(APPROACHES), help="подходы прежней таблицы метрик через запятую"
    )
    parser.add_argument(
        "--census",
        help="подходы раздела переписи через запятую (по умолчанию все каталоги evals/results/census/)",
    )
    args = parser.parse_args(argv)
    approaches = [x for x in args.approaches.split(",") if x]
    census = (
        [x for x in args.census.split(",") if x]
        if args.census
        else sorted(p.name for p in CENSUS_DIR.iterdir() if p.is_dir())
        if CENSUS_DIR.exists()
        else []
    )
    lines = [
        "# Сравнение: простой чат против агента",
        "",
        "Одни и те же 7 брифов и материалы, одна reasoning-модель. Метрики считает судья другого семейства",
        "(`evals/metrics.py`, 20 случайных утверждений на бриф); для агента оценивается стратегический документ без",
        "служебных разделов (самоаудит, чек стоимости, приложение-реестр). Это автоматический прокси, а не оценка",
        "эксперта: нужна выборочная ручная проверка.",
        "",
        "| Бриф | Подход | Без опоры | Противоречит | «Оставить» | «Закрыть» | Неподкр. обещания | Baseline | Guardrail | Kill с датой | Как у эксперта | Вызовов | $ | Время, с |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    totals: dict[str, dict[str, float]] = {
        a: {"n": 0, "sq": 0, "kill": 0, "guard": 0, "killd": 0, "yes": 0} for a in approaches
    }
    for brief in list_briefs():
        for a in approaches:
            if not (RESULTS_DIR / a / brief / "metrics.json").exists():
                continue  # дешёвый маршрут прогнан не на всех брифах
            r = _row(a, brief)
            lines.append(
                f"| {brief} | {a} | {_fmt(r['unsupported'])} | {_fmt(r['contradicted'])} | {_fmt(r['status_quo'])} | "
                f"{_fmt(r['kill'])} | {r['unbacked']} | {_fmt(r['baseline'])} | {_fmt(r['guardrail'])} | "
                f"{_fmt(r['kill_date'])} | {_fmt(r['expert'])} | {_fmt(r['calls'])} | "
                f"{_usd(r['usd'])} | {_fmt(r['duration'])} |"
            )
            t = totals[a]
            t["n"] += 1
            t["sq"] += bool(r["status_quo"])
            t["kill"] += bool(r["kill"])
            t["guard"] += bool(r["guardrail"])
            t["killd"] += bool(r["kill_date"])
            t["yes"] += r["expert"] == "yes"
    lines += [
        "",
        "| Подход | «Оставить» | «Закрыть» | Guardrail | Kill с датой | Выбор как у эксперта |",
        "|---|---|---|---|---|---|",
    ]
    for a, t in totals.items():
        n = int(t["n"])
        if not n:
            continue
        lines.append(
            f"| {a} | {int(t['sq'])}/{n} | {int(t['kill'])}/{n} | {int(t['guard'])}/{n} | {int(t['killd'])}/{n} | {int(t['yes'])}/{n} |"
        )
    judge = RESULTS_DIR / "comparison" / "baseline-vs-agent" / "SUMMARY.md"
    if judge.exists():
        lines += [
            "",
            "## Слепое попарное судейство",
            "",
            judge.read_text(encoding="utf-8").split("\n", 1)[1].strip(),
        ]
    lines += codex_section()
    reversal = _load(RESULTS_DIR / "comparison" / "reversal.json")
    if reversal:
        lines += [
            "",
            "## Decision Reversal",
            "",
            f"{reversal.get('summary')}. Токенов на пересмотр: {reversal.get('tokens_used')} против "
            f"{reversal.get('tokens_full_regeneration')} на полную перегенерацию.",
        ]
    runs = _load(RESULTS_DIR / "comparison" / "reversal_runs.json")
    if runs:
        s = runs["summary"]
        lines += [
            "",
            f"Повторные прогоны ({s['runs']}): решение открыто заново — {s['reopened']}, рекомендация ушла от "
            f"неприемлемой — {s['moved_away_from_unacceptable']}, совпала с экспертом — {s['matched_expert']} "
            "(`reversal_runs.json`).",
        ]
    lines += cheap_route_section()
    lines += census_section(census)
    lines += full_judges_section()
    lines += full_judges_section(b="agent-v2", title=": агент v0.2.0 (`agent-v2`)")
    lines += fidelity_section()
    out = RESULTS_DIR / "comparison" / "SUMMARY.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
