"""Общие помощники для HTML: декодирование, основной текст (trafilatura) и запасной вариант без тегов.

Используются и парсером сохранённых HTML-файлов, и веб-загрузчиком. Текст страницы — недоверенные
данные: здесь он только извлекается, никакой интерпретации содержимого нет.
"""

from __future__ import annotations

import codecs
import html as html_lib
import re
from html.parser import HTMLParser

import trafilatura

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.IGNORECASE)
_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SPACES = re.compile(r"[ \t\r\f\v ]+")

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head", "iframe", "object"}
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}


def _known_codec(name: str) -> str | None:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def decode_html(data: bytes, content_type: str | None = None) -> str:
    """Кодировка: заголовок Content-Type → <meta charset> → UTF-8 → cp1251 (частый случай рунета)."""
    candidates: list[str] = []
    if content_type:
        m = _HEADER_CHARSET.search(content_type)
        if m:
            candidates.append(m.group(1))
    m_meta = _META_CHARSET.search(data[:4096])
    if m_meta:
        candidates.append(m_meta.group(1).decode("ascii", errors="ignore"))
    candidates += ["utf-8", "cp1251"]
    for name in candidates:
        codec = _known_codec(name)
        if codec is None:
            continue
        try:
            text = data.decode(codec)
        except UnicodeDecodeError:
            continue
        return text.lstrip("﻿")
    return data.decode("utf-8", errors="replace")


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def strip_tags(html: str) -> str:
    """Запасной вариант: текст без тегов, скриптов и стилей; абзацы — по блочным элементам."""
    collector = _TextCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # битая разметка — грубое удаление тегов
        return "\n".join(_paragraph_lines(html_lib.unescape(re.sub(r"<[^>]+>", "\n", html))))
    return "\n".join(_paragraph_lines("".join(collector.parts)))


def _paragraph_lines(text: str) -> list[str]:
    lines = (_SPACES.sub(" ", line).strip() for line in text.splitlines())
    return [line for line in lines if line]


def main_text_paragraphs(html: str) -> list[str]:
    """Абзацы основного текста: trafilatura (без меню, подвалов и скриптов), иначе — текст без тегов."""
    extracted = None
    try:
        extracted = trafilatura.extract(
            html, include_comments=False, include_tables=True, include_images=False, deduplicate=False
        )
    except Exception:
        extracted = None
    if extracted and extracted.strip():
        return _paragraph_lines(extracted)
    return _paragraph_lines(strip_tags(html))


def html_title(html: str) -> str:
    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        title = (meta.title or "") if meta is not None else ""
    except Exception:
        title = ""
    if not title:
        m = _TITLE.search(html)
        if m:
            title = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
    return _SPACES.sub(" ", title).strip()
