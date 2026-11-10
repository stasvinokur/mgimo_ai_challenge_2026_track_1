"""Тест смены решения по выжимке: разбор отчёта и сводка без вызова моделей."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))
import summary_fidelity as sf


def test_executive_section_is_cut_from_the_report():
    report = "# К\n\n## Решение на одной странице\n\n**Ответ:** снимаем барьер.\n\n## Панель самоаудита\n…"
    assert sf.executive_section(report) == "**Ответ:** снимаем барьер."
    assert sf.executive_section("# без выжимки") == ""


def test_section_reports_n_and_noise():
    data = {
        "summary": {
            "label": "agent-v2",
            "n": 7,
            "order_sensitive": 1,
            "ours_matches_full": 6,
            "one_pass_matches_full": 4,
            "full_matches_selected": 7,
            "ours_matches_selected": 6,
            "one_pass_matches_selected": 4,
        }
    }
    text = "\n".join(sf.section(data))
    assert "n = 7" in text and "| 6 из 7 |" in text and "Иллюстрация, а не доказательство" in text


def test_section_shows_clef_confidence_per_text_when_rows_are_present():
    rows = [
        {"full": {"confidence": 0.98}, "ours": {"confidence": 0.9}, "one_pass": {"confidence": 0.8}},
        {"full": {"confidence": 0.96}, "ours": {"confidence": 0.94}, "one_pass": None},
    ]
    summary = {
        "label": "agent-v2",
        "n": 2,
        "order_sensitive": 0,
        "ours_matches_full": 2,
        "one_pass_matches_full": 1,
        "full_matches_selected": 2,
        "ours_matches_selected": 2,
        "one_pass_matches_selected": 1,
    }
    text = "\n".join(sf.section({"summary": summary, "rows": rows}))
    assert "| Полный документ | 0,97 | 0,96 |" in text
    assert "| Наша выжимка | 0,92 | 0,90 |" in text
    assert "| Выжимка одним проходом | 0,80 | 0,80 |" in text
    assert "рекомендация в нём названа прямо" in text


class _Decider:
    def __init__(self, choice: str, fallback=None):
        self.choice, self.fallback, self.asked = choice, fallback, 0

    def decide(self, question):
        from types import SimpleNamespace

        self.asked += 1
        return SimpleNamespace(choice=self.choice, confidence=0.9)


def test_documents_longer_than_clef_context_go_through_the_fallback():
    from types import SimpleNamespace

    fallback = _Decider("OPT-002")
    clef = _Decider("OPT-001", fallback=fallback)
    app = SimpleNamespace(ctx=SimpleNamespace(decision=clef))
    case = SimpleNamespace(id="eval-x")
    options = {"OPT-001": "оставить", "OPT-002": "закрыть"}
    short = sf._decide(app, case, "короткий документ", options)
    long = sf._decide(app, case, "я" * (sf.MAX_CLEF_CHARS + 1), options)
    assert (short["path"], short["choice"]) == ("clef", "OPT-001")
    assert (long["path"], long["choice"]) == ("fallback", "OPT-002")
    assert (clef.asked, fallback.asked) == (1, 1)
