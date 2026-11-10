"""Контракты чистой архитектуры: реальный пакет проходит, нарушитель ловится."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINT_IMPORTS = shutil.which("lint-imports") or str(Path(sys.executable).with_name("lint-imports"))


def _run_lint(config: Path, cwd: Path, extra_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if extra_path is not None:
        env["PYTHONPATH"] = f"{extra_path}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        [LINT_IMPORTS, "--config", str(config), "--no-cache"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def test_real_package_respects_layers() -> None:
    result = _run_lint(ROOT / ".importlinter", ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def _make_fixture_package(tmp_path: Path, violation: str) -> Path:
    pkg = tmp_path / "src" / "fixturepkg"
    for layer in ["domain", "application", "infrastructure", "interfaces"]:
        (pkg / layer).mkdir(parents=True)
        (pkg / layer / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "bootstrap.py").write_text("")
    (pkg / "infrastructure" / "adapter.py").write_text("VALUE = 1\n")
    (pkg / "domain" / "model.py").write_text("X = 1\n")
    target, line = violation.split(":", 1)
    (pkg / target).write_text(line + "\n")
    config = (ROOT / ".importlinter").read_text(encoding="utf-8").replace("strategist", "fixturepkg")
    cfg_path = tmp_path / ".importlinter"
    cfg_path.write_text(config, encoding="utf-8")
    return cfg_path


def test_domain_importing_infrastructure_is_rejected(tmp_path: Path) -> None:
    cfg = _make_fixture_package(tmp_path, "domain/bad.py:from fixturepkg.infrastructure import adapter")
    result = _run_lint(cfg, tmp_path, tmp_path / "src")
    assert result.returncode != 0, "нарушение domain → infrastructure должно валить проверку"
    assert "BROKEN" in result.stdout or "broken" in result.stdout.lower()


def test_domain_importing_framework_is_rejected(tmp_path: Path) -> None:
    cfg = _make_fixture_package(tmp_path, "domain/bad.py:import httpx")
    result = _run_lint(cfg, tmp_path, tmp_path / "src")
    assert result.returncode != 0, "домен не должен импортировать httpx"


def test_interfaces_importing_infrastructure_is_rejected(tmp_path: Path) -> None:
    cfg = _make_fixture_package(tmp_path, "interfaces/cli.py:from fixturepkg.infrastructure import adapter")
    result = _run_lint(cfg, tmp_path, tmp_path / "src")
    assert result.returncode != 0, "интерфейсы ходят в инфраструктуру только через bootstrap"
