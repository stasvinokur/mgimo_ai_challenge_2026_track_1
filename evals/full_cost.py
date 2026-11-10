"""Полная стоимость решения брифа: API + минуты человека по ставке + переделки (TASK-22, сценарий GDPval).

Машинное время и API — из прогонов стенда (run.json), совпадение с экспертом — из metrics.json, минуты
человека и ставка — из evals/cost_assumptions.yaml (средние из открытых источников, а не замер).

    uv run python evals/full_cost.py      → evals/results/comparison/FULL_COST.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RESULTS_DIR, ROOT, list_briefs

RUB_PER_USD = float(
    yaml.safe_load((ROOT / "config" / "prices.yaml").read_text())["currency_rate_rub_per_usd"]
)


def _int(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def _dec(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def measured(approach: str) -> dict:
    """Средние по брифам: машинное время, API в $, доля полного совпадения выбора с экспертом."""
    secs, usd, match, n = [], [], 0, 0
    for brief in list_briefs():
        run = _load(RESULTS_DIR / approach / brief / "run.json")
        metrics = _load(RESULTS_DIR / approach / brief / "metrics.json")
        if not run:
            continue
        n += 1
        secs.append(float(run.get("duration_s") or 0))
        cost = (run.get("costs") or {}).get("usd_list_price")
        usd.append(float(cost if cost is not None else (run.get("cost_estimate") or {}).get("usd") or 0))
        match += (metrics.get("expected_choice") or {}).get("expected_match") == "yes"
    return {"n": n, "minutes": sum(secs) / n / 60, "usd": sum(usd) / n, "w": match / n} if n else {}


def total(machine_min: float, gate_min: float, a: dict, w: float, usd: float) -> tuple[float, float]:
    review = a["review_minutes_per_deliverable"]
    redo = (1 - w) * a["analyst_minutes_per_task"]
    minutes = machine_min + gate_min + review + redo
    rub = (gate_min + review + redo) / 60 * a["human_rate_rub_per_hour"] + usd * RUB_PER_USD
    return minutes, rub


def _times(x: float) -> str:
    """110 → «в 110 раз», 872 → «в 872 раза»."""
    k = round(x)
    form = "раза" if k % 10 in (2, 3, 4) and not 12 <= k % 100 <= 14 else "раз"
    return f"в {_int(k)} {form}"


def main() -> int:
    a = yaml.safe_load((Path(__file__).parent / "cost_assumptions.yaml").read_text(encoding="utf-8"))
    rate, analyst_min = a["human_rate_rub_per_hour"], a["analyst_minutes_per_task"]
    analyst_rub = analyst_min / 60 * rate
    chat, agent = measured("baseline"), measured("agent")
    w_cons = a["redo_scenarios"]["conservative"]["w"]
    rows = []
    for name, m, gates in (("Простой чат", chat, 0), ("Агент", agent, a["agent_gate_minutes"])):
        for scenario, w in (("оптимистичный", m["w"]), ("консервативный", w_cons)):
            minutes, rub = total(m["minutes"], gates, a, w, m["usd"])
            rows.append((name, scenario, m, gates, w, minutes, rub))
    lines = [
        "# Полная стоимость решения брифа: API + минуты человека + переделки",
        "",
        "Аналитика на брифах стенда не прогоняли: время эксперта, время проверки и ставка — средние значения из",
        "открытых источников (`evals/cost_assumptions.yaml`). Машинное время, API и совпадение выбора с экспертом —",
        f"из прогонов стенда (среднее по {agent.get('n', 0)} брифам). Курс {RUB_PER_USD:g} ₽/$, ставка {rate:g} ₽/ч.",
        "Формула — сценарий GDPval «Try 1x, then fix»: машина + шлюзы + проверка + (1 − w) × время эксперта.",
        "",
        "| Подход | Сценарий переделок | Машина, мин | Шлюзы, мин | Проверка, мин | Переделка, мин (w) | Всего, мин | API, ₽ | Всего, ₽ | Быстрее аналитика | Дешевле аналитика |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        f"| Аналитик (оценка) | — | — | — | — | — | {_int(analyst_min)} | 0 | {_int(analyst_rub)} | 1,0× | 1,0× |",
    ]
    for name, scenario, m, gates, w, minutes, rub in rows:
        redo = (1 - w) * analyst_min
        lines.append(
            f"| {name} | {scenario} | {_dec(m['minutes'])} | {_int(gates)} | {_int(a['review_minutes_per_deliverable'])} | "
            f"{_int(redo)} (w = {_dec(w, 2)}) | {_int(minutes)} | {_dec(m['usd'] * RUB_PER_USD)} | {_int(rub)} | "
            f"{_dec(analyst_min / minutes)}× | {_dec(analyst_rub / rub)}× |"
        )
    naive = {
        name: (analyst_min / m["minutes"], analyst_rub / max(m["usd"] * RUB_PER_USD, 1e-9))
        for name, m in (("чат", chat), ("агент", agent))
    }
    lines += [
        "",
        f"Без проверки и переделок («наивно») агент быстрее аналитика {_times(naive['агент'][0])} и дешевле "
        f"{_times(naive['агент'][1])}, чат — быстрее {_times(naive['чат'][0])} и дешевле {_times(naive['чат'][1])}.",
        "Но полную стоимость задаёт",
        "время человека: проверка результата и доля переделок. Это тот же вывод, что в GDPval: у GPT-5 наивно 90× по",
        "времени и 474× по стоимости, с проверкой и переделкой — 1,12× и 1,18× (прил. A.2.1, табл. 2).",
        "Чат и агент здесь почти равны по деньгам: у обоих одинаковое время проверки (данных, что реестр и",
        "самоаудит агента ускоряют проверку, нет) и близкая доля совпадения с экспертом. Преимущество агента —",
        "в качестве и проверяемости (см. SUMMARY.md), а не в цене.",
        "",
        "Источники допущений:",
        "",
        *[
            f"- **{key}**: {a[key + '_source'].strip()}"
            for key in ("analyst_minutes", "review_minutes", "agent_gate_minutes", "human_rate")
        ],
    ]
    out = RESULTS_DIR / "comparison" / "FULL_COST.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
