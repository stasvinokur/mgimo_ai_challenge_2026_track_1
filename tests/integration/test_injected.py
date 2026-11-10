"""Подложенные числовые ошибки: правки корректны, набор детерминирован, holdout не считается без явного флага."""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parents[2] / "evals" / "injected"
sys.path.insert(0, str(EVALS))
import build  # noqa: E402
import mutate as mu  # noqa: E402
import score  # noqa: E402

from factories import add_claim, add_option, empty_case  # noqa: E402
from strategist.domain.entities import OptionKind  # noqa: E402


def _q(text: str) -> mu.Qty:
    return mu.quantities(text)[0]


def test_quantities_understand_units_thousands_and_signs():
    qs = mu.quantities("Цена 1 234 ₽, рост −3,2 п.п., доля 13,4%, срок 1,34 мес., 8 дней; 2026-01: 62.6")
    assert [(q.value, q.kind) for q in qs] == [
        (1234.0, "rub"),
        (-3.2, "pp"),
        (13.4, "pct"),
        (1.34, "month"),
        (8.0, "day"),
    ]


def test_errors_change_the_meaning_and_benign_edits_keep_it():
    s = "Удержание M1 — 84,1% [CL-007]."
    q = _q(s)
    assert mu.value_shift(s, q, [84.1]) == "Удержание M1 — 58,9% [CL-007]."
    assert mu.pct_pp(s, q) == "Удержание M1 — 84,1 п.п. [CL-007]."
    assert mu.churn_label(s, q) == "отток M1 — 84,1% [CL-007]."
    assert mu.cohort_swap(s) == "Удержание M3 — 84,1% [CL-007]."
    assert mu.no_ref(s) == "Удержание M1 — 84,1% ."
    assert mu.rounding(s, q) == "Удержание M1 — 84% [CL-007]."
    assert mu.target(s, q, [84.1]) == "Удержание M1 — 84,1% [CL-007]; цель — не ниже 50%."
    assert mu.id_codes(s) == "Удержание M1 — 84,1% [CL-007]; подробнее — OPT-003, SRC-005 и EXP-001."
    price = "Цена выросла с 399 ₽ до 479 ₽ [CL-001]."
    pq = mu.quantities(price)
    assert mu.derived_diff(price, pq) == "Цена выросла с 399 ₽ до 479 ₽ [CL-001] — разница 80 ₽."
    assert mu.derived_ratio(price, pq) == "Цена выросла с 399 ₽ до 479 ₽ [CL-001], то есть на 20,1% выше."
    assert mu.thousands("Чек 2590 ₽ [CL-2].", _q("Чек 2590 ₽ [CL-2].")) == "Чек 2 590 ₽ [CL-2]."
    assert mu.lost_minus("Сдвиг −3,2 п.п. [CL-3].", _q("Сдвиг −3,2 п.п. [CL-3].")) == "Сдвиг 3,2 п.п. [CL-3]."
    assert mu.days_months("Срок 1,34 мес. [CL-4].", _q("Срок 1,34 мес. [CL-4].")) == "Срок 1,34 дня [CL-4]."


def test_wrong_ref_points_to_a_row_without_that_number():
    s = "Отток 16% [CL-002]."
    out = mu.wrong_ref(
        s, _q(s), {"CL-001": "Отток 16%", "CL-002": "Отток 16%", "CL-003": "Цена 399 ₽"}, ["CL-002"]
    )
    assert out == "Отток 16% [CL-003]."


def _case():
    case = empty_case("eval-x")
    c1 = add_claim(case, "Удержание M1 составило 84,1%, цена 399 ₽ и 479 ₽")
    c2 = add_claim(case, "Средний срок до отмены 1,34 месяца")
    add_option(
        case,
        OptionKind.STATUS_QUO,
        rationale=f"Удержание M1 — 84,1% [{c1.id}], а срок до отмены 1,34 месяца [{c2.id}]. Цена выросла с 399 ₽ до 479 ₽ [{c1.id}].",
    )
    return case


def test_build_is_deterministic_and_keeps_originals_passing_v1():
    first = build.build_from_case("family-pack", _case(), ["Знали о пакете 57%"])
    second = build.build_from_case("family-pack", _case(), ["Знали о пакете 57%"])
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)
    kinds = {it["kind"] for it in first["items"]}
    assert {"value_shift", "pct_pp", "days_months", "original", "rounding", "derived_diff"} <= kinds
    assert first["split"] == "dev" and all(it["split"] == "dev" for it in first["items"])
    originals = [it for it in first["items"] if it["kind"] == "original"]
    assert originals and not any(score.checker_v1(it["text"], it, first) for it in originals)
    assert not any(score.checker_v2(it["text"], it, first) for it in originals)


def test_holdout_is_not_scored_by_default(tmp_path, monkeypatch, capsys):
    data = build.build_from_case("app-onboarding", _case(), [])
    assert data["split"] == "holdout"
    (tmp_path / "app-onboarding.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(score, "DATA", tmp_path)
    monkeypatch.setattr(score, "OUT", tmp_path / "out")
    assert score.main(["--checker", "v1"]) == 1  # dev пуст — holdout сам не подставляется
    assert score.main(["--checker", "v1", "--split", "holdout"]) == 0
    assert "Полнота на ошибках" in capsys.readouterr().out


def test_metric_contract_fixes_every_calculation_mutant_on_dev_and_spares_benign_specs():
    """evals/injected/calc/dev.json — ошибки смысла в спецификациях расчётов на настоящих таблицах dev."""
    import calc

    data = json.loads(calc.OUT.read_text(encoding="utf-8"))
    result = calc.score(data)
    assert result["misses"] == []
    assert sum(r["n"] for r in result["rows"].values() if r["expect"] != "—") >= 25
