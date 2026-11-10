"""Загрузка конфигурации: config/*.yaml + переменные окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from strategist.application.quality import switch_off


def find_root(start: Path | None = None) -> Path:
    """Корень проекта — каталог, где лежат AGENTS.md и config/."""
    env_root = os.environ.get("STRATEGIST_ROOT")
    if env_root:
        return Path(env_root).resolve()
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config" / "models.yaml").exists() and (candidate / "AGENTS.md").exists():
            return candidate
    # Установленный пакет: src/strategist/infrastructure/config.py → корень на 4 уровня выше.
    pkg_root = Path(__file__).resolve().parents[3]
    if (pkg_root / "config" / "models.yaml").exists():
        return pkg_root
    raise FileNotFoundError("Не найден корень проекта (config/models.yaml и AGENTS.md)")


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидался YAML-словарь")
    return data


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    config_dir: Path
    cases_dir: Path
    instructions_dir: Path
    skills_dir: Path
    knowledge_dir: Path
    evals_dir: Path

    @classmethod
    def from_root(cls, root: Path, cases_dir: str | None = None) -> ProjectPaths:
        cases = Path(cases_dir) if cases_dir else root / "cases"
        if not cases.is_absolute():
            cases = root / cases
        return cls(
            root=root,
            config_dir=root / "config",
            cases_dir=cases,
            instructions_dir=root / "agent" / "instructions",
            skills_dir=root / ".agents" / "skills",
            knowledge_dir=root / "knowledge",
            evals_dir=root / "evals",
        )


@dataclass
class Settings:
    paths: ProjectPaths
    models: dict[str, Any]
    prices: dict[str, Any]
    allowlist: dict[str, Any]
    workflow: dict[str, Any]
    env: dict[str, str] = field(default_factory=dict)

    @property
    def ollama_host(self) -> str:
        return self.env.get(self.models.get("host_env", "OLLAMA_HOST")) or self.models.get(
            "default_host", "http://127.0.0.1:11434"
        )

    @property
    def ollama_api_key(self) -> str | None:
        return self.env.get(self.models.get("api_key_env", "OLLAMA_API_KEY")) or None

    @property
    def decide_backend(self) -> str:
        decide = self.models.get("decide", {})
        return self.env.get(decide.get("backend_env", "STRATEGIST_DECIDE_BACKEND")) or decide.get(
            "default_backend", "llm"
        )


ENV_KEYS = (
    "OLLAMA_HOST",
    "OLLAMA_API_KEY",
    "TYPESAFE_API_KEY",
    "OPENROUTER_API_KEY",
    "STRATEGIST_DECIDE_BACKEND",
    "STRATEGIST_CASES_DIR",
    "STRATEGIST_MODELS_PROFILE",
    "STRATEGIST_QUALITY_OFF",
)


def deep_merge(base: dict, overlay: dict) -> dict:
    """Профиль моделей поверх базового конфига: словари сливаются по ключам, прочее заменяется."""
    out = dict(base)
    for key, value in overlay.items():
        out[key] = (
            deep_merge(out[key], value)
            if isinstance(value, dict) and isinstance(out.get(key), dict)
            else value
        )
    return out


def load_models(project_root: Path, profile: str | None) -> dict:
    models = load_yaml(project_root / "config" / "models.yaml")
    if profile:
        overlay = project_root / "config" / f"models.{profile}.yaml"
        if not overlay.exists():
            raise FileNotFoundError(f"Нет профиля моделей {overlay} (STRATEGIST_MODELS_PROFILE={profile})")
        models = deep_merge(models, load_yaml(overlay))
        models["profile"] = profile
    return models


def load_settings(root: Path | None = None) -> Settings:
    project_root = root or find_root()
    load_dotenv(project_root / ".env", override=False)
    env = {key: os.environ[key] for key in ENV_KEYS if os.environ.get(key)}
    workflow = load_yaml(project_root / "config" / "settings.yaml")
    workflow["quality"] = switch_off(workflow.get("quality") or {}, env.get("STRATEGIST_QUALITY_OFF"))
    cases_dir = env.get("STRATEGIST_CASES_DIR") or workflow.get("cases_dir")
    return Settings(
        paths=ProjectPaths.from_root(project_root, cases_dir),
        models=load_models(project_root, env.get("STRATEGIST_MODELS_PROFILE")),
        prices=load_yaml(project_root / "config" / "prices.yaml"),
        allowlist=load_yaml(project_root / "config" / "allowlist.yaml"),
        workflow=workflow,
        env=env,
    )
