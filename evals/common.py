"""Общие функции стенда сравнения: загрузка брифов и материалов, конфиг моделей, вызов Ollama.

Модуль намеренно не зависит от `src/strategist`: стенд должен одинаково оценивать любой подход
(простой чат, агент, аналитик) и не меняться вместе с кодом агента.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = ROOT / "evals"
BRIEFS_DIR = EVALS_DIR / "briefs"
RESULTS_DIR = EVALS_DIR / "results"


# ----------------------------------------------------------------------------- конфиг


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидался YAML-словарь")
    return data


def models_config() -> dict[str, Any]:
    return load_yaml(ROOT / "config" / "models.yaml")


def prices_config() -> dict[str, Any]:
    path = ROOT / "config" / "prices.yaml"
    return load_yaml(path) if path.exists() else {}


def ollama_host(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or models_config()
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:  # pragma: no cover - python-dotenv входит в зависимости проекта
        pass
    return os.environ.get(cfg.get("host_env", "OLLAMA_HOST")) or cfg.get(
        "default_host", "http://127.0.0.1:11434"
    )


def role(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    roles = cfg.get("roles", {})
    if name not in roles:
        raise KeyError(f"В config/models.yaml нет роли {name!r}")
    return dict(roles[name])


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
    """Справочная стоимость по config/prices.yaml (допущение, не счёт провайдера)."""
    prices = prices_config()
    table = prices.get("models", {})
    rate = table.get(model) or prices.get("default") or {"input": 0.0, "output": 0.0, "estimated": True}
    usd = input_tokens / 1e6 * float(rate.get("input", 0)) + output_tokens / 1e6 * float(
        rate.get("output", 0)
    )
    rub_rate = float(prices.get("currency_rate_rub_per_usd", 0) or 0)
    return {
        "usd": round(usd, 6),
        "rub": round(usd * rub_rate, 4),
        "estimated": bool(rate.get("estimated", True)),
        "basis": f"config/prices.yaml (as_of {prices.get('as_of', '?')}): "
        f"${rate.get('input', 0)}/{rate.get('output', 0)} за 1 млн токенов вход/выход",
    }


# ----------------------------------------------------------------------------- брифы и материалы


@dataclass
class Material:
    rel: str  # путь относительно каталога брифа, например materials/cohorts.csv
    path: Path
    sha256: str
    size: int
    meta: dict[str, Any]
    text: str  # отрисованный для модели текст (без заголовка файла)

    @property
    def synthetic(self) -> bool | None:
        value = self.meta.get("synthetic")
        return value if isinstance(value, bool) else None


@dataclass
class Brief:
    id: str
    dir: Path
    request: str
    meta: dict[str, Any]
    materials: list[Material] = field(default_factory=list)


def list_briefs() -> list[str]:
    return sorted(p.name for p in BRIEFS_DIR.iterdir() if p.is_dir() and (p / "brief.md").exists())


def load_brief(brief_id: str) -> Brief:
    bdir = BRIEFS_DIR / brief_id
    if not (bdir / "brief.md").exists():
        raise FileNotFoundError(f"Нет брифа {brief_id!r}: {bdir / 'brief.md'}")
    meta_path = bdir / "meta.yaml"
    meta = load_yaml(meta_path) if meta_path.exists() else {}
    brief = Brief(
        id=brief_id, dir=bdir, request=(bdir / "brief.md").read_text(encoding="utf-8").strip(), meta=meta
    )
    mat_dir = bdir / "materials"
    if mat_dir.exists():
        for path in sorted(mat_dir.iterdir()):
            if path.name.endswith(".meta.yaml") or path.name.startswith(".") or not path.is_file():
                continue
            brief.materials.append(_load_material(bdir, path))
    return brief


def _load_material(bdir: Path, path: Path) -> Material:
    data = path.read_bytes()
    side = path.with_name(path.name + ".meta.yaml")
    meta: dict[str, Any] = load_yaml(side) if side.exists() else {}
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        text = data.decode("utf-8")
        if suffix == ".md" and text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end > 0:
                meta = yaml.safe_load(text[4:end]) or {}
    elif suffix == ".csv":
        text = csv_to_markdown(data.decode("utf-8"))
    elif suffix == ".xlsx":
        text = xlsx_to_markdown(path)
    elif suffix == ".docx":
        text = docx_to_text(path)
    else:
        text = f"(файл {path.name}: формат {suffix} не поддерживается стендом, содержимое не передано)"
    return Material(
        rel=path.relative_to(bdir).as_posix(),
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        meta=meta,
        text=text.strip(),
    )


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def markdown_table(rows: list[list[Any]]) -> str:
    if not rows:
        return "(пустая таблица)"
    width = max(len(r) for r in rows)
    norm = [[_cell(v) for v in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(norm[0]) + " |", "|" + "---|" * width]
    lines.extend("| " + " | ".join(r) + " |" for r in norm[1:])
    return "\n".join(lines)


def csv_to_markdown(text: str) -> str:
    rows = list(csv.reader(io.StringIO(text)))
    return markdown_table(rows)


def xlsx_to_markdown(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True) if any(v is not None for v in r)]
        parts.append(f"Лист «{ws.title}»:\n\n{markdown_table(rows)}")
    wb.close()
    return "\n\n".join(parts)


def docx_to_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    lines = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style is not None else ""
        if style.startswith("heading"):
            level = re.sub(r"\D", "", style) or "1"
            lines.append("#" * (int(level) + 1) + " " + text)
        elif "list" in style:
            lines.append("- " + text)
        else:
            lines.append(text)
    for table in doc.tables:
        lines.append(markdown_table([[c.text for c in row.cells] for row in table.rows]))
    return "\n\n".join(lines)


def render_material(mat: Material) -> str:
    """Блок материала для промпта: имя файла, сведения о файле (для не-markdown) и содержимое."""
    head = f"### Файл: {mat.rel}"
    if mat.path.suffix.lower() == ".md":
        return f"{head}\n\n{mat.text}"
    side = mat.path.with_name(mat.path.name + ".meta.yaml")
    info = side.read_text(encoding="utf-8").strip() if side.exists() else "(сведений о файле нет)"
    return f"{head}\n\nСведения о файле ({side.name}):\n```yaml\n{info}\n```\n\nСодержимое:\n\n{mat.text}"


def render_materials(brief: Brief) -> str:
    return "\n\n".join(render_material(m) for m in brief.materials)


def materials_corpus(brief: Brief) -> str:
    """Полный текст, по которому проверяются цитаты: запрос + все материалы."""
    return f"### Файл: brief.md\n\n{brief.request}\n\n{render_materials(brief)}"


def normalize_for_match(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[«»\"“”„'`*_|>#\\]", " ", text)
    text = re.sub(r"[\u00a0\u2009\u202f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_in_corpus(quote: str, corpus_norm: str) -> bool:
    """Мягкая проверка дословности: цитата (или её длинный фрагмент) есть в нормализованном корпусе."""
    q = normalize_for_match(quote)
    if len(q) < 8:
        return False
    if q in corpus_norm:
        return True
    # Цитаты из нескольких строк таблицы и с многоточиями: каждая строка/фрагмент длиной ≥ 12 символов
    # должна найтись дословно (строки таблицы в корпусе не обязательно идут подряд).
    raw_pieces = re.split(r"\n|\.\.\.|…|\[…\]", quote)
    pieces = [p for p in (normalize_for_match(x) for x in raw_pieces) if len(p) >= 12]
    return bool(pieces) and all(p in corpus_norm for p in pieces)


# ----------------------------------------------------------------------------- вызов моделей


@dataclass
class ChatResult:
    model: str
    content: str = ""
    thinking: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    attempts: int = 0
    done_reason: str | None = None
    ollama_durations_s: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.content)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def chat(
    model: str,
    messages: list[dict[str, str]],
    *,
    host: str,
    think: bool | str | None = None,
    options: dict[str, Any] | None = None,
    fmt: dict[str, Any] | str | None = None,
    retries: int = 3,
    timeout: float = 900.0,
) -> ChatResult:
    """Один вызов chat с повторами при сетевых ошибках. Ошибки не скрываются — пишутся в result.errors."""
    import ollama

    client = ollama.Client(host=host, timeout=timeout)
    result = ChatResult(model=model, started_at=now_iso())
    t0 = time.monotonic()
    for attempt in range(1, retries + 1):
        result.attempts = attempt
        try:
            kwargs: dict[str, Any] = {"model": model, "messages": messages, "options": options or {}}
            if think is not None:
                kwargs["think"] = think
            if fmt is not None:
                kwargs["format"] = fmt
            resp = client.chat(**kwargs)
            result.content = (resp.message.content or "").strip()
            result.thinking = resp.message.thinking or ""
            result.prompt_tokens += int(resp.prompt_eval_count or 0)
            result.output_tokens += int(resp.eval_count or 0)
            result.done_reason = resp.done_reason
            result.ollama_durations_s = {
                k: round((getattr(resp, k) or 0) / 1e9, 3)
                for k in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")
            }
            if result.content:
                break
            result.errors.append(f"попытка {attempt}: пустой ответ (done_reason={resp.done_reason})")
        except Exception as exc:  # сеть, 5xx, 410 (модель снята) — фиксируем и повторяем
            result.errors.append(f"попытка {attempt}: {type(exc).__name__}: {str(exc)[:300]}")
            status = getattr(exc, "status_code", None)
            if status in (400, 401, 403, 404, 410):
                break
        if attempt < retries:
            time.sleep(min(30, 5 * attempt))
    result.finished_at = now_iso()
    result.duration_s = round(time.monotonic() - t0, 2)
    return result


def extract_json(text: str) -> Any:
    """Достаёт JSON из ответа модели: снимает ```json-ограду и лишний текст вокруг."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("в ответе нет JSON")
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    end = text.rfind(closer)
    if end <= start:
        raise ValueError("JSON не закрыт")
    return json.loads(text[start : end + 1])


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
