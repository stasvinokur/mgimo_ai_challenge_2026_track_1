"""`strategist demo --replay --check`: воспроизведение демо без моделей падает на промахе кассеты и на расхождении
отчёта с записанным (`make demo-check`)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from typer.testing import CliRunner

from strategist.interfaces.cli.app import app, comparable_report

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "cases" / "demo-retention"
RECORDED = ROOT / "cases" / "demo-retention-run" / "out" / "report.md"


def _cases(tmp_path: Path) -> Path:
    cases = tmp_path / "cases"
    shutil.copytree(DEMO, cases / "demo-retention", ignore=shutil.ignore_patterns("README.md", "generate.py"))
    (cases / "demo-retention-run" / "out").mkdir(parents=True)
    shutil.copy(RECORDED, cases / "demo-retention-run" / "out" / "report.md")
    return cases


def _check(cases: Path, monkeypatch) -> int:
    monkeypatch.setenv("STRATEGIST_CASES_DIR", str(cases))
    result = CliRunner().invoke(app, ["demo", "--replay", "--check", "--work-dir", str(cases / "replay")])
    return result.exit_code


def test_demo_check_passes_on_the_recorded_run_and_fails_on_a_missing_cassette(tmp_path, monkeypatch):
    cases = _cases(tmp_path)
    assert _check(cases, monkeypatch) == 0
    # кассет выжимки несколько (промпт v1 — путь без флагов, v2 — с проверкой чисел): убираем все, промах при любых флагах
    for victim in (cases / "demo-retention" / "cassettes").glob("executive_summary-*.json"):
        victim.unlink()
    assert _check(cases, monkeypatch) == 1


def test_demo_check_fails_when_the_report_differs_from_the_recorded_one(tmp_path, monkeypatch):
    cases = _cases(tmp_path)
    recorded = cases / "demo-retention-run" / "out" / "report.md"
    recorded.write_text(recorded.read_text(encoding="utf-8").replace("## Варианты", "## Другие варианты", 1))
    assert _check(cases, monkeypatch) == 1


# Отчёт воспроизведения демо при всех выключенных флагах качества (STRATEGIST_QUALITY_OFF=all) — поведение v0.1.1.
# Сверено 07.11.2026 с воспроизведением кода v0.1.1: отличие только в порядке строк чека стоимости с равной ценой
# (порядок стал детерминированным). Меняется хеш — значит, правка без флага изменила поведение v0.1.1.
V011_REPLAY_SHA256 = "9573c29c0fe47d4678304a5b2316a4957d3af39399c26955e5d9dbe2cef2fe27"


def test_with_quality_flags_off_the_demo_report_is_byte_identical_to_v011(tmp_path, monkeypatch):
    cases = _cases(tmp_path)
    monkeypatch.setenv("STRATEGIST_CASES_DIR", str(cases))
    monkeypatch.setenv("STRATEGIST_QUALITY_OFF", "all")
    work = cases / "replay"
    result = CliRunner().invoke(app, ["demo", "--replay", "--work-dir", str(work)])
    assert result.exit_code == 0, result.output
    report = (work / "demo-retention-run" / "out" / "report.md").read_bytes()
    assert hashlib.sha256(report).hexdigest() == V011_REPLAY_SHA256


def test_comparable_report_ignores_only_the_cost_check_and_revision_tokens():
    a = "# О\n\nТокенов на пересмотр: 80 076 против ~452 602\n\n## Чек стоимости\n- $0.18\n\n## Реестр\nCL-001\n"
    b = "# О\n\nТокенов на пересмотр: 61 000 против ~412 366\n\n## Чек стоимости\n- $0.00\n\n## Реестр\nCL-001\n"
    assert comparable_report(a) == comparable_report(b)
    assert comparable_report(a) != comparable_report(a.replace("CL-001", "CL-002"))


def _replay_claims(cases: Path, monkeypatch, work: str, quality_off: str | None) -> dict[str, str]:
    from strategist.infrastructure.persistence.db import CaseDatabase
    from strategist.infrastructure.persistence.repositories import SqliteCaseRepository

    monkeypatch.setenv("STRATEGIST_CASES_DIR", str(cases))
    if quality_off:
        monkeypatch.setenv("STRATEGIST_QUALITY_OFF", quality_off)
    else:
        monkeypatch.delenv("STRATEGIST_QUALITY_OFF", raising=False)
    result = CliRunner().invoke(app, ["demo", "--replay", "--work-dir", str(cases / work)])
    assert result.exit_code == 0, result.output
    case = SqliteCaseRepository(CaseDatabase(cases / work)).load("demo-retention-run")
    return {c.id: c.text for c in case.claims}


def test_metric_contract_does_not_change_any_demo_claim_text(tmp_path, monkeypatch):
    """Контракт расчёта меняет текст строки только при срабатывании; на демо-кейсе он не срабатывает —
    иначе разошлись бы входы моделей и записанные ответы демо (и сцены видео)."""
    cases = _cases(tmp_path)
    off = _replay_claims(cases, monkeypatch, "off", "metric_contract")
    on = _replay_claims(cases, monkeypatch, "on", None)
    assert on == off and len(on) > 40
