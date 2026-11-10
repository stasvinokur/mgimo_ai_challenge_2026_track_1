"""Профиль моделей поверх базового конфига: «дешёвый маршрут» без облака."""

from __future__ import annotations

from pathlib import Path

import pytest

from strategist.infrastructure.config import deep_merge, load_models

ROOT = Path(__file__).resolve().parents[3]


def test_deep_merge_overrides_nested_keys_and_keeps_the_rest():
    base = {
        "roles": {"reasoning": {"model": "big", "think": False}, "embeddings": {"model": "emb"}},
        "tasks": {"a": 1},
    }
    merged = deep_merge(base, {"roles": {"reasoning": {"model": "small"}}})
    assert merged["roles"]["reasoning"] == {"model": "small", "think": False}
    assert merged["roles"]["embeddings"] == {"model": "emb"} and merged["tasks"] == {"a": 1}
    assert base["roles"]["reasoning"]["model"] == "big"  # база не меняется


def test_local_profile_routes_everything_to_local_models():
    models = load_models(ROOT, "local")
    roles = models["roles"]
    assert models["profile"] == "local"
    assert all(not r["model"].endswith(("cloud", ":cloud")) for r in roles.values())
    assert models["decide"]["default_backend"] == "llm"
    assert models["tasks"] == load_models(ROOT, None)["tasks"]  # маршрутизация задач не меняется
    with pytest.raises(FileNotFoundError, match="Нет профиля"):
        load_models(ROOT, "nonexistent")


def test_request_timeout_is_short_for_cloud_and_long_for_local():
    """Облако обрывает соединения: с таймаутом 600 с и тремя повторами один обрыв держал демо ~40 минут.
    Самый медленный настоящий облачный вызов в прогонах — 62 с; локальная модель на ноутбуке медленнее."""
    cloud, local = load_models(ROOT, None), load_models(ROOT, "local")
    assert 90 <= cloud["request_timeout_seconds"] <= 240
    assert local["request_timeout_seconds"] > cloud["request_timeout_seconds"]


def test_quality_flags_are_on_by_default_and_switched_off_by_env(monkeypatch):
    from strategist.application.quality import FLAGS
    from strategist.infrastructure.config import load_settings

    monkeypatch.delenv("STRATEGIST_QUALITY_OFF", raising=False)
    assert load_settings(ROOT).workflow["quality"] == dict.fromkeys(FLAGS, True)
    monkeypatch.setenv("STRATEGIST_QUALITY_OFF", "all")
    assert load_settings(ROOT).workflow["quality"] == dict.fromkeys(FLAGS, False)
    monkeypatch.setenv("STRATEGIST_QUALITY_OFF", "numbers_v2, summary_repair")
    quality = load_settings(ROOT).workflow["quality"]
    assert not quality["numbers_v2"] and not quality["summary_repair"] and quality["metric_contract"]
    monkeypatch.setenv("STRATEGIST_QUALITY_OFF", "numbers_v3")
    with pytest.raises(ValueError, match="неизвестные флаги"):
        load_settings(ROOT)
