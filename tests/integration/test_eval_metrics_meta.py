"""Метаданные метрик стенда: модель агента берётся из самого прогона, а не из базового конфига."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))
from metrics import run_agent_model


def _run(tmp_path: Path, by_model: dict) -> Path:
    (tmp_path / "run.json").write_text(json.dumps({"costs": {"by_model": by_model}}), encoding="utf-8")
    return tmp_path / "output.md"


def test_local_profile_run_reports_its_own_model(tmp_path):
    out = _run(tmp_path, {"gpt-oss:20b": {"calls": 85}})
    assert run_agent_model(out, "deepseek-v4.1-flash:cloud") == "gpt-oss:20b"


def test_cloud_run_keeps_the_configured_reasoning_model(tmp_path):
    out = _run(tmp_path, {"gemma4:31b-cloud": {"calls": 90}, "deepseek-v4.1-flash:cloud": {"calls": 30}})
    assert run_agent_model(out, "deepseek-v4.1-flash:cloud") == "deepseek-v4.1-flash:cloud"


def test_without_run_metadata_falls_back_to_config(tmp_path):
    assert run_agent_model(tmp_path / "output.md", "deepseek-v4.1-flash:cloud") == "deepseek-v4.1-flash:cloud"
