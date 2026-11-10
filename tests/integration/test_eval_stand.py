"""Честный стенд: полные документы у судей, защита от тихой обрезки, перепись утверждений и интервалы."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[2] / "evals"
sys.path.insert(0, str(EVALS))
sys.path.insert(0, str(EVALS / "runners"))
import codex_judge  # noqa: E402
import compare_summary  # noqa: E402
import judge  # noqa: E402
import metrics  # noqa: E402

# ----------------------------------------------------------------------------- судьи: обрезка входа


def test_truncated_input_flags_too_few_tokens_or_a_full_context():
    assert judge.truncated_input(100_000, 20_000, 131072)  # прочитано меньше, чем 100 000 / 4
    assert not judge.truncated_input(100_000, 42_000, 131072)  # обычные 2,4 символа на токен
    assert judge.truncated_input(10_000, 129_000, 131072)  # упёрлись в контекст
    assert not judge.truncated_input(100_000, 0, 131072)  # нет данных об использовании — решает ошибка


def _verdict(overall: str) -> dict:
    return {
        "criteria": [{"criterion": c, "winner": overall, "reason": ""} for c in judge.CRITERIA],
        "overall": overall,
    }


def test_truncated_or_failed_orders_are_excluded_not_counted():
    runs = [
        {"model": "m1", "forward": {"verdict": _verdict("Y")}, "reverse": {"verdict": _verdict("X")}},
        {
            "model": "m2",
            "forward": {"verdict": _verdict("Y"), "truncated": True},
            "reverse": {"verdict": _verdict("X")},
        },
        {"model": "m3", "forward": {"error": "нет JSON"}, "reverse": {"verdict": _verdict("X")}},
    ]
    summary, excluded = judge.summarize_runs(runs, "baseline", "agent")
    assert summary == {"m1": {"overall": "agent", **dict.fromkeys(judge.CRITERIA, "agent")}}
    assert excluded == [
        {"model": "m2", "reason": "truncated", "orders": ["forward"]},
        {"model": "m3", "reason": "error"},
    ]


def test_full_summary_goes_to_its_own_directory_and_lists_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "RESULTS_DIR", tmp_path)
    results = [
        {
            "brief_id": "b1",
            "chars": {"baseline": 9000, "agent": 80000},
            "summary": {"m1": {"overall": "agent", **dict.fromkeys(judge.CRITERIA, "tie")}},
            "excluded": [{"model": "m2", "reason": "truncated", "orders": ["reverse"]}],
        }
    ]
    path = judge.write_summary(results, "baseline", "agent", full=True)
    assert path == tmp_path / "comparison" / "baseline-vs-agent-full" / "SUMMARY.md"
    text = path.read_text(encoding="utf-8")
    assert "baseline — 9–9, agent — 80–80" in text
    assert "Итог по всем вердиктам: baseline — 0, agent — 1, ничья/неустойчиво — 0." in text
    assert "Не засчитано вердиктов: 1 — b1, m2: модель обрезала вход." in text


# ----------------------------------------------------------------------------- перепись утверждений


def test_split_sections_never_mixes_sections_and_skips_a_bare_title():
    long_body = "\n\n".join(f"абзац {i} " + "слово " * 40 for i in range(40))
    doc = f"# Заголовок\n\n## Решение\nкоротко\n\n## Варианты\n{long_body}\n\n## План\nметрики"
    parts = metrics.split_sections(doc, limit=2000)
    titles = [t for t, _ in parts]
    assert titles[0] == "Решение" and titles[-1] == "План"
    assert metrics.PREAMBLE not in titles  # только «# Заголовок» — нечего извлекать
    variants = [c for t, c in parts if t == "Варианты"]
    assert len(variants) > 1 and all(len(c) <= 2000 + 40 for c in variants)
    assert all(c.startswith("## Варианты (продолжение)") for c in variants[1:])
    assert all("метрики" not in c and "коротко" not in c for c in variants)


def test_preamble_with_text_is_its_own_section():
    parts = metrics.split_sections(
        "# Док\n\nВыручка выросла на 12% за квартал по данным отчёта.\n\n## Итог\nок"
    )
    assert [t for t, _ in parts] == [metrics.PREAMBLE, "Итог"]


def test_sample_zero_is_a_census_and_a_sample_is_deterministic():
    claims = [{"id": f"c{i:03d}"} for i in range(1, 31)]
    assert metrics.pick_sample(claims, 0, 42) == claims
    a, b = metrics.pick_sample(claims, 10, 42), metrics.pick_sample(claims, 10, 42)
    assert a == b and len(a) == 10 and a == sorted(a, key=lambda c: c["id"])
    assert metrics.pick_sample(claims, 10, 7) != a


def test_wilson_interval_matches_known_values():
    assert metrics.wilson(0, 0) is None
    lo, hi = metrics.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = metrics.wilson(5, 10)
    assert (lo, hi) == (pytest.approx(0.2366, abs=1e-3), pytest.approx(0.7634, abs=1e-3))


def test_by_section_counts_unsupported_and_partial():
    counted = [
        {"section": "Варианты", "verdict": "unsupported"},
        {"section": "Варианты", "verdict": "supported"},
        {"section": "Ситуация", "verdict": "partial"},
        {"verdict": "supported"},
    ]
    assert metrics.by_section(counted) == {
        "Варианты": {"verified": 2, "unsupported": 1, "partial": 0},
        "Ситуация": {"verified": 1, "unsupported": 0, "partial": 1},
        metrics.PREAMBLE: {"verified": 1, "unsupported": 0, "partial": 0},
    }


def test_census_never_writes_into_the_working_results():
    out = Path("evals/results/agent/family-pack/output.md")
    assert (
        metrics.default_target(out, "family-pack", "agent", 0, 42)
        == metrics.CENSUS_DIR / "agent" / "family-pack.json"
    )
    assert metrics.default_target(out, "family-pack", "agent", 0, 7).name == "family-pack.s7.json"
    assert metrics.default_target(out, "family-pack", "agent", 20, 42) == out.with_name("metrics.json")


# ----------------------------------------------------------------------------- сводка переписи


def _census(root: Path, label: str, counts: dict[str, tuple[int, int]], sections: dict | None = None) -> None:
    for bid, (k, n) in counts.items():
        path = root / label / f"{bid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        claims = {"unsupported": k, "verified": n, "by_section": sections or {}}
        path.write_text(json.dumps({"claims": claims}, ensure_ascii=False), encoding="utf-8")


def test_census_section_pools_reruns_and_keeps_video_rows_unambiguous(tmp_path, monkeypatch):
    monkeypatch.setattr(compare_summary, "CENSUS_DIR", tmp_path)
    briefs = {f"b{i}": (1, 20) for i in range(5)}
    _census(tmp_path, "baseline", briefs)
    _census(tmp_path, "baseline-r2", briefs)
    _census(tmp_path, "agent", briefs, {"Варианты": {"verified": 20, "unsupported": 1, "partial": 2}})
    (tmp_path / "agent" / "b0.s7.json").write_text('{"claims": {"unsupported": 20, "verified": 20}}')
    lines = compare_summary.census_section(["baseline", "baseline-r2", "agent", "missing"])
    text = "\n".join(lines)
    assert "| `agent` | 5 | 100 | 5 | 5,0% |" in text  # другой seed не попал в основной замер
    assert "| `agent` | 5 | 5,0% | 5,0% | +0,0 |" in text and "не различимо" in text
    assert "| Раздел | `agent` |" in text and "| Варианты | 5,0% (5/100) |" in text
    assert not any(x.startswith(("| baseline |", "| agent |")) for x in lines)


def test_rows_without_the_model_section_drop_only_that_section(tmp_path, monkeypatch):
    """Стресс-тест v0.2.0 подписан как расчёт модели; строка «без стресс-теста» вычитает только этот раздел."""
    monkeypatch.setattr(compare_summary, "CENSUS_DIR", tmp_path)
    briefs = {f"b{i}": (2, 20) for i in range(4)}
    _census(tmp_path, "baseline", briefs)
    stress = {"verified": 5, "unsupported": 5, "partial": 0}
    other = {"verified": 15, "unsupported": 2, "partial": 1}
    _census(
        tmp_path,
        "agent-v2",
        {f"b{i}": (7, 20) for i in range(4)},
        {"Стресс-тест решения": stress, "Варианты": other},
    )
    assert compare_summary.census_counts("agent-v2", compare_summary.MODEL_SECTIONS)["b0"] == (2, 15)
    text = "\n".join(compare_summary.census_section(["baseline", "agent-v2"]))
    assert "| `agent-v2` | 4 | 35,0% | 10,0% | +25,0 |" in text
    assert "| `agent-v2` без стресс-теста | 4 | 13,3% | 10,0% | +3,3 |" in text
    assert "| Стресс-тест решения | 100,0% (20/20) |" in text and "| Варианты | 13,3% (8/60) |" in text


def test_bootstrap_is_deterministic_and_detects_a_clear_difference(tmp_path, monkeypatch):
    monkeypatch.setattr(compare_summary, "CENSUS_DIR", tmp_path)
    _census(tmp_path, "baseline", {f"b{i}": (1, 50) for i in range(7)})
    _census(tmp_path, "agent", {f"b{i}": (10, 50) for i in range(7)})
    first = compare_summary.census_section(["baseline", "agent"])
    assert first == compare_summary.census_section(["baseline", "agent"])
    assert "у подхода больше" in "\n".join(first)
    assert compare_summary.group_of("agent-v2-r3") == "agent-v2"


# ----------------------------------------------------------------------------- Codex


def test_codex_full_compares_with_full_stand_verdicts_when_they_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_judge, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(codex_judge, "A", "baseline")
    monkeypatch.setattr(codex_judge, "B", "agent-v2")
    monkeypatch.setattr(codex_judge, "LIMIT", None)
    assert codex_judge.stand_dir().name == "baseline-vs-agent-v2"
    (tmp_path / "comparison" / "baseline-vs-agent-v2-full").mkdir(parents=True)
    assert codex_judge.stand_dir().name == "baseline-vs-agent-v2-full"
    monkeypatch.setattr(codex_judge, "LIMIT", judge.LIMIT)
    assert codex_judge.stand_dir().name == "baseline-vs-agent-v2"
