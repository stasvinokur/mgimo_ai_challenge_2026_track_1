"""Проверка окружения: демон Ollama, наличие моделей ролей, ключи опциональных сервисов."""

from __future__ import annotations

import sys

import httpx

from strategist.application.dto.diagnostics import DiagnosticCheck
from strategist.infrastructure.config import Settings


def _ollama_models(settings: Settings) -> tuple[set[str] | None, str]:
    headers = {"Authorization": f"Bearer {settings.ollama_api_key}"} if settings.ollama_api_key else {}
    try:
        resp = httpx.get(f"{settings.ollama_host.rstrip('/')}/api/tags", headers=headers, timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return None, f"{settings.ollama_host}: {exc.__class__.__name__}: {exc}"
    names = {m.get("name", "") for m in resp.json().get("models", [])}
    return names, f"{settings.ollama_host}: {len(names)} моделей"


def _model_exists(settings: Settings, model: str, local: set[str] | None) -> bool:
    """Локальные модели — по списку; облачные (*:cloud) — через /api/show демона (без скачивания)."""
    if local is not None and model in local:
        return True
    headers = {"Authorization": f"Bearer {settings.ollama_api_key}"} if settings.ollama_api_key else {}
    try:
        resp = httpx.post(
            f"{settings.ollama_host.rstrip('/')}/api/show",
            json={"model": model},
            headers=headers,
            timeout=10.0,
        )
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def run_diagnostics(settings: Settings) -> list[DiagnosticCheck]:
    checks = [
        DiagnosticCheck(
            name="Python",
            ok=sys.version_info >= (3, 12),
            detail=sys.version.split()[0],
        ),
        DiagnosticCheck(
            name="Конфигурация",
            ok=bool(settings.models.get("roles")),
            detail=str(settings.paths.config_dir),
        ),
    ]
    names, detail = _ollama_models(settings)
    checks.append(DiagnosticCheck(name="Демон Ollama", ok=names is not None, detail=detail))
    for role, spec in (settings.models.get("roles") or {}).items():
        model = spec.get("model", "")
        present = names is not None and _model_exists(settings, model, names)
        fallbacks = [
            f for f in spec.get("fallbacks", []) if names is not None and _model_exists(settings, f, names)
        ]
        checks.append(
            DiagnosticCheck(
                name=f"Модель роли {role}",
                ok=present or bool(fallbacks),
                detail=model + ("" if present else f" — нет; доступные запасные: {fallbacks or '—'}"),
                required=role in {"reasoning", "workhorse", "embeddings"},
            )
        )
    backend = settings.decide_backend
    decide_cfg = settings.models.get("decide", {})
    if backend == "jev":
        key_env = decide_cfg.get("jev", {}).get("api_key_env", "TYPESAFE_API_KEY")
        ok, detail = (
            bool(settings.env.get(key_env)),
            f"backend=jev ({'ключ задан' if settings.env.get(key_env) else 'нет ' + key_env})",
        )
    elif backend == "systemone":
        model = decide_cfg.get("systemone", {}).get("model", "clef:cloud")
        ok, detail = (
            names is not None,
            f"backend=systemone ({model} через {settings.ollama_host}/v1/systemone)",
        )
    else:
        ok, detail = True, "backend=llm (запасной вариант)"
    checks.append(DiagnosticCheck(name="decide()", ok=ok, detail=detail, required=False))
    return checks
