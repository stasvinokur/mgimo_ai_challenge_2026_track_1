"""Неизменяемые материалы кейса: копия в cases/<id>/materials, SHA-256 в manifest.json.

Метаданные — из соседнего `<файл>.meta.yaml` или YAML front matter markdown-файла.
Файл с тем же именем, но другим содержимым не перезаписывает исходный: сохраняется как `<имя>-v2`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from strategist.application.ports import MaterialFile

TABLE_EXT = {".csv", ".xlsx", ".xls", ".tsv"}
WEB_EXT = {".html", ".htm"}
META_SUFFIX = ".meta.yaml"
MANIFEST = "manifest.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_front_matter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def read_meta(path: Path) -> dict[str, Any]:
    sidecar = path.with_name(path.name + META_SUFFIX)
    meta: dict[str, Any] = {}
    if sidecar.exists():
        loaded = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            meta.update(loaded)
    if path.suffix.lower() in {".md", ".markdown"}:
        try:
            fm = read_front_matter(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            fm = {}
        for key, value in fm.items():
            meta.setdefault(key, value)
    return meta


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None


def _default_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in TABLE_EXT:
        return "table"
    if ext in WEB_EXT:
        return "web_snapshot"
    return "document"


def _title(path: Path, meta: dict[str, Any]) -> str:
    if meta.get("title"):
        return str(meta["title"])
    if path.suffix.lower() == ".md":
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return path.stem


class FileMaterialStore:
    def __init__(self, cases_dir: Path, clock: Any) -> None:
        self.cases_dir = Path(cases_dir)
        self.clock = clock

    def materials_dir(self, case_id: str) -> Path:
        return self.cases_dir / case_id / "materials"

    def _manifest_path(self, case_id: str) -> Path:
        return self.materials_dir(case_id) / MANIFEST

    def _load_manifest(self, case_id: str) -> list[MaterialFile]:
        path = self._manifest_path(case_id)
        if not path.exists():
            return []
        return [MaterialFile.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]

    def _save_manifest(self, case_id: str, items: list[MaterialFile]) -> None:
        path = self._manifest_path(case_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([i.model_dump(mode="json") for i in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _entry(self, rel: str, data: bytes, meta: dict[str, Any], path_for_defaults: Path) -> MaterialFile:
        known = {"title", "kind", "synthetic", "source_url", "publisher", "published", "retrieved", "note"}
        return MaterialFile(
            path=rel,
            sha256=sha256_bytes(data),
            size=len(data),
            title=_title(path_for_defaults, meta),
            kind=str(meta.get("kind") or _default_kind(path_for_defaults)),
            synthetic=bool(meta.get("synthetic", False)),
            source_url=meta.get("source_url") or meta.get("url"),
            publisher=meta.get("publisher"),
            published=_date(meta.get("published")),
            retrieved=_date(meta.get("retrieved")),
            note=str(meta.get("note") or ""),
            added_at=self.clock.now(),
            meta={k: v for k, v in meta.items() if k not in known},
        )

    def _free_name(self, directory: Path, name: str) -> Path:
        target = directory / name
        stem, suffix = Path(name).stem, Path(name).suffix
        n = 2
        while target.exists():
            target = directory / f"{stem}-v{n}{suffix}"
            n += 1
        return target

    def add_file(self, case_id: str, src_path: str, *, subdir: str = "") -> MaterialFile:
        src = Path(src_path).resolve()
        if not src.is_file():
            raise FileNotFoundError(src_path)
        data = src.read_bytes()
        digest = sha256_bytes(data)
        manifest = self._load_manifest(case_id)
        for item in manifest:
            if item.sha256 == digest:
                return item  # тот же файл уже зарегистрирован
        meta = read_meta(src)
        root = self.materials_dir(case_id).resolve()
        if root in src.parents:
            rel = src.relative_to(root).as_posix()
        else:
            directory = root / subdir if subdir else root
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / src.name
            if target.exists() and sha256_bytes(target.read_bytes()) != digest:
                target = self._free_name(directory, src.name)
            if not target.exists():
                target.write_bytes(data)
                sidecar = src.with_name(src.name + META_SUFFIX)
                if sidecar.exists():
                    target.with_name(target.name + META_SUFFIX).write_bytes(sidecar.read_bytes())
                target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            rel = target.relative_to(root).as_posix()
        entry = self._entry(rel, data, meta, src)
        manifest.append(entry)
        self._save_manifest(case_id, manifest)
        return entry

    def add_bytes(self, case_id: str, name: str, data: bytes, meta: dict[str, Any]) -> MaterialFile:
        root = self.materials_dir(case_id)
        directory = root / "web"
        directory.mkdir(parents=True, exist_ok=True)
        digest = sha256_bytes(data)
        manifest = self._load_manifest(case_id)
        for item in manifest:
            if item.sha256 == digest:
                return item
        target = self._free_name(directory, name)
        target.write_bytes(data)
        target.with_name(target.name + META_SUFFIX).write_text(
            yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        entry = self._entry(target.relative_to(root).as_posix(), data, meta, target)
        manifest.append(entry)
        self._save_manifest(case_id, manifest)
        return entry

    def list(self, case_id: str) -> list[MaterialFile]:
        return self._load_manifest(case_id)

    def verify(self, case_id: str) -> list[str]:
        problems = []
        root = self.materials_dir(case_id)
        for item in self._load_manifest(case_id):
            path = root / item.path
            if not path.exists():
                problems.append(f"{item.path}: файл отсутствует")
            elif sha256_bytes(path.read_bytes()) != item.sha256:
                problems.append(f"{item.path}: содержимое изменено (SHA-256 не совпадает)")
        return problems

    def absolute_path(self, case_id: str, rel_path: str) -> str:
        return str(self.materials_dir(case_id) / rel_path)
