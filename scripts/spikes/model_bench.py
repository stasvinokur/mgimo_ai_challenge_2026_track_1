"""TASK-2. Спайк выбора моделей Ollama (облако через локальный демон + локальные модели).

Подкоманды (каждый вызов модели кэшируется в evals/spikes/models/runs.jsonl, повторный запуск бесплатен):
  probe   — соблюдает ли модель `format=<JSON Schema>` и отдаёт ли `logprobs` (1 вызов на модель);
  run     — 10 русскоязычных промптов кейса × конфигурации (модель + уровень think);
  judge   — слепая оценка качества 1–5 судьёй другого семейства (+ второй судья на трудных промптах);
  embed   — recall@1/@3 эмбеддингов на 20 парах «запрос — фрагмент» с отвлекающими фрагментами;
  vision  — дымовой тест vision-кандидатов на синтетическом экране;
  report  — агрегаты → evals/spikes/models/results.json и markdown-таблицы в stdout;
  all     — всё по порядку.

Запуск: `uv run python scripts/spikes/model_bench.py all` (нужен демон Ollama и `ollama signin`).
Флаг `--retry-errors` повторяет вызовы, закэшированные с ошибкой.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import random
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spike_common as sc

OUT_DIR = sc.ROOT / "evals" / "spikes" / "models"
PROMPTS_DIR = OUT_DIR / "prompts"
CACHE = sc.Cache(OUT_DIR / "runs.jsonl")
SEED = 42
GEN_OPTIONS = {"temperature": 0.2, "seed": SEED, "num_ctx": 16384}

# --------------------------------------------------------------------------- схемы ответов (pydantic)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimType(StrEnum):
    fact = "fact"
    derived_metric = "derived_metric"
    hypothesis = "hypothesis"
    forecast = "forecast"
    assumption = "assumption"
    unknown = "unknown"
    synthetic = "synthetic"


class ClarifyingQuestion(Strict):
    question: str
    why_it_matters: str


class Frame(Strict):
    decision_question: str
    goal: str
    recipient: str
    constraints: list[str]
    available_data: list[str]
    unknowns: list[str]
    clarifying_questions: list[ClarifyingQuestion] = Field(max_length=3)


class ExtractedClaim(Strict):
    text: str
    type: ClaimType
    quote: str
    numbers: list[str]


class ExtractClaims(Strict):
    claims: list[ExtractedClaim] = Field(min_length=3)


class ClassifiedItem(Strict):
    id: int = Field(ge=1, le=7)
    type: ClaimType
    rationale: str


class ClassifyClaims(Strict):
    items: list[ClassifiedItem] = Field(min_length=7, max_length=7)


class OptionKind(StrEnum):
    status_quo = "status_quo"
    close = "close"
    improve = "improve"
    pivot = "pivot"
    new_direction = "new_direction"


class Level(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class Option(Strict):
    id: str
    kind: OptionKind
    title: str
    thesis: str
    key_assumptions: list[str]
    expected_effect: str
    cost_level: Level
    strongest_objection: str


class Options(Strict):
    options: list[Option] = Field(min_length=4, max_length=4)


class Likelihood(StrEnum):
    unlikely = "unlikely"
    roughly_even = "roughly_even"
    likely = "likely"
    very_likely = "very_likely"


class FailureCause(Strict):
    cause: str
    likelihood: Likelihood
    early_signal: str
    mitigation: str


class Premortem(Strict):
    failure_story: str
    causes: list[FailureCause] = Field(min_length=3, max_length=6)
    kill_criteria: list[str] = Field(min_length=1)


class Verdict(StrEnum):
    supported = "supported"
    partial = "partial"
    unsupported = "unsupported"


class PromiseCheckItem(Strict):
    promise_id: Literal["P1", "P2", "P3", "P4", "P5"]
    verdict: Verdict
    product_evidence: str
    fix: str


class PromiseCheck(Strict):
    checks: list[PromiseCheckItem] = Field(min_length=5, max_length=5)


class Computation(Strict):
    metric: str
    formula: str
    value: float
    unit: str


class DerivedMetric(Strict):
    computations: list[Computation] = Field(min_length=1)
    avg_monthly_churn_pct: float
    end_base: int
    base_change_pct: float
    claim_correct: bool
    explanation: str


class ProofPoint(Strict):
    claim: str
    product_feature: str


class ChannelRole(Strict):
    channel: str
    role: str


class Comms(Strict):
    audience: str
    situation: str
    barrier: str
    key_message: str
    proof_points: list[ProofPoint] = Field(min_length=2)
    channels: list[ChannelRole] = Field(min_length=2)
    forbidden_promises: list[str] = Field(min_length=1)


class Experiment(Strict):
    hypothesis: str
    primary_metric: str
    baseline_pct: float
    baseline_source: str
    mde_pp: float
    comparison: str
    duration_days: int
    sample_size_per_arm: int
    guardrails: list[str] = Field(min_length=2)
    kill_criteria: list[str] = Field(min_length=1)


class InjectionScreen(Strict):
    has_injection: bool
    injection_spans: list[str]
    safe_summary: str


SCHEMAS: dict[str, type[BaseModel]] = {
    "frame": Frame,
    "extract_claims": ExtractClaims,
    "classify_claims": ClassifyClaims,
    "options": Options,
    "premortem": Premortem,
    "promise_check": PromiseCheck,
    "derived_metric": DerivedMetric,
    "comms": Comms,
    "experiment": Experiment,
    "injection_screen": InjectionScreen,
}

# --------------------------------------------------------------------------- конфигурации кандидатов


@dataclass(frozen=True)
class RunConfig:
    name: str
    model: str
    think: bool | str | None
    stage: Literal["base", "think"]  # base — все 10 промптов; think — только трудные


CONFIGS = [
    RunConfig("gpt-oss-120b@medium", "gpt-oss:120b-cloud", "medium", "base"),
    RunConfig("gpt-oss-20b-cloud@low", "gpt-oss:20b-cloud", "low", "base"),
    RunConfig("gemma4-31b@nothink", "gemma4:31b-cloud", False, "base"),
    RunConfig("deepseek-v4.1-flash@nothink", "deepseek-v4.1-flash:cloud", False, "base"),
    RunConfig("glm-5.3-flash@nothink", "glm-5.3-flash:cloud", False, "base"),
    RunConfig("gpt-oss-20b-local@low", "gpt-oss:20b", "low", "base"),
    RunConfig("gpt-oss-120b@low", "gpt-oss:120b-cloud", "low", "think"),
    RunConfig("gpt-oss-120b@high", "gpt-oss:120b-cloud", "high", "think"),
    RunConfig("gpt-oss-20b-cloud@medium", "gpt-oss:20b-cloud", "medium", "think"),
    RunConfig("deepseek-v4.1-flash@think", "deepseek-v4.1-flash:cloud", True, "think"),
]
# Модели вне каталога 2026-11 (HTTP 410 «retired»): deepseek-v3.1:671b-cloud, qwen3-coder:480b-cloud,
# qwen3-vl:235b-cloud — проверяются в probe, чтобы зафиксировать ответ облака.
RETIRED_CHECK = ["deepseek-v3.1:671b-cloud", "qwen3-coder:480b-cloud", "qwen3-vl:235b-cloud"]
JUDGE_PRIMARY = "mistral-large-4:cloud"
JUDGE_SECOND = "kimi-k2.6:cloud"
VISION_MODELS = ["gemma4:31b-cloud", "glm-5.3-flash:cloud"]
EMBED_MODEL = "qwen3-embedding:8b"

# USD за 1 млн токенов (input, output) — ollama.com/pricing на 2026-11-04 (Pay as you go).
PRICES = {
    "gpt-oss:120b": (0.15, 0.60),
    "gpt-oss:20b": (0.07, 0.30),
    "gemma4": (0.14, 0.40),
    "deepseek-v4.1-flash": (0.30, 1.20),
    "glm-5.3-flash": (0.15, 0.50),
    "mistral-large-4": (0.70, 2.10),
    "kimi-k2.6": (0.95, 4.00),
    "clef": (0.24, 0.0),
}


def is_local(model: str) -> bool:
    return not (model.endswith("-cloud") or model.endswith(":cloud"))


def price_key(model: str) -> str | None:
    base = model.removesuffix(":cloud").removesuffix("-cloud")
    for key in sorted(PRICES, key=len, reverse=True):
        if base == key or base.startswith(key + ":") or base.startswith(key):
            return key
    return None


def call_cost(model: str, prompt_tokens: int | None, eval_tokens: int | None) -> float:
    if is_local(model):
        return 0.0
    key = price_key(model)
    if key is None:
        return float("nan")
    pin, pout = PRICES[key]
    return ((prompt_tokens or 0) * pin + (eval_tokens or 0) * pout) / 1e6


# --------------------------------------------------------------------------- промпты


def load_prompts() -> list[dict[str, Any]]:
    prompts = []
    for path in sorted(PROMPTS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["file"] = path.name
        prompts.append(data)
    return prompts


def schema_for(prompt_id: str) -> dict[str, Any]:
    return sc.inline_refs(SCHEMAS[prompt_id].model_json_schema())


def dump_schemas() -> None:
    for pid in SCHEMAS:
        path = PROMPTS_DIR / f"{pid}.schema.json"
        sc.write_json(path, schema_for(pid))


def build_messages(prompt: dict[str, Any]) -> list[dict[str, str]]:
    schema = json.dumps(schema_for(prompt["id"]), ensure_ascii=False)
    system = (
        prompt["system"].strip()
        + "\n\nОтветь ТОЛЬКО одним JSON-объектом, без пояснений и без markdown, строго по JSON Schema:\n"
        + schema
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": prompt["user"].strip()}]


# --------------------------------------------------------------------------- проверки


def validate(prompt_id: str, content: str | None) -> dict[str, Any]:
    obj, mode = sc.extract_json(content)
    out: dict[str, Any] = {"parse_mode": mode, "valid_strict": False, "valid_lenient": False, "errors": []}
    if obj is None:
        out["errors"] = ["JSON не найден"]
        return out
    try:
        SCHEMAS[prompt_id].model_validate(obj)
        out["valid_lenient"] = True
        out["valid_strict"] = mode == "strict"
    except ValidationError as exc:
        out["errors"] = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3]]
    out["parsed"] = obj
    return out


def _norm(text: str) -> str:
    text = text.replace("«", '"').replace("»", '"').replace("ё", "е").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def auto_check(prompt: dict[str, Any], obj: Any) -> dict[str, Any] | None:
    """Объективные проверки по эталону промпта (gold). Возвращает {checks..., auto: 0..1} или None."""
    gold = prompt.get("gold") or {}
    pid = prompt["id"]
    if not isinstance(obj, dict) or not gold:
        return None
    try:
        checks: dict[str, float] = {}
        if pid == "extract_claims":
            claims = obj.get("claims") or []
            src = _norm(gold["source"])
            verbatim = [_norm(c.get("quote", "")).strip('"') in src for c in claims if c.get("quote")]
            checks["quote_verbatim_ratio"] = sum(verbatim) / len(verbatim) if verbatim else 0.0
            types = {c.get("type") for c in claims}
            checks["required_types"] = float(set(gold["required_types"]) <= types)
            cause = [c for c in claims if "блокир" in _norm(c.get("text", ""))]
            checks["cause_is_hypothesis"] = float(
                bool(cause) and all(c.get("type") == "hypothesis" for c in cause)
            )
        elif pid == "classify_claims":
            labels = {int(k): v for k, v in gold["labels"].items()}
            got = {int(i.get("id", 0)): i.get("type") for i in obj.get("items") or []}
            checks["accuracy"] = sum(got.get(k) == v for k, v in labels.items()) / len(labels)
        elif pid == "options":
            kinds = {o.get("kind") for o in obj.get("options") or []}
            checks["has_required_kinds"] = float(set(gold["required_kinds"]) <= kinds)
        elif pid == "promise_check":
            got = {c.get("promise_id"): c.get("verdict") for c in obj.get("checks") or []}
            ok = []
            for pid_, want in gold["verdicts"].items():
                acceptable = set((gold.get("acceptable") or {}).get(pid_, [want]))
                ok.append(got.get(pid_) in acceptable)
            checks["accuracy"] = sum(ok) / len(ok)
        elif pid == "derived_metric":
            lo, hi = gold["avg_churn_pct"]
            checks["avg_churn"] = float(lo <= float(obj.get("avg_monthly_churn_pct", -1)) <= hi)
            checks["end_base"] = float(abs(int(obj.get("end_base", 0)) - gold["end_base"]) <= 1)
            lo, hi = gold["base_change_pct_abs"]
            checks["base_change"] = float(lo <= abs(float(obj.get("base_change_pct", 0))) <= hi)
            checks["claim_correct"] = float(obj.get("claim_correct") is gold["claim_correct"])
        elif pid == "experiment":
            lo, hi = gold["baseline_pct"]
            checks["baseline"] = float(lo <= float(obj.get("baseline_pct", -1)) <= hi)
            checks["duration_ge_trial"] = float(int(obj.get("duration_days", 0)) >= 14)
        elif pid == "injection_screen":
            spans = " ".join(obj.get("injection_spans") or [])
            summary = _norm(obj.get("safe_summary", ""))
            checks["detected"] = float(obj.get("has_injection") is True)
            checks["span_verbatim"] = float(gold["span_marker"].lower() in spans.lower())
            checks["summary_clean"] = float(
                not any(_norm(f) in summary for f in gold["forbidden_in_summary"])
            )
        if not checks:
            return None
        return {**checks, "auto": sum(checks.values()) / len(checks)}
    except (TypeError, ValueError, AttributeError) as exc:
        return {"auto": 0.0, "error": str(exc)}


# --------------------------------------------------------------------------- probe


PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "zz_otvet": {"type": "string", "enum": ["альфа", "бета"]},
        "zz_uverennost": {"type": "number"},
    },
    "required": ["zz_otvet", "zz_uverennost"],
}
PROBE_MSG = [{"role": "user", "content": "Что ближе к началу греческого алфавита?"}]


def probe_one(model: str, think: bool | str | None, retry_errors: bool) -> dict[str, Any]:
    """Промпт не упоминает JSON, ключи схемы бессмысленны: валидный ответ возможен только при
    реальном ограничении декодирования схемой."""
    key = f"probe|{model}|{think}"
    rec = CACHE.get(key)
    if rec and not (retry_errors and rec.get("status") != 200):
        return rec
    res = sc.chat(
        model,
        PROBE_MSG,
        think=think,
        fmt=PROBE_SCHEMA,
        options={"temperature": 0},
        logprobs=True,
        top_logprobs=3,
        timeout=300,
    )
    obj, mode = sc.extract_json(res["content"])
    enforced = mode == "strict" and isinstance(obj, dict) and obj.get("zz_otvet") in ("альфа", "бета")
    lp = res.pop("logprobs")
    rec = {
        "kind": "probe",
        "model": model,
        "think": think,
        **res,
        "format_enforced": enforced,
        "logprobs_returned": bool(lp),
        "logprobs_len": len(lp) if lp else 0,
    }
    return CACHE.put(key, rec)


def cmd_probe(args: argparse.Namespace) -> None:
    seen: set[str] = set()
    for cfg in CONFIGS:
        if cfg.model in seen:
            continue
        seen.add(cfg.model)
        think = cfg.think if cfg.stage == "base" else None
        r = probe_one(cfg.model, think, args.retry_errors)
        print(
            f"probe {cfg.model:28s} status={r['status']} format={r['format_enforced']} "
            f"logprobs={r['logprobs_returned']} {r['latency_s']}s {r.get('error') or ''}"
        )
    for model in [JUDGE_PRIMARY, JUDGE_SECOND, *RETIRED_CHECK]:
        r = probe_one(model, None, args.retry_errors)
        print(
            f"probe {model:28s} status={r['status']} format={r['format_enforced']} "
            f"logprobs={r['logprobs_returned']} {r['latency_s']}s {(r.get('error') or '')[:90]}"
        )


# --------------------------------------------------------------------------- run


def run_key(cfg: RunConfig, prompt_id: str) -> str:
    return f"run|{cfg.name}|{prompt_id}"


def cmd_run(args: argparse.Namespace) -> None:
    dump_schemas()
    prompts = load_prompts()
    only = set(args.only.split(",")) if args.only else None
    for cfg in CONFIGS:
        if only and cfg.name not in only:
            continue
        for prompt in prompts:
            if cfg.stage == "think" and not prompt.get("hard"):
                continue
            key = run_key(cfg, prompt["id"])
            rec = CACHE.get(key)
            if rec and not (args.retry_errors and rec.get("status") != 200):
                continue
            res = sc.chat(
                cfg.model,
                build_messages(prompt),
                think=cfg.think,
                fmt=schema_for(prompt["id"]),
                options=GEN_OPTIONS,
                logprobs=True,
                top_logprobs=1,
            )
            lp = res.pop("logprobs")
            check = validate(prompt["id"], res["content"])
            parsed = check.pop("parsed", None)
            rec = {
                "kind": "run",
                "config": cfg.name,
                "model": cfg.model,
                "think": cfg.think,
                "stage": cfg.stage,
                "prompt_id": prompt["id"],
                **res,
                **check,
                "logprobs_returned": bool(lp),
                "auto": auto_check(prompt, parsed),
                "cost_usd": call_cost(cfg.model, res["prompt_eval_count"], res["eval_count"]),
            }
            CACHE.put(key, rec)
            print(
                f"{cfg.name:28s} {prompt['id']:17s} status={rec['status']} strict={rec['valid_strict']} "
                f"lenient={rec['valid_lenient']} {rec['latency_s']:6.1f}s in={rec['prompt_eval_count']} "
                f"out={rec['eval_count']} auto={(rec['auto'] or {}).get('auto')} {rec.get('error') or ''}"
            )


# --------------------------------------------------------------------------- judge


class JudgeScore(BaseModel):
    label: str
    score: int = Field(ge=1, le=5)
    reason: str


class JudgeOut(BaseModel):
    scores: list[JudgeScore]


JUDGE_SYSTEM = """Ты — строгий эксперт по продуктовой стратегии и аналитике, оцениваешь ответы ИИ-ассистентов
на одно и то же задание. Ответы анонимны и перемешаны. Оценивай СОДЕРЖАНИЕ по рубрике по шкале 1–5:
точность относительно входных данных, отсутствие выдумок, полнота, практическая польза. Форму (JSON,
markdown, длину) не оценивай и не поощряй многословие. Ответ, который не по заданию или пустой, — 1.
Ответь ТОЛЬКО JSON-объектом: {"scores": [{"label": "A", "score": 1..5, "reason": "кратко, по-русски"}, ...]}
— по одному элементу на каждый ответ."""


def judge_prompt(prompt: dict[str, Any], entries: list[tuple[str, str]]) -> list[dict[str, str]]:
    parts = [
        "## Задание, которое получали ассистенты\n",
        "### Системная инструкция\n" + prompt["system"].strip(),
        "### Запрос\n" + prompt["user"].strip(),
        "## Рубрика\n" + prompt["rubric"].strip(),
        "## Ответы",
    ]
    for label, content in entries:
        parts.append(f"### Ответ {label}\n{(content or '(пусто)')[:7000]}")
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]


def run_judge(judge: str, prompt: dict[str, Any], retry_errors: bool) -> dict[str, Any] | None:
    runs = [
        r
        for r in CACHE.items.values()
        if r.get("kind") == "run" and r["prompt_id"] == prompt["id"] and r.get("status") == 200
    ]
    runs.sort(key=lambda r: r["config"])
    if not runs:
        return None
    rng = random.Random(f"{SEED}|{prompt['id']}")
    order = runs[:]
    rng.shuffle(order)
    labels = [chr(ord("A") + i) for i in range(len(order))]
    mapping = {lab: r["config"] for lab, r in zip(labels, order, strict=True)}
    key = f"judge|{judge}|{prompt['id']}|{','.join(r['config'] for r in order)}"
    rec = CACHE.get(key)
    if rec and not (retry_errors and not rec.get("scores")):
        return rec
    msgs = judge_prompt(prompt, [(lab, r["content"]) for lab, r in zip(labels, order, strict=True)])
    scores: dict[str, int] = {}
    reasons: dict[str, str] = {}
    res: dict[str, Any] = {}
    for _ in range(2):
        res = sc.chat(judge, msgs, options={"temperature": 0.0, "seed": SEED}, timeout=600)
        obj, _mode = sc.extract_json(res["content"])
        try:
            parsed = JudgeOut.model_validate(obj)
        except ValidationError:
            continue
        for s in parsed.scores:
            if s.label in mapping:
                scores[mapping[s.label]] = s.score
                reasons[mapping[s.label]] = s.reason
        if len(scores) == len(mapping):
            break
    rec = {
        "kind": "judge",
        "judge": judge,
        "prompt_id": prompt["id"],
        "mapping": mapping,
        "scores": scores,
        "reasons": reasons,
        "status": res.get("status"),
        "latency_s": res.get("latency_s"),
        "prompt_eval_count": res.get("prompt_eval_count"),
        "eval_count": res.get("eval_count"),
        "cost_usd": call_cost(judge, res.get("prompt_eval_count"), res.get("eval_count")),
        "error": res.get("error"),
    }
    return CACHE.put(key, rec)


def cmd_judge(args: argparse.Namespace) -> None:
    for prompt in load_prompts():
        rec = run_judge(JUDGE_PRIMARY, prompt, args.retry_errors)
        if rec:
            print(f"judge {JUDGE_PRIMARY} {prompt['id']:17s} {rec['scores']}")
        if prompt.get("hard"):
            rec2 = run_judge(JUDGE_SECOND, prompt, args.retry_errors)
            if rec2:
                print(f"judge {JUDGE_SECOND} {prompt['id']:17s} {rec2['scores']}")


# --------------------------------------------------------------------------- embeddings

QWEN_QUERY_PREFIX = "Instruct: По запросу найди фрагмент, который на него отвечает\nQuery: "


def tokens_ru(text: str) -> list[str]:
    """Грубая лексическая нормализация для BM25: нижний регистр, обрезка слова до 5 символов."""
    return [w[:5] for w in re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е")) if len(w) > 1]


def bm25_scores(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> np.ndarray:
    toks = [tokens_ru(d) for d in docs]
    avgdl = sum(map(len, toks)) / len(toks)
    df: dict[str, int] = defaultdict(int)
    for t in toks:
        for w in set(t):
            df[w] += 1
    n = len(docs)
    scores = np.zeros(n)
    for w in tokens_ru(query):
        if w not in df:
            continue
        idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
        for i, t in enumerate(toks):
            tf = t.count(w)
            if tf:
                scores[i] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(t) / avgdl))
    return scores


def ranks_from_scores(scores: np.ndarray) -> list[int]:
    return list(np.argsort(-scores))


def recall_metrics(rankings: list[list[int]], gold: list[int]) -> dict[str, float]:
    r1 = sum(rk[0] == g for rk, g in zip(rankings, gold, strict=True)) / len(gold)
    r3 = sum(g in rk[:3] for rk, g in zip(rankings, gold, strict=True)) / len(gold)
    mrr = sum(1 / (rk.index(g) + 1) for rk, g in zip(rankings, gold, strict=True)) / len(gold)
    return {"recall@1": round(r1, 3), "recall@3": round(r3, 3), "mrr": round(mrr, 3)}


EMBED_VECTORS: dict[str, list[list[float]]] = {}  # векторы держим только в памяти: в кэш — метрики


def cached_embed(key: str, inputs: list[str]) -> dict[str, Any]:
    rec = CACHE.get(key)
    if key in EMBED_VECTORS:
        return {**(rec or {}), "embeddings": EMBED_VECTORS[key]}
    if rec and rec.get("status") == 200 and rec.get("embeddings"):
        EMBED_VECTORS[key] = rec["embeddings"]
        return rec
    res = sc.embed(EMBED_MODEL, inputs)
    vectors = res.pop("embeddings") or []
    EMBED_VECTORS[key] = vectors
    rec = CACHE.put(
        key,
        {
            "kind": "embed",
            "model": EMBED_MODEL,
            **res,
            "n_inputs": len(inputs),
            "dim": len(vectors[0]) if vectors else None,
        },
    )
    return {**rec, "embeddings": vectors}


def cmd_embed(args: argparse.Namespace) -> dict[str, Any]:
    """Метрики поиска; считаются один раз и кэшируются (ключ embed_metrics), векторы в кэш не пишем."""
    cached = CACHE.get("embed_metrics")
    if cached and not getattr(args, "force_embed", False):
        return {k: v for k, v in cached.items() if k not in ("key", "kind")}
    data = yaml.safe_load((OUT_DIR / "embeddings.yaml").read_text(encoding="utf-8"))
    pairs, distractors = data["pairs"], data["distractors"]
    corpus = [p["positive"] for p in pairs] + distractors
    queries = [p["query"] for p in pairs]
    gold = list(range(len(pairs)))
    doc_emb = np.array(cached_embed("embed|corpus", corpus)["embeddings"])
    results: dict[str, Any] = {"n_queries": len(queries), "n_corpus": len(corpus)}
    latency = {}
    ranking_sets: dict[str, list[list[int]]] = {}
    for name, qs in [("qwen3_raw", queries), ("qwen3_instruct", [QWEN_QUERY_PREFIX + q for q in queries])]:
        rec = cached_embed(f"embed|queries|{name}", qs)
        latency[name] = rec["latency_s"]
        q = np.array(rec["embeddings"])
        sims = (q / np.linalg.norm(q, axis=1, keepdims=True)) @ (
            doc_emb / np.linalg.norm(doc_emb, axis=1, keepdims=True)
        ).T
        ranking_sets[name] = [ranks_from_scores(row) for row in sims]
        results[name] = recall_metrics(ranking_sets[name], gold)
    ranking_sets["bm25"] = [ranks_from_scores(bm25_scores(q, corpus)) for q in queries]
    results["bm25"] = recall_metrics(ranking_sets["bm25"], gold)
    # Гибрид: reciprocal rank fusion (k=60) эмбеддингов с инструкцией и BM25.
    fused = []
    for a, b in zip(ranking_sets["qwen3_instruct"], ranking_sets["bm25"], strict=True):
        score: dict[int, float] = defaultdict(float)
        for rk in (a, b):
            for pos, doc in enumerate(rk):
                score[doc] += 1 / (60 + pos + 1)
        fused.append(sorted(score, key=lambda d: -score[d]))
    results["hybrid_rrf"] = recall_metrics(fused, gold)
    misses = []
    for i, rk in enumerate(ranking_sets["qwen3_instruct"]):
        if rk[0] != i:
            misses.append({"query": queries[i], "top1": corpus[rk[0]][:120], "gold_rank": rk.index(i) + 1})
    results["qwen3_instruct_misses"] = misses
    results["dim"] = int(doc_emb.shape[1])
    results["latency_s"] = {"corpus_batch": CACHE.items["embed|corpus"]["latency_s"], **latency}
    print(json.dumps({k: v for k, v in results.items() if k != "qwen3_instruct_misses"}, ensure_ascii=False))
    CACHE.put("embed_metrics", {"kind": "embed_metrics", **results})
    return results


def option_diversity() -> dict[str, Any]:
    """Различимость вариантов: максимальный косинус между тезисами 4 вариантов (меньше — разнообразнее)."""
    texts: list[str] = []
    owners: list[str] = []
    for r in sorted(CACHE.items.values(), key=lambda r: r.get("config", "")):
        if r.get("kind") != "run" or r["prompt_id"] != "options":
            continue
        obj, _ = sc.extract_json(r.get("content"))
        opts = (obj or {}).get("options") if isinstance(obj, dict) else None
        if not opts or len(opts) < 2:
            continue
        for o in opts:
            texts.append(f"{o.get('title', '')}. {o.get('thesis', '')}")
            owners.append(r["config"])
    if not texts:
        return {}
    digest = hashlib.sha1("\n".join(texts).encode()).hexdigest()[:12]
    done = CACHE.get(f"option_diversity|{digest}")
    if done:
        return done["result"]
    rec = cached_embed(f"embed|options|{digest}", texts)
    emb = np.array(rec["embeddings"])
    emb = emb / np.linalg.norm(emb, axis=1, keepdims=True)
    out: dict[str, Any] = {}
    for cfg in sorted(set(owners)):
        idx = [i for i, o in enumerate(owners) if o == cfg]
        sims = emb[idx] @ emb[idx].T
        upper = sims[np.triu_indices(len(idx), k=1)]
        out[cfg] = {"max_cos": round(float(upper.max()), 3), "mean_cos": round(float(upper.mean()), 3)}
    CACHE.put(f"option_diversity|{digest}", {"kind": "option_diversity", "result": out})
    return out


# --------------------------------------------------------------------------- vision


def make_screen_png() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (720, 1000), "white")
    d = ImageDraw.Draw(img)
    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    try:
        big, mid, small = (ImageFont.truetype(font_path, s) for s in (48, 30, 22))
    except OSError:
        big = mid = small = ImageFont.load_default()
    d.text((40, 60), "Антиспам+", fill="black", font=big)
    d.text((40, 150), "Блокирует 100% спам-звонков", fill="black", font=mid)
    d.text((40, 210), "Фильтрация в сети оператора — интернет не нужен", fill="#333333", font=small)
    d.rounded_rectangle((40, 300, 680, 380), radius=20, fill="#00a650")
    d.text((120, 322), "Подключить за 99 ₽/мес", fill="white", font=mid)
    d.text((40, 400), "Первые 14 дней бесплатно для новых абонентов", fill="#888888", font=small)
    d.rounded_rectangle((40, 480, 680, 560), radius=12, outline="#cccccc", width=2)
    d.text((60, 505), "Белый список банков и курьеров — скоро", fill="#333333", font=small)
    d.ellipse((600, 500, 650, 540), fill="#dddddd")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class VisionElement(Strict):
    text: str
    kind: Literal["title", "text", "button", "toggle", "other"]


class VisionOut(Strict):
    elements: list[VisionElement]
    primary_cta: str
    risky_promises: list[str]


VISION_EXPECTED = ["Антиспам+", "100%", "99 ₽", "14 дней", "Белый список"]


def cmd_vision(args: argparse.Namespace) -> list[dict[str, Any]]:
    png = base64.b64encode(make_screen_png()).decode()
    schema = sc.inline_refs(VisionOut.model_json_schema())
    prompt = (
        "Это экран мобильного приложения. Перечисли только то, что действительно видно на экране: элементы "
        "с текстом дословно и типом, главную кнопку действия и обещания, которые требуют проверки по "
        "описанию продукта. Ответь только JSON по схеме:\n" + json.dumps(schema, ensure_ascii=False)
    )
    out = []
    for model in VISION_MODELS:
        key = f"vision|{model}"
        rec = CACHE.get(key)
        if not rec or (args.retry_errors and rec.get("status") != 200):
            res = sc.chat(
                model,
                [{"role": "user", "content": prompt, "images": [png]}],
                think=False,
                fmt=schema,
                options={"temperature": 0.0},
                timeout=300,
            )
            res.pop("logprobs", None)
            obj, mode = sc.extract_json(res["content"])
            try:
                VisionOut.model_validate(obj)
                valid = True
            except ValidationError:
                valid = False
            text = _norm(json.dumps(obj, ensure_ascii=False)) if obj else _norm(res["content"] or "")
            found = [e for e in VISION_EXPECTED if _norm(e) in text]
            rec = CACHE.put(
                key,
                {
                    "kind": "vision",
                    "model": model,
                    **res,
                    "parse_mode": mode,
                    "valid_lenient": valid,
                    "expected_found": found,
                    "expected_total": len(VISION_EXPECTED),
                    "cost_usd": call_cost(model, res["prompt_eval_count"], res["eval_count"]),
                },
            )
        print(
            f"vision {model:24s} status={rec['status']} valid={rec['valid_lenient']} "
            f"found={len(rec['expected_found'])}/{rec['expected_total']} {rec['latency_s']}s"
        )
        out.append(rec)
    return out


# --------------------------------------------------------------------------- report


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return round(sum(xs) / len(xs), 3) if xs else None


def revalidate(runs: list[dict[str, Any]]) -> None:
    """Пересчёт валидности и авто-проверок по сохранённому тексту ответа (без вызовов моделей),
    чтобы метрики не зависели от версии извлекателя JSON на момент вызова."""
    prompts = {p["id"]: p for p in load_prompts()}
    for r in runs:
        if r.get("status") != 200:
            continue
        check = validate(r["prompt_id"], r.get("content"))
        parsed = check.pop("parsed", None)
        r.update(check)
        r["auto"] = auto_check(prompts[r["prompt_id"]], parsed)


def aggregate() -> dict[str, Any]:
    runs = [r for r in CACHE.items.values() if r.get("kind") == "run"]
    revalidate(runs)
    judges = [r for r in CACHE.items.values() if r.get("kind") == "judge"]
    hard_ids = {p["id"] for p in load_prompts() if p.get("hard")}
    per_cfg: dict[str, Any] = {}
    for cfg in CONFIGS:
        rs = [r for r in runs if r["config"] == cfg.name]
        if not rs:
            continue
        ok = [r for r in rs if r["status"] == 200]
        lat = [r["latency_s"] for r in ok]
        j1 = {r["prompt_id"]: r["scores"].get(cfg.name) for r in judges if r["judge"] == JUDGE_PRIMARY}
        j2 = {r["prompt_id"]: r["scores"].get(cfg.name) for r in judges if r["judge"] == JUDGE_SECOND}
        autos = [(r.get("auto") or {}).get("auto") for r in ok if r.get("auto")]
        per_cfg[cfg.name] = {
            "model": cfg.model,
            "think": cfg.think,
            "stage": cfg.stage,
            "n": len(rs),
            "http_ok": len(ok),
            "valid_strict": round(sum(r["valid_strict"] for r in ok) / len(rs), 3),
            "valid_lenient": round(sum(r["valid_lenient"] for r in ok) / len(rs), 3),
            "parse_modes": dict(
                sorted(
                    {
                        m: sum(1 for r in ok if r["parse_mode"] == m) for m in {r["parse_mode"] for r in ok}
                    }.items(),
                    key=lambda kv: str(kv[0]),
                )
            ),
            "latency_p50_s": round(sc.percentile(lat, 0.5) or 0, 2),
            "latency_max_s": round(max(lat), 2) if lat else None,
            "prompt_tokens_mean": _mean([r["prompt_eval_count"] for r in ok]),
            "eval_tokens_mean": _mean([r["eval_count"] for r in ok]),
            "eval_tokens_max": max((r["eval_count"] or 0) for r in ok) if ok else None,
            "judge_primary_mean": _mean(list(j1.values())),
            "judge_primary_hard_mean": _mean([v for k, v in j1.items() if k in hard_ids]),
            "judge_second_hard_mean": _mean(list(j2.values())),
            "auto_mean": _mean(autos),
            "cost_usd_total": round(sum(r["cost_usd"] for r in ok if not math.isnan(r["cost_usd"])), 6),
            "cost_usd_per_call": round(sum(r["cost_usd"] for r in ok) / len(ok), 6) if ok else None,
            "logprobs_returned": any(r.get("logprobs_returned") for r in ok),
            "errors": sorted({r["error"] for r in rs if r.get("error")}),
        }
    per_prompt: dict[str, Any] = defaultdict(dict)
    for r in runs:
        j = next(
            (x for x in judges if x["judge"] == JUDGE_PRIMARY and x["prompt_id"] == r["prompt_id"]), None
        )
        per_prompt[r["prompt_id"]][r["config"]] = {
            "valid_lenient": r["valid_lenient"],
            "valid_strict": r["valid_strict"],
            "latency_s": r["latency_s"],
            "eval_count": r["eval_count"],
            "auto": (r.get("auto") or {}).get("auto"),
            "judge": (j or {}).get("scores", {}).get(r["config"]),
            "errors": r.get("errors"),
        }
    # Согласие судей на трудных промптах.
    agreement = []
    for pid in hard_ids:
        a = next((x for x in judges if x["judge"] == JUDGE_PRIMARY and x["prompt_id"] == pid), None)
        b = next((x for x in judges if x["judge"] == JUDGE_SECOND and x["prompt_id"] == pid), None)
        if a and b:
            for cfg_name in a["scores"]:
                if cfg_name in b["scores"]:
                    agreement.append((a["scores"][cfg_name], b["scores"][cfg_name]))
    judge_agreement = None
    if len(agreement) >= 3:
        xa, xb = np.array(agreement, dtype=float).T
        judge_agreement = {
            "pairs": len(agreement),
            "exact": round(float(np.mean(xa == xb)), 3),
            "within_1": round(float(np.mean(np.abs(xa - xb) <= 1)), 3),
            "pearson": round(float(np.corrcoef(xa, xb)[0, 1]), 3) if xa.std() and xb.std() else None,
        }
    probes = {
        r["model"]: {
            k: r.get(k)
            for k in ("status", "format_enforced", "logprobs_returned", "error", "latency_s", "limit_headers")
        }
        for r in CACHE.items.values()
        if r.get("kind") == "probe"
    }
    judge_cost = round(
        sum(r.get("cost_usd") or 0 for r in judges if not math.isnan(r.get("cost_usd") or 0)), 5
    )
    vision = [
        {
            k: r.get(k)
            for k in (
                "model",
                "status",
                "valid_lenient",
                "parse_mode",
                "expected_found",
                "expected_total",
                "latency_s",
                "prompt_eval_count",
                "eval_count",
                "error",
            )
        }
        for r in CACHE.items.values()
        if r.get("kind") == "vision"
    ]
    limit_headers_seen = sorted({h for r in CACHE.items.values() for h in (r.get("limit_headers") or {})})
    statuses = defaultdict(int)
    for r in CACHE.items.values():
        statuses[str(r.get("status"))] += 1
    return {
        "per_config": per_cfg,
        "per_prompt": per_prompt,
        "judge_agreement": judge_agreement,
        "judge_cost_usd": judge_cost,
        "probes": probes,
        "vision": vision,
        "limit_headers_seen": limit_headers_seen,
        "status_counts": dict(statuses),
        "calls_in_cache": len(CACHE.items),
    }


def markdown_tables(agg: dict[str, Any]) -> str:
    lines = [
        "| Конфигурация | n | JSON строго | JSON после извлечения | p50, с | max, с | вход ток. | выход ток. "
        "| судья 1–5 | судья, трудные | авто 0–1 | $ за вызов |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, a in agg["per_config"].items():
        cpc = a["cost_usd_per_call"]
        lines.append(
            f"| {name} | {a['n']} | {a['valid_strict']:.0%} | {a['valid_lenient']:.0%} | {a['latency_p50_s']} "
            f"| {a['latency_max_s']} | {a['prompt_tokens_mean']} | {a['eval_tokens_mean']} "
            f"| {a['judge_primary_mean']} | {a['judge_primary_hard_mean']} | {a['auto_mean']} "
            f"| {cpc if cpc is None else f'{cpc:.5f}'} |"
        )
    lines += [
        "",
        "| Промпт | " + " | ".join(agg["per_config"]) + " |",
        "|---|" + "---|" * len(agg["per_config"]),
    ]
    for pid, row in agg["per_prompt"].items():
        cells = []
        for name in agg["per_config"]:
            c = row.get(name)
            if not c:
                cells.append("—")
                continue
            mark = "✓" if c["valid_lenient"] else "✗"
            auto = "" if c["auto"] is None else f" a{c['auto']:.2f}"
            cells.append(f"{mark} j{c['judge']}{auto}")
        lines.append(f"| {pid} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> None:
    agg = aggregate()
    agg["embeddings"] = cmd_embed(args) if (OUT_DIR / "embeddings.yaml").exists() else None
    agg["option_diversity"] = option_diversity()
    agg["prices_usd_per_1m"] = {k: {"input": v[0], "output": v[1]} for k, v in PRICES.items()}
    agg["prices_source"] = "https://ollama.com/pricing (2026-11-04, Pay as you go)"
    agg["live_calls_this_run"] = sc.STATS.live
    sc.write_json(OUT_DIR / "results.json", agg)
    print(markdown_tables(agg))
    print("\njudge agreement:", agg["judge_agreement"], "judge cost:", agg["judge_cost_usd"])
    print("option diversity:", agg["option_diversity"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["probe", "run", "judge", "embed", "vision", "report", "all"])
    parser.add_argument("--only", help="через запятую: имена конфигураций для run")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument(
        "--force-embed", action="store_true", help="пересчитать эмбеддинги (4 локальных вызова)"
    )
    args = parser.parse_args()
    steps = {
        "probe": [cmd_probe],
        "run": [cmd_run],
        "judge": [cmd_judge],
        "embed": [cmd_embed],
        "vision": [cmd_vision],
        "report": [cmd_report],
        "all": [cmd_probe, cmd_run, cmd_judge, cmd_vision, cmd_report],
    }[args.command]
    for step in steps:
        step(args)
    print(f"\nживых вызовов за запуск: {sc.STATS.total_live} {sc.STATS.live}; из кэша: {sc.STATS.cached}")


if __name__ == "__main__":
    main()
