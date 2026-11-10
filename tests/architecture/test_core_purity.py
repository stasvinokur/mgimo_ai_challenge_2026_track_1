"""Домен импортирует только stdlib и pydantic; application — ещё и domain. Проверка по AST."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "strategist"
ALLOWED_THIRD_PARTY = {"pydantic", "pydantic_core", "typing_extensions"}


def _imports(package: Path) -> list[tuple[Path, str]]:
    found = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found += [(path, alias.name.split(".")[0]) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.append((path, node.module.split(".")[0]))
    return found


def _violations(package: Path) -> list[str]:
    bad = []
    for path, top in _imports(package):
        if top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY or top == "strategist":
            continue
        bad.append(f"{path.relative_to(SRC)} → {top}")
    return bad


def test_domain_uses_only_stdlib_and_pydantic():
    assert _violations(SRC / "domain") == []


def test_application_uses_only_stdlib_pydantic_and_domain():
    assert _violations(SRC / "application") == []


def test_domain_does_not_import_application():
    offenders = [
        f"{p.relative_to(SRC)}"
        for p, _ in _imports(SRC / "domain")
        if "strategist.application" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
