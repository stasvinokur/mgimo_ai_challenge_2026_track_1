"""Общие помощники спайков TASK-2/TASK-3: вызов Ollama, кэш вызовов, разбор JSON.

Используется скриптами `model_bench.py` и `decide_bench.py`. Не часть продукта: код спайка,
поэтому здесь нет портов и адаптеров — только то, что нужно для измерений.

Кэш: каждый вызов модели пишется в JSONL по ключу. Повторный запуск берёт ответ из кэша
и не тратит бюджет вызовов (важно: бюджет спайка ~200 вызовов).
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "http://127.0.0.1:11434"
# Заголовки, по которым можно увидеть лимиты (если облако их когда-нибудь начнёт отдавать).
LIMIT_HEADER_HINTS = ("ratelimit", "rate-limit", "retry-after", "x-usage", "x-quota")
RETRY_STATUSES = {429, 500, 502, 503, 504}


def ollama_host() -> str:
    """Хост демона: OLLAMA_HOST → config/models.yaml (через load_settings) → по умолчанию."""
    if os.environ.get("OLLAMA_HOST"):
        host = os.environ["OLLAMA_HOST"]
        return host if host.startswith("http") else f"http://{host}"
    try:
        from strategist.infrastructure.config import load_settings

        return load_settings(ROOT).ollama_host
    except Exception:
        return DEFAULT_HOST


class CallStats:
    """Счётчик реальных (не из кэша) вызовов моделей за запуск."""

    def __init__(self) -> None:
        self.live: dict[str, int] = {}
        self.cached = 0

    def add(self, model: str) -> None:
        self.live[model] = self.live.get(model, 0) + 1

    @property
    def total_live(self) -> int:
        return sum(self.live.values())


STATS = CallStats()


class Cache:
    """Append-only JSONL: одна строка — один вызов, ключ — строка `key`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.items[rec["key"]] = rec

    def get(self, key: str) -> dict[str, Any] | None:
        rec = self.items.get(key)
        if rec is not None:
            STATS.cached += 1
        return rec

    def put(self, key: str, rec: dict[str, Any]) -> dict[str, Any]:
        rec = {"key": key, **rec}
        self.items[key] = rec
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec


def _limit_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if any(h in k.lower() for h in LIMIT_HEADER_HINTS)}


def post_json(
    path: str,
    body: dict[str, Any],
    *,
    model: str,
    timeout: float = 600.0,
    retries: int = 2,
) -> dict[str, Any]:
    """POST к демону с замером времени и повтором на 429/5xx. Возвращает служебную запись.

    В записи: status, latency_s (последней попытки), attempts, data (JSON ответа) или error,
    limit_headers (заголовки про лимиты, если есть).
    """
    url = ollama_host().rstrip("/") + path
    attempts: list[dict[str, Any]] = []
    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        STATS.add(model)
        try:
            resp = httpx.post(url, json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            latency = time.perf_counter() - t0
            attempts.append({"status": None, "latency_s": latency, "error": f"{type(exc).__name__}: {exc}"})
            if attempt < retries:
                time.sleep(2 * 4**attempt)
                continue
            return {
                "status": None,
                "latency_s": latency,
                "attempts": attempts,
                "error": attempts[-1]["error"],
            }
        latency = time.perf_counter() - t0
        try:
            data = resp.json()
        except ValueError:
            data = {"error": resp.text[:500]}
        info = {
            "status": resp.status_code,
            "latency_s": latency,
            "limit_headers": _limit_headers(resp.headers),
        }
        attempts.append({**info, "error": data.get("error") if isinstance(data, dict) else None})
        if resp.status_code in RETRY_STATUSES and attempt < retries:
            retry_after = resp.headers.get("retry-after")
            time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2 * 4**attempt)
            continue
        out = {**info, "attempts": attempts, "data": data}
        if resp.status_code != 200:
            out["error"] = data.get("error") if isinstance(data, dict) else str(data)
        return out
    raise AssertionError("unreachable")


def chat(
    model: str,
    messages: list[dict[str, Any]],
    *,
    think: bool | str | None = None,
    fmt: dict[str, Any] | str | None = None,
    options: dict[str, Any] | None = None,
    logprobs: bool = False,
    top_logprobs: int | None = None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Один вызов /api/chat (без стриминга). Возвращает плоскую запись с метриками."""
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if think is not None:
        body["think"] = think
    if fmt is not None:
        body["format"] = fmt
    if options:
        body["options"] = options
    if logprobs:
        body["logprobs"] = True
        if top_logprobs:
            body["top_logprobs"] = top_logprobs
    raw = post_json("/api/chat", body, model=model, timeout=timeout)
    data = raw.get("data") or {}
    msg = data.get("message") or {}
    return {
        "status": raw["status"],
        "error": raw.get("error"),
        "latency_s": round(raw["latency_s"], 3),
        "attempts": len(raw.get("attempts", [])),
        "limit_headers": raw.get("limit_headers", {}),
        "served_model": data.get("model"),
        "content": msg.get("content"),
        "thinking": msg.get("thinking"),
        "prompt_eval_count": data.get("prompt_eval_count"),
        "eval_count": data.get("eval_count"),
        "total_duration_s": (data.get("total_duration") or 0) / 1e9 or None,
        "done_reason": data.get("done_reason"),
        "logprobs": data.get("logprobs"),
    }


def embed(model: str, inputs: list[str], *, timeout: float = 600.0) -> dict[str, Any]:
    raw = post_json("/api/embed", {"model": model, "input": inputs}, model=model, timeout=timeout)
    data = raw.get("data") or {}
    return {
        "status": raw["status"],
        "error": raw.get("error"),
        "latency_s": round(raw["latency_s"], 3),
        "embeddings": data.get("embeddings"),
        "prompt_eval_count": data.get("prompt_eval_count"),
    }


def systemone(model: str, state: Any, questions: dict[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
    """Вызов System One API демона Ollama (`/v1/systemone`) — тот же протокол, что у Jev."""
    raw = post_json(
        "/v1/systemone",
        {"model": model, "state": state, "questions": questions},
        model=model,
        timeout=timeout,
    )
    data = raw.get("data") or {}
    return {
        "status": raw["status"],
        "error": raw.get("error"),
        "latency_s": round(raw["latency_s"], 3),
        "attempts": len(raw.get("attempts", [])),
        "limit_headers": raw.get("limit_headers", {}),
        "answers": data.get("answers"),
        "usage": data.get("usage"),
        "served_model": data.get("model"),
    }


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str | None) -> tuple[Any, str | None]:
    """Достаёт JSON из ответа. Режим: strict (весь ответ — JSON), fenced (в ```), braces (первый {...})."""
    if not text:
        return None, None
    stripped = text.strip()
    try:
        return json.loads(stripped), "strict"
    except ValueError:
        pass
    for block in _FENCE_RE.findall(stripped):
        try:
            return json.loads(block.strip()), "fenced"
        except ValueError:
            continue
    start = stripped.find("{")
    end = stripped.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(stripped[start : end + 1]), "braces"
        except ValueError:
            pass
    # Рассуждение текстом, а JSON где-то внутри (так делает glm-5.3-flash при think=false):
    # берём самый длинный объект, который целиком разбирается с какой-либо открывающей скобки.
    decoder = json.JSONDecoder()
    best: tuple[Any, int] | None = None
    for m in re.finditer(r"\{", stripped):
        try:
            obj, stop = decoder.raw_decode(stripped, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and (best is None or stop - m.start() > best[1]):
            best = (obj, stop - m.start())
    if best is not None:
        return best[0], "embedded"
    return None, None


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Разворачивает $ref/$defs pydantic-схемы: грамматике Ollama и промпту так проще."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                resolved = walk(copy.deepcopy(defs[name]))
                extra = {k: walk(v) for k, v in node.items() if k not in ("$ref", "title")}
                return {**resolved, **extra}
            out = {}
            for k, v in node.items():
                if k == "properties" and isinstance(v, dict):
                    # Здесь ключи — имена полей (в том числе поле `title`), их не трогаем.
                    out[k] = {name: walk(sub) for name, sub in v.items()}
                elif k == "title" and isinstance(v, str):
                    continue  # аннотация pydantic, а не поле
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    idx = (len(xs) - 1) * q
    lo, hi = int(idx), min(int(idx) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (idx - lo)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
