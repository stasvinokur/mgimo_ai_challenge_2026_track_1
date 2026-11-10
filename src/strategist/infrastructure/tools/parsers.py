"""Парсеры документов: MD, TXT, DOCX, PDF, HTML → фрагменты с точным адресом (Locator).

Фрагмент — единица цитирования: абзац (строка таблицы markdown / строка таблицы DOCX). Текст фрагмента
не обрезается и не переписывается: цитаты потом сверяются с ним как подстроки. Номера строк markdown
считаются по исходному файлу — вместе с YAML front matter, чтобы адрес совпадал с тем, что видит человек.
"""

from __future__ import annotations

import itertools
import re
import statistics
from pathlib import Path
from typing import Any

import yaml

from strategist.application.ports import DocumentParser, Fragment, ParsedDocument
from strategist.domain.entities import Locator
from strategist.infrastructure.tools.html_text import decode_html, html_title, main_text_paragraphs

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_NO_SPACE = re.compile(r"\s+")


def read_text_file(path: str | Path) -> str:
    """UTF-8 (с BOM или без), иначе cp1251 — частая кодировка русских текстов."""
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def split_front_matter(lines: list[str]) -> tuple[dict[str, Any], int]:
    """YAML front matter → (метаданные, индекс первой строки тела). Без front matter — ({}, 0)."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            try:
                data = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                data = {}
            return (data if isinstance(data, dict) else {}), i + 1
    return {}, 0


def _line_fragments(lines: list[str], start: int, *, markdown: bool) -> list[Fragment]:
    """Абзацы = непустые строки подряд; в markdown заголовок и строка таблицы — отдельные фрагменты.

    `start` — индекс первой строки тела в исходном файле; номера строк в Locator — 1-based по файлу.
    """
    fragments: list[Fragment] = []
    block: list[str] = []
    block_start = 0
    in_fence = False

    def flush(end_line: int) -> None:
        nonlocal block
        text = "\n".join(block).strip("\n")
        if text.strip():
            fragments.append(Fragment(text=text, locator=Locator(line_start=block_start, line_end=end_line)))
        block = []

    for idx in range(start, len(lines)):
        line_no = idx + 1
        line = lines[idx].rstrip()
        if markdown and _FENCE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if block:
                flush(line_no - 1)
            continue
        if markdown and not in_fence and (_HEADING.match(line) or line.lstrip().startswith("|")):
            if block:
                flush(line_no - 1)
            if _TABLE_SEPARATOR.match(line):
                continue  # строка-разделитель таблицы |---|---| — не содержит сведений
            block, block_start = [line], line_no
            flush(line_no)
            continue
        if not block:
            block_start = line_no
        block.append(line)
    if block:
        flush(len(lines))
    return fragments


class MarkdownParser:
    """Markdown: front matter вырезается, но номера строк — по исходному файлу."""

    extensions = (".md", ".markdown")

    def supports(self, path: str) -> bool:
        return str(path).lower().endswith(self.extensions)

    def parse(self, path: str) -> ParsedDocument:
        lines = read_text_file(path).splitlines()
        meta, body_start = split_front_matter(lines)
        fragments = _line_fragments(lines, body_start, markdown=True)
        title = str(meta.get("title") or "").strip()
        if not title:
            for line in lines[body_start:]:
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        return ParsedDocument(
            path=str(path),
            title=title or Path(path).stem,
            kind="document",
            fragments=fragments,
            meta={"front_matter": meta, "format": "markdown"} if meta else {"format": "markdown"},
        )


class PlainTextParser:
    """Обычный текст: абзацы (непустые строки подряд) с номерами строк."""

    def supports(self, path: str) -> bool:
        return str(path).lower().endswith(".txt")

    def parse(self, path: str) -> ParsedDocument:
        lines = read_text_file(path).splitlines()
        return ParsedDocument(
            path=str(path),
            title=Path(path).stem,
            kind="document",
            fragments=_line_fragments(lines, 0, markdown=False),
            meta={"format": "text"},
        )


class DocxParser:
    """DOCX: абзацы и строки таблиц в порядке документа.

    Нумерация блоков сквозная: каждый непустой абзац и каждая таблица — очередной номер `paragraph`;
    строки таблицы получают номер своей таблицы и адрес `cell="таблица T, строка R"`.
    """

    def supports(self, path: str) -> bool:
        return str(path).lower().endswith(".docx")

    def parse(self, path: str) -> ParsedDocument:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = Document(path)
        fragments: list[Fragment] = []
        block_no = 0
        table_no = 0
        heading = ""
        for block in doc.iter_inner_content():
            if isinstance(block, Paragraph):
                text = block.text
                if not text.strip():
                    continue
                block_no += 1
                style = (block.style.name if block.style is not None else "") or ""
                if not heading and style.lower().startswith(("heading", "title", "заголовок", "название")):
                    heading = text.strip()
                fragments.append(Fragment(text=text, locator=Locator(paragraph=block_no)))
            elif isinstance(block, Table):
                block_no += 1
                table_no += 1
                for row_no, row in enumerate(block.rows, start=1):
                    cells: list[str] = []
                    seen: set[int] = set()
                    for cell in row.cells:
                        if id(cell._tc) in seen:  # объединённые ячейки python-docx повторяет
                            continue
                        seen.add(id(cell._tc))
                        cells.append(cell.text.strip())
                    if not any(cells):
                        continue
                    fragments.append(
                        Fragment(
                            text=" | ".join(cells),
                            locator=Locator(paragraph=block_no, cell=f"таблица {table_no}, строка {row_no}"),
                        )
                    )
        title = (doc.core_properties.title or "").strip() or heading or Path(path).stem
        return ParsedDocument(
            path=str(path), title=title, kind="document", fragments=fragments, meta={"format": "docx"}
        )


def _pdf_lines(page: Any) -> list[tuple[float | None, str]]:
    """Строки страницы с вертикальной координатой (в системе устройства) — для поиска границ абзацев."""
    parts: list[tuple[str, float | None]] = []

    def visitor(text: str, cm: list[float], tm: list[float], font: Any, size: float) -> None:
        try:
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        except (TypeError, IndexError):
            y = None
        parts.append((text, y))

    output = page.extract_text(visitor_text=visitor) or ""
    if _NO_SPACE.sub("", "".join(t for t, _ in parts)) != _NO_SPACE.sub("", output):
        # куски от visitor не сложились в тот же текст — координатам не доверяем, делим по пустым строкам
        return [(None, line) for line in output.splitlines()]
    lines: list[tuple[float | None, str]] = []
    current = ""
    current_y: float | None = None
    started = False
    for text, y in parts:
        if not text:
            continue
        segments = text.split("\n")
        for i, segment in enumerate(segments):
            if segment and not started:
                current_y, started = y, True
            current += segment
            if i < len(segments) - 1:
                lines.append((current_y, current))
                current, current_y, started = "", None, False
    if current:
        lines.append((current_y, current))
    return lines


def _pdf_paragraphs(lines: list[tuple[float | None, str]]) -> list[str]:
    """Граница абзаца: пустая строка, вертикальный разрыв больше обычного межстрочного или переход вверх
    (новая колонка/блок)."""
    ys = [y for y, _ in lines]
    gaps = [
        prev - cur
        for prev, cur in itertools.pairwise(ys)
        if prev is not None and cur is not None and prev - cur > 0.5
    ]
    typical = statistics.median(gaps) if gaps else None
    paragraphs: list[list[str]] = []
    block: list[str] = []
    prev_y: float | None = None
    for y, text in lines:
        if not text.strip():
            if block:
                paragraphs.append(block)
            block, prev_y = [], y
            continue
        new_paragraph = False
        if block and prev_y is not None and y is not None and typical:
            gap = prev_y - y
            new_paragraph = gap > typical * 1.4 or gap < -0.5
        if new_paragraph:
            paragraphs.append(block)
            block = []
        block.append(text.rstrip())
        prev_y = y
    if block:
        paragraphs.append(block)
    return ["\n".join(p) for p in paragraphs if "".join(p).strip()]


class PdfParser:
    """PDF (текстовый слой): фрагменты — абзацы внутри страницы, Locator(page=P, paragraph=N)."""

    def supports(self, path: str) -> bool:
        return str(path).lower().endswith(".pdf")

    def parse(self, path: str) -> ParsedDocument:
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError(f"{Path(path).name}: PDF зашифрован, текст недоступен")
        fragments: list[Fragment] = []
        for page_no, page in enumerate(reader.pages, start=1):
            try:
                paragraphs = _pdf_paragraphs(_pdf_lines(page))
            except Exception:  # нестандартный поток — простое извлечение и деление по пустым строкам
                text = page.extract_text() or ""
                paragraphs = [p.strip("\n") for p in re.split(r"\n\s*\n", text) if p.strip()]
            for para_no, text in enumerate(paragraphs, start=1):
                fragments.append(Fragment(text=text, locator=Locator(page=page_no, paragraph=para_no)))
        title = ""
        try:
            title = str((reader.metadata or {}).get("/Title") or "").strip()
        except Exception:
            title = ""
        meta = {"format": "pdf", "pages": len(reader.pages)}
        if not fragments:
            meta["warning"] = "нет текстового слоя (скан?) — нужен OCR"
        return ParsedDocument(
            path=str(path), title=title or Path(path).stem, kind="document", fragments=fragments, meta=meta
        )


class HtmlParser:
    """Сохранённая HTML-страница: основной текст через trafilatura, абзацы с Locator(paragraph=N)."""

    def supports(self, path: str) -> bool:
        return str(path).lower().endswith((".html", ".htm"))

    def parse(self, path: str) -> ParsedDocument:
        html = decode_html(Path(path).read_bytes())
        paragraphs = main_text_paragraphs(html)
        fragments = [
            Fragment(text=text, locator=Locator(paragraph=no)) for no, text in enumerate(paragraphs, start=1)
        ]
        return ParsedDocument(
            path=str(path),
            title=html_title(html) or Path(path).stem,
            kind="web_snapshot",
            fragments=fragments,
            meta={"format": "html"},
        )


def default_parsers() -> list[DocumentParser]:
    return [MarkdownParser(), PlainTextParser(), DocxParser(), PdfParser(), HtmlParser()]
