"""Панель в браузере (Playwright): демо по записанным ответам моделей — от «Открыть демо» через четыре шлюза до ответа,
фактов, отчёта и пересмотра по новым данным.

Запуск: `make ui-check`. Моделей не зовёт: `strategist ui --demo` воспроизводит записанный прогон, ответы на шлюзы —
те же, что у демо без панели. По умолчанию тест пропускается (маркер `browser`, Playwright не входит в зависимости).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser
sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
WAIT = 180_000  # мс: шаг воспроизведения с шлюзом занимает секунды, запас — на медленную машину


def T(name: str) -> str:
    return f'[data-testid="{name}"]'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def panel(tmp_path):
    cases = tmp_path / "cases"
    shutil.copytree(ROOT / "cases" / "demo-retention", cases / "demo-retention")
    port = _free_port()
    env = {**os.environ, "STRATEGIST_CASES_DIR": str(cases)}
    server = subprocess.Popen(
        [sys.executable, "-m", "strategist.interfaces.cli.app", "ui", "--demo", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while True:
        try:
            with urllib.request.urlopen(f"{url}/api/meta", timeout=2):
                break
        except OSError:
            if time.monotonic() > deadline or server.poll() is not None:
                server.kill()
                pytest.fail("панель не поднялась")
            time.sleep(0.3)
    yield url
    server.terminate()
    server.wait(timeout=20)


def test_demo_goes_from_open_to_revision_in_the_browser(panel):
    errors: list[str] = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800}, locale="ru-RU")
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(panel)

        page.click(T("open-demo"))
        page.click(T("run"))
        gates = []
        while True:
            page.locator(f"{T('gate-card')}, {T('answer')}").first.wait_for(timeout=WAIT)
            if page.locator(T("answer")).count():
                break
            kind = page.locator(T("gate-card")).get_attribute("data-gate")
            gates.append(kind)
            page.click(T("gate-primary"))
            page.locator(f'{T("gate-card")}[data-gate="{kind}"]').wait_for(state="detached", timeout=WAIT)
        assert gates == ["clarify", "research_plan", "option_choice", "decision_commit"]
        assert page.locator(T("answer-text")).inner_text().strip()
        assert page.locator(f"{T('answer-reasons')} li").count() >= 2

        # факты: паспорт строки и «что если неверно» — без моделей и без изменения кейса
        page.click(T("tab-facts"))
        page.locator(T("fact-row")).first.wait_for()
        assert page.locator(T("fact-row")).count() > 10
        page.locator(T("fact-row")).first.click()
        page.click(T("fact-challenge"))
        page.locator(T("fact-impact")).wait_for()
        page.keyboard.press("Escape")

        # отчёт: ссылки на факты — чипы, по клику открывается факт
        page.click(T("tab-report"))
        page.locator(f'{T("report")} h2:has-text("Продуктовая стратегия")').wait_for()
        assert page.locator(f"{T('report')} {T('claim-chip')}").count() > 10
        page.locator(f"{T('report')} {T('claim-chip')}").first.click()
        page.locator(T("fact-sheet")).wait_for()
        page.keyboard.press("Escape")

        # новые данные: в демо — итоги A/B-теста из записи; пересмотр открывает решение заново
        page.click(T("tab-decision"))
        page.click(T("add-data"))
        page.click(T("add-data-submit"))
        page.locator(T("revision-banner")).wait_for(timeout=WAIT)
        assert "Пересмотрено выводов" in page.locator(T("revision-banner")).inner_text()
        page.click(T("revision-toggle"))
        assert page.locator(T("revision-card")).count() > 0
        browser.close()
    assert not errors, errors
