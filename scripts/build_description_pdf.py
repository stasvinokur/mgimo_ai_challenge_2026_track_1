"""Описание работы в PDF: docs/DESCRIPTION.md со скриншотами панели и схемой архитектуры → docs/DESCRIPTION.pdf.

Схема архитектуры — scripts/diagrams/architecture.html, Chromium снимает её в docs/img/architecture.png. Скриншоты
панели (docs/img/panel-*.png) — кадры настоящей панели на демо-кейсе. Печать — та же, что у демо-проекта:

    uv run --with playwright==1.64.1 python scripts/build_description_pdf.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from build_demo_project import to_pdf  # noqa: E402

DOCS = ROOT / "docs"
DIAGRAM = ROOT / "scripts" / "diagrams" / "architecture.html"
# картинки во всю ширину страницы, подписи под ними — мельче; код — моноширинным
CSS = """
img { max-width: 100%; border: 1px solid #ddd; border-radius: 6px; margin: 6pt 0 2pt; }
pre { background: #f6f7f9; border: 1px solid #e2e5e9; border-radius: 6px; padding: 6pt 8pt; font-size: 7.5pt;
      white-space: pre-wrap; } code { font-size: 9pt; }
"""


def render_diagram() -> Path:
    from playwright.sync_api import sync_playwright

    out = DOCS / "img" / "architecture.png"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1664, "height": 900}, device_scale_factor=2)
        page.goto(DIAGRAM.as_uri())
        page.locator("#diagram").screenshot(path=str(out))
        browser.close()
    return out


def main() -> int:
    diagram = render_diagram()
    to_pdf(
        [(DOCS / "DESCRIPTION.md").read_text(encoding="utf-8")],
        "Стратег Фабрики проектов — описание работы",
        DOCS / "DESCRIPTION.pdf",
        base=DOCS,
        css=CSS,
    )
    print(f"{diagram.relative_to(ROOT)}, docs/DESCRIPTION.pdf")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
