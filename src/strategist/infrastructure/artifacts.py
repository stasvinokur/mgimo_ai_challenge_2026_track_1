"""Артефакты кейса (out/ — отчёты, state/ — выгрузки) и рендер Markdown → HTML."""

from __future__ import annotations

import html
import os
from pathlib import Path

import markdown as md

CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;max-width:980px;margin:32px auto;
padding:0 20px;color:#1d1d1f;line-height:1.55}
h1{font-size:28px;border-bottom:3px solid #00b956;padding-bottom:8px}h2{margin-top:36px;border-bottom:1px solid #e5e5e5}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}th,td{border:1px solid #e0e0e0;padding:6px 8px;
vertical-align:top;text-align:left}th{background:#f6f6f6}code{background:#f3f3f3;padding:1px 4px;border-radius:3px}
a{color:#00813d}blockquote{border-left:3px solid #00b956;margin:0;padding-left:12px;color:#555}
"""


class FileArtifactWriter:
    def __init__(self, cases_dir: Path) -> None:
        self.cases_dir = Path(cases_dir)

    def _path(self, case_id: str, rel_path: str) -> Path:
        path = (self.cases_dir / case_id / rel_path).resolve()
        if self.cases_dir.resolve() / case_id not in path.parents:
            raise ValueError(f"Путь артефакта вне каталога кейса: {rel_path}")
        return path

    def write(self, case_id: str, rel_path: str, content: str) -> str:
        path = self._path(case_id, rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        return str(path)

    def read(self, case_id: str, rel_path: str) -> str | None:
        path = self._path(case_id, rel_path)
        return path.read_text(encoding="utf-8") if path.exists() else None


class MarkdownRenderer:
    def to_html(self, markdown_text: str, title: str) -> str:
        body = md.markdown(markdown_text, extensions=["tables", "fenced_code", "sane_lists"])
        return (
            f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f"<style>{CSS}</style></head><body>{body}</body></html>"
        )
