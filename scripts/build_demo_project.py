"""Демонстрационный проект для жюри (demo-project/): задача, материалы агента и итоговые стратегии одного примера.

Ничего не пишется руками. Запрос и материалы копируются как есть из cases/demo-retention. Разделы решения и
стратегий вырезаются без правок из отчёта записанного прогона cases/demo-retention-run/out/report.md. Числа в
README берутся из того же отчёта. PDF печатает Chromium (Playwright):

    uv run --with playwright==1.64.1 python scripts/build_demo_project.py
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import markdown
import yaml

ROOT = Path(__file__).resolve().parent.parent
CASE = ROOT / "cases" / "demo-retention"
REPORT = ROOT / "cases" / "demo-retention-run" / "out" / "report.md"
OUT = ROOT / "demo-project"
FULL = "отчёт-целиком.md"

# файл → (заголовок, разделы отчёта по порядку)
SECTIONS = {
    "3-решение-на-одной-странице.md": ("Решение на одной странице", ["Решение на одной странице"]),
    "4-продуктовая-стратегия.md": (
        "Продуктовая стратегия и план проверки",
        ["Продуктовая стратегия", "План проверки"],
    ),
    "5-коммуникационная-стратегия.md": ("Коммуникационная стратегия", ["Коммуникационная стратегия"]),
    "6-что-изменилось.md": ("Что изменилось после новых данных", ["Что изменилось"]),
}
SOURCE_NOTE = (
    "*Раздел отчёта агента по демо-кейсу «Скворечник» без правок. Коды вида CL-007 — строки реестра утверждений: "
    f"у каждой есть источник, они ведут в [полный отчёт]({FULL}).*"
)

CSS = """
@page { size: A4; margin: 16mm 14mm 18mm; }
body { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; font-size: 10.5pt; line-height: 1.45;
       color: #1b1e22; }
h1 { font-size: 18pt; margin: 0 0 6pt; } h2 { font-size: 14pt; margin: 18pt 0 6pt; border-bottom: 1px solid #ddd; }
h3 { font-size: 11.5pt; margin: 12pt 0 4pt; } p, li { margin: 4pt 0; } a { color: #1b1e22; text-decoration: none; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 8.5pt; page-break-inside: auto; }
th, td { border: 1px solid #ccc; padding: 3pt 4pt; vertical-align: top; overflow-wrap: break-word; min-width: 5.5em; }
th { background: #f2f2f2; text-align: left; } tr { page-break-inside: avoid; }
em { color: #555; } .break { page-break-before: always; } h1, h2, h3 { break-after: avoid-page; }
"""


def front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        head, _, body = text[4:].partition("\n---\n")
        return yaml.safe_load(head) or {}, body.lstrip("\n")
    return {}, text


def section(report: str, title: str) -> str:
    """Раздел второго уровня «## title» до следующего раздела того же уровня."""
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", report, re.S | re.M)
    assert m, f"в отчёте нет раздела «{title}»"
    return f"## {title}\n{m.group(1)}".rstrip() + "\n"


CODE = r"[A-Z]{2,6}-\d{3,4}"


def spaced(text: str) -> str:
    """Соседние ссылки на строки реестра — через пробел: «CL-069 CL-076», а не «CL-069CL-076». Слова не меняются."""
    return re.sub(rf"(\[{CODE}\](?:\([^)]*\))?)(?=\[{CODE}\])", r"\1 ", text)


def to_full_report(text: str) -> str:
    """Ссылки на строки реестра внутри раздела ведут в полный отчёт; одиночные «[CL-052]» тоже становятся ссылками."""
    text = re.sub(rf"\[({CODE})\](?!\()", r"[\1](#\1)", text)
    return spaced(re.sub(rf"\]\(#({CODE})\)", rf"]({FULL}#\1)", text))


def materials_table(folder: Path, rel: str) -> list[str]:
    rows = []
    for path in sorted(p for p in folder.iterdir() if p.is_file() and not p.name.endswith(".meta.yaml")):
        meta = front_matter(path.read_text(encoding="utf-8"))[0] if path.suffix == ".md" else {}
        side = path.with_name(path.name + ".meta.yaml")
        if side.exists():
            meta = yaml.safe_load(side.read_text(encoding="utf-8")) or {}
        if "bait" in path.name:
            origin = "приманка: страница с командой для ИИ — проверка защиты (синтетика)"
        elif meta.get("synthetic"):
            origin = "синтетика"
        else:
            origin = f"открытый источник: [{meta.get('publisher', 'ссылка')}]({meta['source_url']})"
        rows.append(f"| [{path.name}]({rel}/{path.name}) | {meta.get('title', '—')} | {origin} |")
    return rows


def plural(n: int | str, one: str, few: str, many: str) -> str:
    """82 утверждения, 17 выводов, 1 вывод."""
    n = int(n)
    d, dd = n % 10, n % 100
    return f"{n} {one if d == 1 and dd != 11 else few if 2 <= d <= 4 and not 12 <= dd <= 14 else many}"


def numbers(report: str) -> dict[str, str]:
    """Числа для README — из отчёта записанного прогона."""

    def grab(pattern: str) -> re.Match:
        m = re.search(pattern, report, re.M)
        assert m, f"в отчёте нет «{pattern}»"
        return m

    audit = grab(r"^утверждений (\d+) · без опоры (\d+%) · точность цитат (\d+%)")
    change = grab(
        r"^\*\*(\d+) новых? источник\w* → (\d+) новых строк реестра → (\d+) оснований под вопросом → "
        r"(\d+) выводов пересмотрено, (\d+) узлов не затронуто"
    )
    chosen = grab(r"^\*\*DEC-\d+\*\* · вариант \[(OPT-\d+)\]").group(1)
    return {
        "claims": audit.group(1),
        "unsupported": audit.group(2),
        "citations": audit.group(3),
        "new_sources": change.group(1),
        "revised": change.group(4),
        "untouched": change.group(5),
        "options": str(len(re.findall(r"^### \[OPT-\d+\]", report, re.M))),
        "chosen": grab(rf"^### \[{chosen}\] (.+)$").group(1),
        "premise": grab(r"посылка запроса «[^»]+» — ([^*]+)\*").group(1).strip(),
        "injections": grab(r"^\| Обнаруженные инъекции в материалах \| (\d+) \|").group(1),
        "calls": grab(r"^- Вызовов моделей: (\d+)").group(1),
        "rub": grab(r"^- Итого \(модели \+ человек\): ([\d.]+) ₽").group(1).replace(".", ","),
        "seconds": grab(r"^- Время ответа моделей: (\d+) с").group(1),
        "answer": grab(r"^\*\*Ответ:\*\* (.+)$").group(1),
    }


def readme(n: dict[str, str], start: list[str], new: list[str]) -> str:
    return f"""# Демонстрационный проект: «Скворечник» — почему падает удержание

Пример работы агента «Стратег Фабрики проектов» на одной задаче руководителя цифрового продукта: что он получил и
что выдал. Файлы ниже — запрос и материалы агента и разделы его итогового отчёта **без правок**.

> **Только открытые данные.** «Скворечник» — вымышленный продукт вымышленного оператора. Все внутренние данные
> (когорты, воронка, опрос, интервью, отчёт поддержки, паспорт продукта, A/B-тест) — синтетика, и это помечено в
> каждом файле. Реальны только четыре выдержки из открытых источников, с адресами и датами. Страница конкурента —
> подложенная приманка с командой для ИИ: так проверяется защита агента.

## Что читать

| Файл | Что внутри |
|---|---|
| [1-задача.md](1-задача.md) | запрос руководителя — как есть, своими словами |
| [2-материалы/](2-материалы/) | {plural(len(start), "файл", "файла", "файлов")}, которые получил агент, и {plural(len(new), "файл", "файла", "файлов")} новых данных (итоги A/B-теста) |
| [3-решение-на-одной-странице.md](3-решение-на-одной-странице.md) | ответ руководителю, доводы с источниками, шаги, что нужно от него |
| [4-продуктовая-стратегия.md](4-продуктовая-стратегия.md) | продуктовая стратегия, план действий и план проверки |
| [5-коммуникационная-стратегия.md](5-коммуникационная-стратегия.md) | коммуникационная стратегия и сверка «обещание = продукт» |
| [6-что-изменилось.md](6-что-изменилось.md) | что агент пересмотрел после новых данных и почему |
| [стратегии.pdf](стратегии.pdf) | решение, обе стратегии и пересмотр одним документом |
| [{FULL}]({FULL}), [отчёт-целиком.pdf](отчёт-целиком.pdf) | полный отчёт: картина ситуации, {n["options"]} вариантов с возражениями, стресс-тест, реестр утверждений с источниками, чек стоимости |

## Задача

Удержание подписки упало, руководитель уверен, что виноват онбординг, а поддержка — что цена.
{n["committee"]} — продуктовый комитет. Нужна рекомендация, что делать с продуктом и что говорить родителям, с честными вариантами,
включая «оставить как есть» и «закрыть». Бюджет и команда не растут, эффект нужно показать за квартал.

## Что получил агент

| Файл | Что это | Происхождение |
|---|---|---|
{chr(10).join(start)}

Новые данные, поданные после решения:

| Файл | Что это | Происхождение |
|---|---|---|
{chr(10).join(new)}

## Как шла работа

1. **Постановка.** Агент проверил посылку руководителя («виноват онбординг, особенно на iPhone») по данным.
   Вердикт: {n["premise"]}.
2. **Факты с источником.** В реестре {plural(n["claims"], "утверждение", "утверждения", "утверждений")}. Доля фактов без опоры — {n["unsupported"]}, точность
   цитат — {n["citations"]}. Числа считаются кодом по таблицам.
3. **Защита.** В материалах найдено попыток вмешательства: {n["injections"]}, включая команду для ИИ на странице
   конкурента. Агент их не выполнил, а записал в журнал безопасности.
4. **Варианты.** {n["options"]} содержательно разных вариантов, среди них «оставить как есть» и «закрыть». У каждого —
   сильнейшие возражения, оценки ролей и стресс-тест.
5. **Выбор и стратегии.** Выбран вариант «{n["chosen"]}». Под него написаны продуктовая и
   коммуникационная стратегии, план проверки с kill-критериями. Каждое обещание клиенту сверено с продуктом.
6. **Новые данные.** После решения пришли итоги A/B-теста ({plural(n["new_sources"], "файл", "файла", "файлов")}). Агент пересмотрел
   {plural(n["revised"], "вывод", "вывода", "выводов")}, ещё {plural(n["untouched"], "узел", "узла", "узлов")} решения не тронул и открыл
   решение заново.

В этом записанном прогоне шлюзы пройдены в режиме демо: на каждом принят вариант агента по умолчанию. В живой
работе их проходит руководитель в панели (`uv run strategist ui`): отвечает на уточнения, правит план, выбирает
вариант и утверждает решение.

## Итог

> {n["answer"]}

## Сколько это стоило

{plural(n["calls"], "вызов", "вызова", "вызовов")} моделей, {n["rub"]} ₽ по прайсу облачных моделей вместе с пересмотром, {n["seconds"]} с работы
моделей.

## Как повторить

```bash
uv sync
uv run strategist demo --replay   # тот же прогон по записанным ответам моделей, без ключей и оплаты
uv run strategist ui --demo       # то же в панели: «Открыть демо» → шлюзы → ответ → факты → отчёт
```

Как это выглядит в панели — скриншоты в [описании работы](../docs/DESCRIPTION.md#панель-руководителя).

Ключ к кейсу (что проверяет каждый файл и где в данных противоречия) — для проверяющих, в
[cases/demo-retention/README.md](../cases/demo-retention/README.md). Агенту он не подаётся.
"""


def to_pdf(sources: list[str], title: str, path: Path, base: Path | None = None, css: str = "") -> None:
    """Markdown → PDF через Chromium. base — папка, от которой считаются относительные пути картинок."""
    from playwright.sync_api import sync_playwright

    # список сразу после строки текста («**Что решать сейчас:**\n- …»): GitHub видит список, Python-Markdown — нет
    lists = [re.sub(r"(?m)^([^\n|\-\s].*)\n(- )", r"\1\n\n\2", spaced(s)) for s in sources]
    parts = [markdown.markdown(s, extensions=["tables", "sane_lists", "fenced_code"]) for s in lists]
    body = '<div class="break"></div>'.join(parts)
    html = f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{title}</title><style>{CSS}{css}</style></head><body>{body}</body></html>'
    footer = '<div style="font-size:8px;width:100%;text-align:center;color:#888"><span class="pageNumber"></span> / <span class="totalPages"></span></div>'
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        if base:  # страница из строки не читает локальные файлы — временный файл рядом с картинками
            tmp = base / f".{path.stem}.print.html"
            tmp.write_text(html, encoding="utf-8")
            try:
                page.goto(tmp.resolve().as_uri(), wait_until="networkidle")
            finally:
                tmp.unlink()
        else:
            page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=str(path),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=footer,
        )
        browser.close()


def main() -> int:
    report = REPORT.read_text(encoding="utf-8")
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "2-материалы" / "новые-данные").mkdir(parents=True)

    meta, brief = front_matter((CASE / "brief.md").read_text(encoding="utf-8"))
    (OUT / "1-задача.md").write_text(
        f"# Задача руководителя\n\n*{meta.get('note', '')}*\n\n{brief}", encoding="utf-8"
    )
    for src, dst in (
        (CASE / "materials", OUT / "2-материалы"),
        (CASE / "new_evidence", OUT / "2-материалы" / "новые-данные"),
    ):
        for f in sorted(src.iterdir()):
            if f.is_file():
                shutil.copy2(f, dst / f.name)

    pages = []
    for name, (title, titles) in SECTIONS.items():
        text = "\n".join(to_full_report(section(report, t)) for t in titles)
        if titles == [title]:  # один раздел — его заголовок и есть заголовок файла
            text = text.replace(f"## {title}\n", "", 1)
        page = f"# {title}\n\n{SOURCE_NOTE}\n\n{text}"
        (OUT / name).write_text(page, encoding="utf-8")
        pages.append(page)
    (OUT / FULL).write_text(report, encoding="utf-8")

    start = materials_table(CASE / "materials", "2-материалы")
    new = materials_table(CASE / "new_evidence", "2-материалы/новые-данные")
    n = numbers(report)
    committee = re.search(r"(\d+ [а-я]+) продуктовый комитет", brief)
    assert committee, "в запросе нет даты продуктового комитета"
    n["committee"] = committee.group(1).capitalize()
    (OUT / "README.md").write_text(readme(n, start, new), encoding="utf-8")

    if "--no-pdf" not in sys.argv:
        to_pdf(pages, "«Скворечник»: решение и стратегии", OUT / "стратегии.pdf")
        # заголовок отчёта демо — запрос целиком; в PDF он идёт абзацем под обычным заголовком, текст тот же
        cover = re.sub(
            r"\A# (.+)\n",
            r"# Отчёт агента по демо-кейсу «Скворечник»\n\n**Запрос руководителя:** \1\n",
            report,
        )
        to_pdf([cover], "«Скворечник»: отчёт агента", OUT / "отчёт-целиком.pdf")
    print(f"{OUT.relative_to(ROOT)}: {sum(1 for _ in OUT.rglob('*') if _.is_file())} файлов")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
