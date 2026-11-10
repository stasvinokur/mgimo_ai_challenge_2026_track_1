"""TASK-3. Спайк decide(): «умный if» для узких частых решений.

Сравниваем на размеченном наборе evals/spikes/decide/dataset.jsonl (38 русских примеров):
  clef — модель решений Clef (Cloudflare) в Ollama Cloud через System One API демона (`/v1/systemone`).
         Тот же протокол, что у Jev: state + questions {choice|noul|score}; вероятности — от модели.
         Все варианты одного примера (база, перестановка, переименование, отрицание) уходят в одном запросе
         как отдельные вопросы; независимость вопросов проверяется на 2 примерах отдельными запросами.
  llm  — LLM с выводом из перечня (enum JSON Schema через `format`), вероятность — из `logprobs`
         первого токена значения `choice`, плюс самооценка `confidence` в том же ответе.
         Облачные модели Ollama `format` и `logprobs` игнорируют (см. model_bench probe), поэтому
         llm-вариант меряем на локальной gpt-oss:20b.

Подкоманды: run (clef + llm), report (агрегаты → evals/spikes/decide/results.json). Кэш вызовов —
evals/spikes/decide/runs.jsonl, повторный запуск бесплатен.

Запуск: `uv run python scripts/spikes/decide_bench.py run && uv run python scripts/spikes/decide_bench.py report`
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spike_common as sc

OUT_DIR = sc.ROOT / "evals" / "spikes" / "decide"
DATASET = OUT_DIR / "dataset.jsonl"
CACHE = sc.Cache(OUT_DIR / "runs.jsonl")
CLEF_MODEL = "clef:cloud"
LLM_MODELS = {"gpt-oss-20b-local": ("gpt-oss:20b", "low")}
CLEF_PRICE_IN = 0.24  # USD за 1 млн входных токенов, ollama.com/pricing (2026-11-04); выход не тарифицируется
THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.9]

CRITERIA = {
    "claim_type": {
        "fact": "наблюдение, прямо записанное в источнике",
        "derived_metric": "показатель, вычисленный из фактов источника",
        "hypothesis": "предположение о причине или механизме",
        "forecast": "утверждение о будущем",
        "assumption": "посылка, принятая для расчёта или плана без проверки",
        "unknown": "явно названный пробел в данных",
        "synthetic": "высказывание модельного (ИИ) респондента, а не реального человека",
    },
    "objection": {
        "да": "есть сильное возражение, опирающееся на данные контекста",
        "нет": "данные контекста не дают сильного возражения: его нет, оно основано на мнении "
        "или данные к варианту не относятся",
    },
    "promise": {
        "да": "функция из описания продукта полностью подтверждает обещание",
        "нет": "функции нет, она ещё не запущена или подтверждает обещание лишь частично",
    },
}
NEGATED = {
    "objection": "Верно ли, что у варианта НЕТ сильного возражения, опирающегося на данные из контекста?",
    "promise": "Верно ли, что обещание НЕ подтверждено функцией продукта, описанной в контексте?",
}
LETTERS = "ABCDEFG"


def load_dataset() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def permuted(ex: dict[str, Any]) -> list[str]:
    """Детерминированная перестановка вариантов, отличная от исходного порядка."""
    opts = list(ex["options"])
    if len(opts) == 2:
        return opts[::-1]
    rng = random.Random(ex["id"])
    perm = opts[:]
    while perm == opts:
        rng.shuffle(perm)
    return perm


# --------------------------------------------------------------------------- clef (System One)


def clef_questions(ex: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Вопросы одного примера + карта «имя вопроса → вариант теста». Имена нейтральные (q1…),
    чтобы название вопроса не подсказывало модели вариант."""
    crit = CRITERIA[ex["kind"]]
    qs: dict[str, Any] = {}
    meta: dict[str, dict[str, Any]] = {}
    if ex["kind"] == "claim_type":
        qs["q1"] = {
            "type": "choice",
            "instructions": ex["question"],
            "criteria": {k: crit[k] for k in ex["options"]},
        }
        meta["q1"] = {"variant": "base"}
        perm = permuted(ex)
        qs["q2"] = {"type": "choice", "instructions": ex["question"], "criteria": {k: crit[k] for k in perm}}
        meta["q2"] = {"variant": "perm"}
        renamed = {LETTERS[i]: k for i, k in enumerate(ex["options"])}
        qs["q3"] = {
            "type": "choice",
            "instructions": ex["question"],
            "criteria": {letter: crit[k] for letter, k in renamed.items()},
        }
        meta["q3"] = {"variant": "renamed", "map": renamed}
    else:
        yes, no = crit["да"], crit["нет"]
        qs["q1"] = {"type": "noul", "instructions": ex["question"], "criteria": {"true": yes, "false": no}}
        meta["q1"] = {"variant": "base"}
        qs["q2"] = {
            "type": "noul",
            "instructions": NEGATED[ex["kind"]],
            "criteria": {"true": no, "false": yes},
        }
        meta["q2"] = {"variant": "negated"}
        qs["q3"] = {"type": "choice", "instructions": ex["question"], "criteria": {"да": yes, "нет": no}}
        meta["q3"] = {"variant": "choice"}
        qs["q4"] = {"type": "choice", "instructions": ex["question"], "criteria": {"нет": no, "да": yes}}
        meta["q4"] = {"variant": "perm"}
        qs["q5"] = {"type": "choice", "instructions": ex["question"], "criteria": {"A": yes, "B": no}}
        meta["q5"] = {"variant": "renamed", "map": {"A": "да", "B": "нет"}}
    return qs, meta


def parse_clef_answer(ans: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """→ {choice (каноническая метка), p (вероятность выбранного), probs, confidence (поле модели)}.

    Терпим к расхождениям формы ответа (для Jev форма не проверена): нет `type` — определяем по полям;
    `probability` вместо `noul`; нет `probabilities` у choice — берём `confidence` выбранного варианта.
    """
    is_noul = ans.get("type") == "noul" or (("noul" in ans or "probability" in ans) and "choice" not in ans)
    if is_noul:
        p_true = float(ans["noul"] if "noul" in ans else ans["probability"])
        p_yes = 1 - p_true if meta["variant"] == "negated" else p_true
        choice = "да" if p_yes >= 0.5 else "нет"
        return {
            "choice": choice,
            "p": max(p_yes, 1 - p_yes),
            "probs": {"да": p_yes, "нет": 1 - p_yes},
            "confidence": None,
            "raw_noul": p_true,
        }
    mapping = meta.get("map") or {}
    probs = {mapping.get(k, k): float(v) for k, v in (ans.get("probabilities") or {}).items()}
    choice = mapping.get(ans["choice"], ans["choice"])
    if not probs and ans.get("confidence") is not None:
        probs = {choice: float(ans["confidence"])}
    return {"choice": choice, "p": probs.get(choice), "probs": probs, "confidence": ans.get("confidence")}


def run_clef(examples: list[dict[str, Any]], retry_errors: bool) -> None:
    for ex in examples:
        key = f"clef|{ex['id']}"
        rec = CACHE.get(key)
        if rec and not (retry_errors and rec.get("status") != 200):
            continue
        qs, meta = clef_questions(ex)
        res = sc.systemone(CLEF_MODEL, ex["state"], qs)
        rec = {"kind": "clef", "id": ex["id"], "meta": meta, **res}
        CACHE.put(key, rec)
        base = (res.get("answers") or {}).get("q1")
        print(
            f"clef {ex['id']} status={res['status']} {res['latency_s']}s base={base and parse_clef_answer(base, meta['q1'])['choice']} "
            f"gold={ex['label']} {res.get('error') or ''}"
        )
    # Проверка независимости вопросов в одном запросе: базовый вопрос отдельно.
    for ex in [e for e in examples if e["id"] in ("ct04", "ob05")]:
        key = f"clef_single|{ex['id']}"
        if CACHE.get(key):
            continue
        qs, meta = clef_questions(ex)
        res = sc.systemone(CLEF_MODEL, ex["state"], {"q1": qs["q1"]})
        CACHE.put(key, {"kind": "clef_single", "id": ex["id"], "meta": {"q1": meta["q1"]}, **res})
        print(f"clef single {ex['id']} status={res['status']} {res['latency_s']}s")


# --------------------------------------------------------------------------- llm (enum JSON Schema + logprobs)


def llm_messages(
    ex: dict[str, Any], labels: list[str], label_to_canon: dict[str, str]
) -> list[dict[str, str]]:
    crit = CRITERIA[ex["kind"]]
    lines = "\n".join(f"- {lab}: {crit[label_to_canon[lab]]}" for lab in labels)
    system = (
        "Ты — модуль решений. Отвечаешь на узкий вопрос ровно одним вариантом из перечня.\n"
        f"Варианты:\n{lines}\n\n"
        'Ответь только JSON: {"choice": "<вариант из перечня>", "confidence": <число от 0 до 1 — '
        "твоя уверенность, что ответ верный>}."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Контекст:\n{ex['state']}\n\nВопрос: {ex['question']}"},
    ]


def llm_variant(ex: dict[str, Any], variant: str) -> tuple[list[str], dict[str, str]]:
    canon = list(ex["options"])
    if variant == "base":
        return canon, {k: k for k in canon}
    if variant == "perm":
        perm = permuted(ex)
        return perm, {k: k for k in perm}
    if variant == "renamed":
        return [LETTERS[i] for i in range(len(canon))], {LETTERS[i]: k for i, k in enumerate(canon)}
    raise ValueError(variant)


def probs_from_logprobs(logprobs: list[dict[str, Any]] | None, labels: list[str]) -> dict[str, Any] | None:
    """Вероятности вариантов по top_logprobs токена, с которого начинается значение `choice`.

    Склеиваем токены в текст, находим последнее `"choice": "` (последнее — чтобы не попасть в рассуждение
    модели), берём токен, покрывающий первый символ значения, и раскладываем массу альтернатив по
    вариантам, для которых альтернатива — префикс. Возвращает probs (нормированные) и coverage —
    сырую массу, пришедшуюся на допустимые варианты.
    """
    if not logprobs:
        return None
    text, starts = "", []
    for t in logprobs:
        starts.append(len(text))
        text += t.get("token", "")
    matches = list(re.finditer(r'"choice"\s*:\s*"', text))
    if not matches:
        return None
    vstart = matches[-1].end()
    idx = max(i for i, s in enumerate(starts) if s <= vstart)
    if starts[idx] + len(logprobs[idx]["token"]) <= vstart:  # токен кончается ровно перед значением
        idx += 1
        if idx >= len(logprobs):
            return None
    prefix = text[starts[idx] : vstart]
    mass: dict[str, float] = defaultdict(float)
    alts = logprobs[idx].get("top_logprobs") or [logprobs[idx]]
    for alt in alts:
        tok = alt.get("token", "")
        if not tok.startswith(prefix):
            continue
        frag = tok[len(prefix) :]
        if not frag:
            continue
        closed = '"' in frag
        frag = frag.split('"')[0]
        cands = [lab for lab in labels if (lab == frag if closed else lab.startswith(frag))]
        if not cands:
            continue
        for lab in cands:
            mass[lab] += math.exp(alt["logprob"]) / len(cands)
    total = sum(mass.values())
    if total <= 0:
        return None
    return {"probs": {lab: mass.get(lab, 0.0) / total for lab in labels}, "coverage": total}


def run_llm(examples: list[dict[str, Any]], retry_errors: bool) -> None:
    robust_ids = {e["id"] for e in examples if e["kind"] == "claim_type"}
    for name, (model, think) in LLM_MODELS.items():
        for ex in examples:
            variants = ["base", "perm", "renamed"] if ex["id"] in robust_ids else ["base"]
            for variant in variants:
                key = f"llm|{name}|{ex['id']}|{variant}"
                rec = CACHE.get(key)
                if rec and not (retry_errors and rec.get("status") != 200):
                    continue
                labels, to_canon = llm_variant(ex, variant)
                schema = {
                    "type": "object",
                    "properties": {
                        "choice": {"type": "string", "enum": labels},
                        "confidence": {"type": "number"},
                    },
                    "required": ["choice", "confidence"],
                }
                res = sc.chat(
                    model,
                    llm_messages(ex, labels, to_canon),
                    think=think,
                    fmt=schema,
                    options={"temperature": 0.0, "seed": 42, "num_ctx": 8192},
                    logprobs=True,
                    top_logprobs=10,
                    timeout=300,
                )
                lp = res.pop("logprobs")
                obj, mode = sc.extract_json(res["content"])
                raw_choice = obj.get("choice") if isinstance(obj, dict) else None
                lp_probs = probs_from_logprobs(lp, labels)
                rec = {
                    "kind": "llm",
                    "backend": name,
                    "model": model,
                    "id": ex["id"],
                    "variant": variant,
                    **{
                        k: res[k]
                        for k in (
                            "status",
                            "error",
                            "latency_s",
                            "prompt_eval_count",
                            "eval_count",
                            "content",
                        )
                    },
                    "parse_mode": mode,
                    "choice": to_canon.get(raw_choice) if raw_choice else None,
                    "self_confidence": obj.get("confidence") if isinstance(obj, dict) else None,
                    "lp_probs": {to_canon[k]: v for k, v in lp_probs["probs"].items()} if lp_probs else None,
                    "lp_coverage": lp_probs["coverage"] if lp_probs else None,
                    "logprobs_len": len(lp) if lp else 0,
                }
                CACHE.put(key, rec)
                p = (rec["lp_probs"] or {}).get(rec["choice"]) if rec["choice"] else None
                print(
                    f"llm {name} {ex['id']} {variant:8s} status={rec['status']} choice={rec['choice']} "
                    f"gold={ex['label']} p_lp={p if p is None else round(p, 3)} self={rec['self_confidence']} "
                    f"{rec['latency_s']}s"
                )


# --------------------------------------------------------------------------- report


def calibration(rows: list[tuple[float | None, bool]]) -> dict[str, Any]:
    rows = [(c, ok) for c, ok in rows if c is not None]
    out: dict[str, Any] = {"n": len(rows)}
    if not rows:
        return out
    table = []
    n_err = sum(1 for _, ok in rows if not ok)
    for t in THRESHOLDS:
        kept = [ok for c, ok in rows if c >= t]
        caught = sum(1 for c, ok in rows if c < t and not ok)
        table.append(
            {
                "threshold": t,
                "coverage": round(len(kept) / len(rows), 3),
                "accuracy_kept": round(sum(kept) / len(kept), 3) if kept else None,
                "escalated": len(rows) - len(kept),
                "errors_caught": caught,
                "errors_total": n_err,
            }
        )
    out["by_threshold"] = table
    # ECE по 5 корзинам.
    bins: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for c, ok in rows:
        bins[min(int(c * 5), 4)].append((c, ok))
    ece = sum(
        len(b) / len(rows) * abs(sum(ok for _, ok in b) / len(b) - sum(c for c, _ in b) / len(b))
        for b in bins.values()
    )
    out["ece_5bins"] = round(ece, 3)
    out["mean_conf"] = round(sum(c for c, _ in rows) / len(rows), 3)
    out["accuracy"] = round(sum(ok for _, ok in rows) / len(rows), 3)
    return out


def acc(items: list[bool]) -> float | None:
    return round(sum(items) / len(items), 3) if items else None


def variant_agreement(rows: list[tuple[str, str, list[str]]]) -> dict[str, Any]:
    """Согласие вариантов вопроса (база, перестановка, переименование…) как бесплатный сигнал уверенности.

    rows: (база, эталон, ответы всех вариантов включая базу). Если варианты расходятся — эскалация.
    """
    agree = [(b, g) for b, g, all_ in rows if len(set(all_)) == 1]
    disagree = [(b, g) for b, g, all_ in rows if len(set(all_)) > 1]
    errors = sum(1 for b, g, _ in rows if b != g)
    return {
        "n": len(rows),
        "all_agree_share": acc([len(set(a)) == 1 for _, _, a in rows]),
        "accuracy_when_agree": acc([b == g for b, g in agree]),
        "accuracy_when_disagree": acc([b == g for b, g in disagree]),
        "base_errors": errors,
        "base_errors_flagged_by_disagreement": sum(1 for b, g in disagree if b != g),
    }


def systemone_summary(
    records: list[dict[str, Any]],
    singles_records: list[dict[str, Any]],
    data: dict[str, dict[str, Any]],
    model: str,
    price_in: float,
) -> dict[str, Any]:
    """Метрики для бэкенда с System One API (Clef, Jev): точность, устойчивость, калибровка, латентность."""
    clef_rows = []
    for rec in records:
        ex = data[rec["id"]]
        parsed = {q: parse_clef_answer(a, rec["meta"][q]) for q, a in rec["answers"].items()}
        by_var = {rec["meta"][q]["variant"]: v for q, v in parsed.items()}
        clef_rows.append(
            {
                "ex": ex,
                "by_var": by_var,
                "latency": rec["latency_s"],
                "usage": rec.get("usage"),
                "n_questions": len(parsed),
            }
        )
    clef: dict[str, Any] = {"model": model, "n": len(clef_rows)}
    if clef_rows:
        for kind in ["claim_type", "objection", "promise", "all"]:
            rows = [r for r in clef_rows if kind == "all" or r["ex"]["kind"] == kind]
            clef[f"accuracy_{kind}"] = acc([r["by_var"]["base"]["choice"] == r["ex"]["label"] for r in rows])
        variants = sorted({v for r in clef_rows for v in r["by_var"]} - {"base"})
        robustness = {}
        for v in variants:
            rows = [r for r in clef_rows if v in r["by_var"]]
            robustness[v] = {
                "n": len(rows),
                "changed_vs_base": acc(
                    [r["by_var"][v]["choice"] != r["by_var"]["base"]["choice"] for r in rows]
                ),
                "accuracy": acc([r["by_var"][v]["choice"] == r["ex"]["label"] for r in rows]),
                "mean_abs_dp": round(
                    sum(
                        abs(
                            (r["by_var"][v]["probs"].get(r["ex"]["label"]) or 0)
                            - (r["by_var"]["base"]["probs"].get(r["ex"]["label"]) or 0)
                        )
                        for r in rows
                    )
                    / len(rows),
                    3,
                ),
            }
        clef["robustness"] = robustness
        clef["variant_agreement"] = variant_agreement(
            [
                (r["by_var"]["base"]["choice"], r["ex"]["label"], [v["choice"] for v in r["by_var"].values()])
                for r in clef_rows
            ]
        )
        clef["calibration_maxprob"] = calibration(
            [(r["by_var"]["base"]["p"], r["by_var"]["base"]["choice"] == r["ex"]["label"]) for r in clef_rows]
        )
        clef["calibration_confidence_field"] = calibration(
            [
                (r["by_var"]["base"]["confidence"], r["by_var"]["base"]["choice"] == r["ex"]["label"])
                for r in clef_rows
                if r["ex"]["kind"] == "claim_type"
            ]
        )
        lat = [r["latency"] for r in clef_rows]
        clef["latency_p50_s"] = round(sc.percentile(lat, 0.5), 3)
        clef["latency_max_s"] = round(max(lat), 3)
        clef["input_tokens_mean_per_request"] = round(
            sum((r["usage"] or {}).get("input_tokens", 0) for r in clef_rows) / len(clef_rows), 1
        )
        clef["questions_per_request_mean"] = round(
            sum(r["n_questions"] for r in clef_rows) / len(clef_rows), 2
        )
        clef["errors"] = [
            {
                "id": r["ex"]["id"],
                "gold": r["ex"]["label"],
                "got": r["by_var"]["base"]["choice"],
                "p": round(r["by_var"]["base"]["p"], 3),
                "note": r["ex"]["note"],
            }
            for r in clef_rows
            if r["by_var"]["base"]["choice"] != r["ex"]["label"]
        ]
        singles = []
        for rec in singles_records:
            packed = next(r for r in clef_rows if r["ex"]["id"] == rec["id"])
            single = parse_clef_answer(rec["answers"]["q1"], rec["meta"]["q1"])
            dp = max(
                abs(single["probs"].get(k, 0) - packed["by_var"]["base"]["probs"].get(k, 0))
                for k in single["probs"]
            )
            tokens = (rec.get("usage") or {}).get("input_tokens")
            singles.append(
                {
                    "id": rec["id"],
                    "max_abs_dprob_single_vs_packed": round(dp, 4),
                    "same_choice": single["choice"] == packed["by_var"]["base"]["choice"],
                    "input_tokens_single": tokens,
                    "latency_single_s": rec["latency_s"],
                    "cost_usd_single": round((tokens or 0) * price_in / 1e6, 7),
                }
            )
        clef["independence_check"] = singles
    return clef


def report() -> dict[str, Any]:
    data = {e["id"]: e for e in load_dataset()}
    out: dict[str, Any] = {"dataset": {"n": len(data), "by_kind": defaultdict(int)}}
    for e in data.values():
        out["dataset"]["by_kind"][e["kind"]] += 1
    clef_recs = [r for r in CACHE.items.values() if r.get("kind") == "clef" and r.get("status") == 200]
    clef_single = [
        r for r in CACHE.items.values() if r.get("kind") == "clef_single" and r.get("status") == 200
    ]
    out["clef"] = systemone_summary(clef_recs, clef_single, data, CLEF_MODEL, CLEF_PRICE_IN)
    # ---- llm
    llm_out: dict[str, Any] = {}
    for name in LLM_MODELS:
        recs = [r for r in CACHE.items.values() if r.get("kind") == "llm" and r["backend"] == name]
        base = [r for r in recs if r["variant"] == "base" and r.get("status") == 200]
        if not base:
            continue
        res: dict[str, Any] = {"model": LLM_MODELS[name][0], "think": LLM_MODELS[name][1], "n": len(base)}
        res["valid_choice"] = acc([r["choice"] is not None for r in base])
        for kind in ["claim_type", "objection", "promise", "all"]:
            rows = [r for r in base if kind == "all" or data[r["id"]]["kind"] == kind]
            res[f"accuracy_{kind}"] = acc([r["choice"] == data[r["id"]]["label"] for r in rows])
        res["logprobs_available"] = acc([r["lp_probs"] is not None for r in base])
        res["lp_coverage_mean"] = round(sum(r["lp_coverage"] or 0 for r in base) / len(base), 3)
        res["lp_agrees_with_choice"] = acc(
            [
                max(r["lp_probs"], key=r["lp_probs"].get) == r["choice"]
                for r in base
                if r["lp_probs"] and r["choice"]
            ]
        )
        res["calibration_logprobs"] = calibration(
            [((r["lp_probs"] or {}).get(r["choice"]), r["choice"] == data[r["id"]]["label"]) for r in base]
        )
        res["calibration_self_report"] = calibration(
            [
                (
                    float(r["self_confidence"]) if isinstance(r["self_confidence"], int | float) else None,
                    r["choice"] == data[r["id"]]["label"],
                )
                for r in base
            ]
        )
        robustness = {}
        for v in ["perm", "renamed"]:
            pairs = []
            for r in [x for x in recs if x["variant"] == v and x.get("status") == 200]:
                b = next((x for x in base if x["id"] == r["id"]), None)
                if b:
                    pairs.append((b, r))
            if pairs:
                robustness[v] = {
                    "n": len(pairs),
                    "changed_vs_base": acc([b["choice"] != r["choice"] for b, r in pairs]),
                    "accuracy": acc([r["choice"] == data[r["id"]]["label"] for _, r in pairs]),
                    "base_accuracy_same_subset": acc(
                        [b["choice"] == data[b["id"]]["label"] for b, _ in pairs]
                    ),
                }
        res["robustness"] = robustness
        agree_rows = []
        for b in base:
            others = [x["choice"] for x in recs if x["id"] == b["id"] and x["variant"] != "base"]
            if others:
                agree_rows.append((b["choice"], data[b["id"]]["label"], [b["choice"], *others]))
        res["variant_agreement"] = variant_agreement(agree_rows)
        lat = [r["latency_s"] for r in base]
        res["latency_p50_s"] = round(sc.percentile(lat, 0.5), 2)
        res["latency_max_s"] = round(max(lat), 2)
        res["prompt_tokens_mean"] = round(sum(r["prompt_eval_count"] or 0 for r in base) / len(base), 1)
        res["eval_tokens_mean"] = round(sum(r["eval_count"] or 0 for r in base) / len(base), 1)
        res["errors"] = [
            {
                "id": r["id"],
                "gold": data[r["id"]]["label"],
                "got": r["choice"],
                "p_lp": round((r["lp_probs"] or {}).get(r["choice"], float("nan")), 3)
                if r["choice"]
                else None,
                "self": r["self_confidence"],
                "note": data[r["id"]]["note"],
            }
            for r in base
            if r["choice"] != data[r["id"]]["label"]
        ]
        llm_out[name] = res
    out["llm"] = llm_out
    out["dataset"]["by_kind"] = dict(out["dataset"]["by_kind"])
    out["live_calls_this_run"] = sc.STATS.live
    sc.write_json(OUT_DIR / "results.json", out)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["run", "clef", "llm", "report"])
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()
    examples = load_dataset()
    if args.command in ("run", "clef"):
        run_clef(examples, args.retry_errors)
    if args.command in ("run", "llm"):
        run_llm(examples, args.retry_errors)
    if args.command == "report":
        out = report()
        print(json.dumps({k: v for k, v in out.items()}, ensure_ascii=False, indent=1)[:6000])
    print(f"\nживых вызовов за запуск: {sc.STATS.total_live} {sc.STATS.live}; из кэша: {sc.STATS.cached}")


if __name__ == "__main__":
    main()
