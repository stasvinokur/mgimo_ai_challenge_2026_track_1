"""TASK-3. Проба Jev (TypeSafe) на наборе decide: тот же датасет и те же вопросы, что у Clef.

Маршруты (по убыванию надёжности):
  1. TYPESAFE_API_KEY (или TYPESAFE_AI_API_KEY) — нативный API `POST https://api.typesafe.ai/v1/systemone`,
     Bearer-токен. Прогоняет весь набор, метрики — как у Clef (decide_bench.systemone_summary).
  2. Только OPENROUTER_API_KEY — один разведывательный запрос к OpenRouter (`typesafe/jev-latest`):
     формат запроса через OpenRouter не задокументирован (листинг в бете), поэтому весь набор не гоняем,
     а сохраняем статус и форму ответа для ручного разбора. На 2026-11-04 в публичном каталоге
     https://openrouter.ai/api/v1/models есть только `typesafe/jev-router` — маршрутизатор LLM для chat
     completions («выбирает модель и усилие рассуждения»), а не System One; `.../typesafe/jev-latest/endpoints`
     отвечает 404. Значит, вероятностей choice/noul через OpenRouter, скорее всего, не получить.
  3. Ключей нет — сообщение «ключа нет, пропуск» и код выхода 0.

ВНИМАНИЕ: схема запроса и ответа Jev НЕ ПРОВЕРЕНА вживую (ключа на момент спайка не было).
Она взята из открытых описаний (ai-sdk.dev, opentweet.io/jev/api-access) и совпадает с System One API,
который Ollama реализует для Clef (`/v1/systemone`, типы ollama._types.SystemOne*): поля model, state,
questions{type: choice|noul|score, instructions, criteria}. Если форма ответа отличается — разбор в
decide_bench.parse_clef_answer терпит `probability` вместо `noul` и отсутствие `probabilities`.

Запуск: `uv run python scripts/spikes/jev_probe.py [--limit N]`. Ключи читаются из окружения или .env
и никуда не печатаются и не сохраняются.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decide_bench as db
import spike_common as sc

JEV_BASE_URL = "https://api.typesafe.ai/v1"
JEV_MODEL = "jev-1.13.0"
JEV_PRICE_IN = 0.042  # USD за 1 млн входных токенов, заявление вендора; выход бесплатно
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "typesafe/jev-latest"
CACHE = sc.Cache(db.OUT_DIR / "jev_runs.jsonl")


def read_keys() -> dict[str, str]:
    try:
        from dotenv import load_dotenv

        load_dotenv(sc.ROOT / ".env", override=False)
    except ImportError:
        pass
    keys = {}
    for name in ("TYPESAFE_API_KEY", "TYPESAFE_AI_API_KEY", "OPENROUTER_API_KEY"):
        if os.environ.get(name):
            keys[name] = os.environ[name]
    return keys


def jev_native(key: str, state: str, questions: dict[str, Any]) -> dict[str, Any]:
    """Нативный вызов. Схема тела НЕ ПРОВЕРЕНА: {"model", "state", "questions"} без temperature."""
    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            f"{JEV_BASE_URL}/systemone",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": JEV_MODEL, "state": state, "questions": questions},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        return {
            "status": None,
            "latency_s": time.perf_counter() - t0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    latency = round(time.perf_counter() - t0, 3)
    try:
        data = resp.json()
    except ValueError:
        data = {"error": resp.text[:300]}
    rec: dict[str, Any] = {
        "status": resp.status_code,
        "latency_s": latency,
        "answers": data.get("answers"),
        "usage": data.get("usage"),
        "served_model": data.get("model"),
        "limit_headers": {
            k: v for k, v in resp.headers.items() if any(h in k.lower() for h in sc.LIMIT_HEADER_HINTS)
        },
    }
    if resp.status_code != 200:
        rec["error"] = str(data.get("error") or data)[:300]
    return rec


def run_native(key: str, examples: list[dict[str, Any]]) -> int:
    data = {e["id"]: e for e in examples}
    ok = 0
    for ex in examples:
        ckey = f"jev|{ex['id']}"
        rec = CACHE.get(ckey)
        if not rec or rec.get("status") != 200:
            qs, meta = db.clef_questions(ex)
            res = jev_native(key, ex["state"], qs)
            rec = CACHE.put(ckey, {"kind": "jev", "id": ex["id"], "meta": meta, **res})
            if res.get("status") == 429:
                print("Jev: 429 — лимит запросов, пауза 10 с")
                time.sleep(10)
        ok += rec.get("status") == 200
        print(f"jev {ex['id']} status={rec.get('status')} {rec.get('latency_s')}s {rec.get('error') or ''}")
    recs = [r for r in CACHE.items.values() if r.get("kind") == "jev" and r.get("status") == 200]
    if recs:
        try:
            summary = db.systemone_summary(recs, [], data, JEV_MODEL, JEV_PRICE_IN)
        except (KeyError, TypeError, ValueError) as exc:
            summary = {
                "error": f"форма ответа не совпала с ожидаемой: {exc}",
                "sample": recs[0].get("answers"),
            }
        sc.write_json(db.OUT_DIR / "jev_results.json", summary)
        print("Итог Jev записан в evals/spikes/decide/jev_results.json")
    return ok


def probe_openrouter(key: str, ex: dict[str, Any]) -> None:
    """Разведка: формат НЕ задокументирован. Отправляем state+questions JSON-ом в сообщении."""
    qs, _ = db.clef_questions(ex)
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": json.dumps({"state": ex["state"], "questions": qs}, ensure_ascii=False),
            }
        ],
    }
    t0 = time.perf_counter()
    try:
        resp = httpx.post(OPENROUTER_URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=60)
        status, text = resp.status_code, resp.text[:2000]
    except httpx.HTTPError as exc:
        status, text = None, f"{type(exc).__name__}: {exc}"
    out = {
        "route": "openrouter",
        "model": OPENROUTER_MODEL,
        "status": status,
        "latency_s": round(time.perf_counter() - t0, 3),
        "response_head": text,
        "example_id": ex["id"],
    }
    sc.write_json(db.OUT_DIR / "jev_openrouter_probe.json", out)
    print(f"OpenRouter: status={status}; ответ сохранён в evals/spikes/decide/jev_openrouter_probe.json")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=0, help="только первые N примеров")
    args = parser.parse_args()
    keys = read_keys()
    examples = db.load_dataset()
    if args.limit:
        examples = examples[: args.limit]
    native_key = keys.get("TYPESAFE_API_KEY") or keys.get("TYPESAFE_AI_API_KEY")
    if native_key:
        print(f"Jev: нативный API {JEV_BASE_URL}/systemone, модель {JEV_MODEL}, примеров: {len(examples)}")
        ok = run_native(native_key, examples)
        return 0 if ok else 1
    if keys.get("OPENROUTER_API_KEY"):
        print(
            "Jev: есть только OPENROUTER_API_KEY — один разведывательный запрос (формат не задокументирован)"
        )
        probe_openrouter(keys["OPENROUTER_API_KEY"], examples[0])
        return 0
    print("Jev: ключа нет (TYPESAFE_API_KEY / OPENROUTER_API_KEY не заданы) — пропуск.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
