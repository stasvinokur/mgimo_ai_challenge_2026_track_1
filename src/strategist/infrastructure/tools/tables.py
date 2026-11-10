"""Таблицы: паспорт (единицы, период, пропуски) и метрики двумя независимыми движками — pandas и DuckDB.

Числа по таблицам считает код, а не модель. Каждая метрика считается дважды: pandas (основной движок)
и SQL в DuckDB (проверочный). CSV DuckDB читает сам (read_csv_auto), XLSX — из зарегистрированного
DataFrame (чтение общее, но расчёт — отдельный SQL). Расхождение больше допуска → consistent=False,
и утверждение не получает статуса «подтверждено».
"""

from __future__ import annotations

import csv
import math
import re
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import yaml

from strategist.application.ports import MetricFilter, MetricResult, MetricSpec, TablePassport

TABLE_EXTENSIONS = (".csv", ".tsv", ".xlsx", ".xls")
META_SUFFIX = ".meta.yaml"
EMPTY_GROUP = "(пусто)"
PREVIEW_ROWS = 5
_DUCK_NUMERIC = re.compile(
    r"^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT|UHUGEINT|FLOAT|DOUBLE|REAL|DECIMAL.*)$"
)
_MONTH = re.compile(r"^\d{4}-\d{2}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T].*)?$")
_RU_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_TOKEN = re.compile(r"[\W_]+")
_PERIOD_HINTS = ("date", "month", "period", "cohort", "дата", "месяц", "период", "когорт", "week", "недел")


# --- Нормализация значений (одинаковая для обоих движков) -------------------------------------


def group_key(value: Any) -> str:
    """Строковый ключ значения: 1.0 → "1", полночь → дата, пропуск → «(пусто)». Одинаков для pandas и DuckDB."""
    if value is None or value is pd.NA:
        return EMPTY_GROUP
    if isinstance(value, float | np.floating) and math.isnan(float(value)):
        return EMPTY_GROUP
    if value is pd.NaT or (isinstance(value, pd.Timestamp) and pd.isna(value)):
        return EMPTY_GROUP
    if isinstance(value, bool | np.bool_):
        return str(bool(value))
    if isinstance(value, int | np.integer):
        return str(int(value))
    if isinstance(value, float | np.floating | Decimal):
        f = float(value)
        return str(int(f)) if f.is_integer() else repr(f)
    if isinstance(value, datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.datetime64):
        return group_key(pd.Timestamp(value))
    return str(value)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m", "%d.%m.%Y", "%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Значение «{value}» не распознано как дата")


def _parse_float(value: Any) -> float:
    if isinstance(value, bool | np.bool_):
        return float(bool(value))
    if isinstance(value, int | float | np.integer | np.floating | Decimal):
        return float(value)
    text = str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"Значение «{value}» не число") from exc


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool | np.bool_):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "1.0", "да", "yes", "истина"):
        return True
    if text in ("false", "0", "0.0", "нет", "no", "ложь"):
        return False
    raise ValueError(f"Значение «{value}» не логическое")


def _to_timestamp(value: Any) -> pd.Timestamp:
    return pd.Timestamp(_parse_datetime(value))


_OPS: dict[str, Callable[[Any, Any], Any]] = {
    "==": lambda s, t: s == t,
    "!=": lambda s, t: s != t,
    ">": lambda s, t: s > t,
    ">=": lambda s, t: s >= t,
    "<": lambda s, t: s < t,
    "<=": lambda s, t: s <= t,
}


def _close(a: float | None, b: float | None, tolerance: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tolerance * max(1.0, abs(a))


def _clean_float(value: Any) -> float | None:
    if value is None:
        return None
    f = float(value)
    return None if math.isnan(f) or math.isinf(f) else f


def column_letter(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA — адрес столбца как в Excel."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def row_ranges(rows: list[int], max_ranges: int = 6) -> str:
    """[2,3,4,7] → «2–4, 7»; слишком много разрывов → «2–145 (выборочно 57 строк)»."""
    if not rows:
        return "—"
    rows = sorted(rows)
    ranges: list[tuple[int, int]] = []
    start = prev = rows[0]
    for r in rows[1:]:
        if r == prev + 1:
            prev = r
            continue
        ranges.append((start, prev))
        start = prev = r
    ranges.append((start, prev))
    if len(ranges) > max_ranges:
        return f"{rows[0]}–{rows[-1]} (выборочно {len(rows)} строк)"
    return ", ".join(f"{a}–{b}" if a != b else str(a) for a, b in ranges)


# --- Загрузка -----------------------------------------------------------------------------------


@dataclass
class _Table:
    path: str
    kind: str  # csv | excel
    df: pd.DataFrame
    sheet: str | None = None
    delimiter: str = ","
    encoding: str = "utf-8"


def _detect_encoding(path: Path) -> str:
    sample = path.read_bytes()[: 1 << 20]
    try:
        sample.decode("utf-8")
        return "utf-8-sig" if sample.startswith(b"\xef\xbb\xbf") else "utf-8"
    except UnicodeDecodeError as exc:
        if exc.start > len(sample) - 4:  # обрезали посреди многобайтового символа
            return "utf-8"
        return "cp1251"


def _detect_delimiter(path: Path, encoding: str) -> str:
    if path.suffix.lower() == ".tsv":
        return "\t"
    with path.open(encoding=encoding, errors="replace", newline="") as fh:
        sample = fh.read(64 * 1024)
    lines = [ln for ln in sample.splitlines()[:50] if ln.strip()]
    if len(lines) > 1 and not sample.endswith(("\n", "\r")):
        lines = lines[:-1]  # последняя строка образца может быть обрезана
    # Разделитель, при котором все строки делятся на одинаковое число (>1) полей; при равенстве —
    # «;» и табуляция важнее запятой: в русских выгрузках запятая — десятичный знак.
    best, best_fields = ",", 1
    for delimiter in (";", "\t", ",", "|"):
        try:
            counts = {len(row) for row in csv.reader(lines, delimiter=delimiter)}
        except csv.Error:
            continue
        if len(counts) == 1 and (fields := counts.pop()) > best_fields:
            best, best_fields = delimiter, fields
    return best


def read_sidecar_meta(path: str) -> dict[str, Any]:
    sidecar = Path(path + META_SUFFIX)
    if not sidecar.exists():
        return {}
    try:
        data = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _guess_unit(name: str, series: pd.Series) -> str | None:
    lowered = name.lower()
    tokens = [t for t in _TOKEN.split(lowered) if t]
    if "₽" in lowered or any(t == "rub" or t.startswith("руб") for t in tokens):
        return "₽"
    if "%" in lowered:
        return "%"
    share_words = ("pct", "share", "retention", "rate", "ratio", "доля", "доли", "процент", "конверси")
    if any(t == w or (len(w) > 4 and t.startswith(w)) for t in tokens for w in share_words):
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        if len(numeric) and (numeric.abs() > 1).any():
            return "%"  # значения вида 41.5 — проценты, а не доля
        return "доля"
    if any(t == "users" or t.startswith("пользоват") for t in tokens):
        return "пользователи"
    return None


def _period_of(df: pd.DataFrame) -> str | None:
    candidates: list[tuple[int, str, str]] = []
    for pos, col in enumerate(df.columns):
        series = df[col].dropna()
        if series.empty:
            continue
        hinted = any(h in col.lower() for h in _PERIOD_HINTS)
        lo = hi = None
        if pd.api.types.is_datetime64_any_dtype(series):
            lo, hi = group_key(series.min()), group_key(series.max())
        elif pd.api.types.is_numeric_dtype(series) and col.lower() in ("year", "год"):
            if series.between(1900, 2100).all():
                lo, hi = group_key(series.min()), group_key(series.max())
        elif not pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
            values = series.map(lambda v: str(v).strip())
            for pattern, to_iso in (
                (_MONTH, lambda s: s),
                (_ISO_DATE, lambda s: s[:10]),
                (_RU_DATE, lambda s: f"{s[6:10]}-{s[3:5]}-{s[0:2]}"),
            ):
                matched = values[values.str.match(pattern)]
                if len(matched) >= 0.8 * len(values):
                    iso = matched.map(to_iso)
                    lo, hi = iso.min(), iso.max()
                    break
        if lo is not None and hi is not None:
            candidates.append((0 if hinted else 1, f"{pos:04d}", f"{lo}–{hi}" if lo != hi else str(lo)))
    if not candidates:
        return None
    return sorted(candidates)[0][2]


def _cell_text(value: Any) -> str:
    key = group_key(value)
    text = "" if key == EMPTY_GROUP else key
    return text.replace("|", "\\|").replace("\n", " ")


def markdown_preview(df: pd.DataFrame, rows: int = PREVIEW_ROWS) -> str:
    if df.columns.empty:
        return ""
    header = "| " + " | ".join(_cell_text(c) for c in df.columns) + " |"
    sep = "| " + " | ".join("---" for _ in df.columns) + " |"
    body = [
        "| " + " | ".join(_cell_text(v) for v in row) + " |" for row in df.head(rows).itertuples(index=False)
    ]
    return "\n".join([header, sep, *body])


# --- Движок -------------------------------------------------------------------------------------


class PandasDuckdbTableEngine:
    """TableEngine: pandas — основной расчёт, DuckDB — независимый проверочный SQL."""

    def __init__(self, tolerance: float = 1e-6, cache_size: int = 8) -> None:
        self.tolerance = tolerance
        self.cache_size = cache_size
        self._cache: OrderedDict[tuple[str, str | None, int, int], _Table] = OrderedDict()
        self._lock = threading.Lock()

    def supports(self, path: str) -> bool:
        return str(path).lower().endswith(TABLE_EXTENSIONS)

    # загрузка ---------------------------------------------------------------------------------

    def _load(self, path: str, sheet: str | None) -> _Table:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(path)
        stat = p.stat()
        key = (str(p.resolve()), sheet, stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        table = self._read(p, sheet)
        with self._lock:
            self._cache[key] = table
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return table

    def _read(self, p: Path, sheet: str | None) -> _Table:
        ext = p.suffix.lower()
        if ext in (".csv", ".tsv"):
            encoding = _detect_encoding(p)
            delimiter = _detect_delimiter(p, encoding)
            df = pd.read_csv(p, sep=delimiter, encoding=encoding)
            df.columns = [str(c) for c in df.columns]
            return _Table(path=str(p), kind="csv", df=df, delimiter=delimiter, encoding=encoding)
        if ext in (".xlsx", ".xls"):
            try:
                xf = pd.ExcelFile(p)
            except ImportError as exc:
                raise ValueError(
                    f"{p.name}: для формата {ext} нет движка чтения — сохраните как .xlsx"
                ) from exc
            with xf:
                names = [str(n) for n in xf.sheet_names]
                name = sheet if sheet is not None else names[0]
                if name not in names:
                    raise ValueError(f"{p.name}: нет листа «{name}». Листы: {', '.join(names)}")
                df = pd.read_excel(xf, sheet_name=name)
            df.columns = [str(c) for c in df.columns]
            return _Table(path=str(p), kind="excel", df=df, sheet=name)
        raise ValueError(f"{p.name}: формат {ext} не поддерживается (нужны {', '.join(TABLE_EXTENSIONS)})")

    @staticmethod
    def _column(table: _Table, name: str | None, role: str) -> str:
        if name is None:
            raise ValueError(f"Для метрики нужен столбец ({role})")
        columns = list(table.df.columns)
        if name in columns:
            return name
        relaxed = [c for c in columns if c.strip().lower() == str(name).strip().lower()]
        if len(relaxed) == 1:
            return relaxed[0]
        raise ValueError(f"Нет столбца «{name}» ({role}). Доступные столбцы: {', '.join(columns)}")

    def _resolve(self, table: _Table, spec: MetricSpec) -> MetricSpec:
        """Имена столбцов сверяются до расчёта: опечатка → понятная ошибка с перечнем столбцов."""
        update: dict[str, Any] = {}
        if spec.agg == "share":
            update["column"] = self._column(table, spec.column, "column") if spec.column else spec.column
        elif spec.agg == "count" and spec.column is None:
            pass
        else:
            update["column"] = self._column(table, spec.column, "column")
        if spec.agg == "ratio":
            update["denominator_column"] = self._column(table, spec.denominator_column, "denominator_column")
        if spec.group_by:
            update["group_by"] = self._column(table, spec.group_by, "group_by")
        update["filters"] = [
            f.model_copy(update={"column": self._column(table, f.column, "filters")}) for f in spec.filters
        ]
        return spec.model_copy(update=update)

    # паспорт ----------------------------------------------------------------------------------

    def passport(self, path: str, sheet: str | None = None) -> TablePassport:
        table = self._load(path, sheet)
        df = table.df
        meta = read_sidecar_meta(path)
        units: dict[str, str] = {}
        meta_units = meta.get("units") if isinstance(meta.get("units"), dict) else {}
        for col in df.columns:
            if col in meta_units and meta_units[col] not in (None, ""):
                units[col] = str(meta_units[col])
                continue
            guessed = _guess_unit(col, df[col])
            if guessed:
                units[col] = guessed
        rows = len(df)
        return TablePassport(
            path=str(path),
            sheet=table.sheet,
            rows=rows,
            columns=list(df.columns),
            dtypes={c: str(df[c].dtype) for c in df.columns},
            units=units,
            period=str(meta["period"]) if meta.get("period") else _period_of(df),
            missing_share={c: round(float(df[c].isna().mean()), 4) if rows else 0.0 for c in df.columns},
            preview=markdown_preview(df),
        )

    def distinct_values(self, path: str, column: str, sheet: str | None = None, limit: int = 20) -> list[str]:
        table = self._load(path, sheet)
        col = self._column(table, column, "distinct_values")
        values = list(table.df[col].dropna().unique())
        try:
            values.sort()
        except TypeError:
            values.sort(key=group_key)
        out: list[str] = []
        for v in values:
            key = group_key(v)
            if key not in out:
                out.append(key)
        return out[:limit]

    # расчёт -----------------------------------------------------------------------------------

    def compute(self, path: str, spec: MetricSpec, sheet: str | None = None) -> MetricResult:
        if spec.agg == "sum_share":
            return self._sum_share(path, spec, sheet)
        table = self._load(path, sheet)
        resolved = self._resolve(table, spec)
        value, by_group, used_rows = self._pandas(table.df, resolved)
        try:
            check_value, check_by_group = self._duckdb(table, resolved)
        except duckdb.Error:
            check_value, check_by_group = None, {}
            consistent = False
        else:
            consistent = _close(value, check_value, self.tolerance) and set(by_group) == set(check_by_group)
            consistent = consistent and all(
                _close(by_group[k], check_by_group[k], self.tolerance) for k in by_group
            )
        return MetricResult(
            spec=spec,
            value=value,
            by_group=by_group,
            check_value=check_value,
            check_by_group=check_by_group,
            primary_engine="pandas",
            check_engine="duckdb",
            consistent=consistent,
            rows_used=len(used_rows),
            cells=self._cells(table, resolved, used_rows),
        )

    def _sum_share(self, path: str, spec: MetricSpec, sheet: str | None) -> MetricResult:
        """Доля суммы столбца по группам (например, доля подписчиков по каналам). Каждый движок делит на свою
        сумму — расчёты остаются независимыми."""
        base = self.compute(path, spec.model_copy(update={"agg": "sum"}), sheet)

        def norm(values: dict[str, float], total: float | None) -> dict[str, float]:
            return {k: (v / total) for k, v in values.items()} if total else {}

        by_group = norm(base.by_group, base.value)
        check_by_group = norm(base.check_by_group, base.check_value)
        consistent = base.consistent and all(
            _close(by_group[k], check_by_group.get(k), self.tolerance) for k in by_group
        )
        return base.model_copy(
            update={
                "spec": spec,
                "value": 1.0 if base.value else None,
                "check_value": 1.0 if base.check_value else None,
                "by_group": by_group,
                "check_by_group": check_by_group,
                "consistent": consistent,
            }
        )

    # pandas -----------------------------------------------------------------------------------

    @staticmethod
    def _numeric(series: pd.Series, name: str) -> pd.Series:
        if pd.api.types.is_bool_dtype(series):
            return series.astype(float)
        if pd.api.types.is_numeric_dtype(series):
            return series.astype(float)
        cleaned = series.map(
            lambda v: v if pd.isna(v) else str(v).replace("\u00a0", "").replace(" ", "").replace(",", ".")
        )
        numeric = pd.to_numeric(cleaned, errors="coerce").astype(float)
        if series.notna().any() and numeric.notna().sum() == 0:
            raise ValueError(f"Столбец «{name}» не числовой — для этой агрегации нужны числа")
        return numeric

    @staticmethod
    def _pandas_mask(df: pd.DataFrame, filters: list[MetricFilter]) -> pd.Series:
        mask = pd.Series(True, index=df.index)
        for f in filters:
            series = df[f.column]
            notna = series.notna()
            present = series[notna]  # как в SQL: сравнение с пропуском строку не пропускает
            convert: Callable[[Any], Any]
            if pd.api.types.is_bool_dtype(series):
                convert, values = _parse_bool, present
            elif pd.api.types.is_numeric_dtype(series):
                convert, values = _parse_float, present.astype(float)
            elif pd.api.types.is_datetime64_any_dtype(series):
                convert, values = _to_timestamp, present
            else:
                convert, values = group_key, present.map(group_key).astype(object)
            try:
                if f.op == "in":
                    raw = f.value if isinstance(f.value, list | tuple | set) else [f.value]
                    cond = values.isin([convert(v) for v in raw])
                else:
                    cond = _OPS[f.op](values, convert(f.value))
            except ValueError as exc:
                raise ValueError(f"Фильтр по столбцу «{f.column}»: {exc}") from exc
            mask &= cond.astype(bool).reindex(df.index, fill_value=False)
        return mask

    def _agg_pandas(self, frame: pd.DataFrame, spec: MetricSpec) -> tuple[float | None, list[int]]:
        """Значение агрегата и позиции строк, вошедших в расчёт."""
        positions = list(frame.index)
        if spec.agg == "count" and spec.column is None:
            return float(len(frame)), positions
        if spec.agg == "count":
            used = frame[spec.column].notna()
            return float(used.sum()), [p for p, u in zip(positions, used, strict=True) if u]
        if spec.agg == "ratio":
            # Только строки, где есть и числитель, и знаменатель: иначе цензурированные когорты (месяц ещё
            # не наступил, ячейка пустая) занижают отношение — знаменатель есть, числителя ещё нет.
            num_s = self._numeric(frame[spec.column], spec.column)  # type: ignore[index, arg-type]
            den_s = self._numeric(frame[spec.denominator_column], spec.denominator_column)  # type: ignore[index, arg-type]
            both = num_s.notna() & den_s.notna()
            num, den = num_s[both].sum(), den_s[both].sum()
            used_ratio = [p for p, ok in zip(positions, both, strict=True) if ok]
            return (None if den == 0 else _clean_float(num / den)), used_ratio
        values = self._numeric(frame[spec.column], spec.column)  # type: ignore[index, arg-type]
        valid = values.dropna()
        used_positions = list(valid.index)
        if spec.agg == "sum":
            return float(valid.sum()), used_positions
        if valid.empty:
            return None, used_positions
        fn = {"mean": valid.mean, "median": valid.median, "min": valid.min, "max": valid.max}[spec.agg]
        return _clean_float(fn()), used_positions

    @staticmethod
    def _sorted_groups(series: pd.Series) -> list[Any]:
        keys = list(series.drop_duplicates())
        present = [k for k in keys if not pd.isna(k)]
        missing = [k for k in keys if pd.isna(k)][:1]
        try:
            present.sort()
        except TypeError:
            present.sort(key=group_key)
        return present + missing

    def _pandas(self, df: pd.DataFrame, spec: MetricSpec) -> tuple[float | None, dict[str, float], list[int]]:
        df = df.reset_index(drop=True)
        mask = self._pandas_mask(df, spec.filters)
        filtered = df[mask]
        if spec.agg == "share":
            total = len(df)
            value = float(mask.sum()) / total if total else None
            used = list(filtered.index)
            by_group: dict[str, float] = {}
            if spec.group_by:
                keys = df[spec.group_by]
                for key in self._sorted_groups(keys):
                    in_group = keys.isna() if pd.isna(key) else keys == key
                    size = int(in_group.sum())
                    if not size:
                        continue
                    if spec.filters:  # доля строк, прошедших фильтры, внутри группы (rate)
                        by_group[group_key(key)] = float((mask & in_group).sum()) / size
                    else:  # без фильтров — распределение: доля группы от всех строк
                        by_group[group_key(key)] = size / total
            return value, by_group, used
        value, used = self._agg_pandas(filtered, spec)
        by_group = {}
        if spec.group_by:
            keys = filtered[spec.group_by]
            for key in self._sorted_groups(keys):
                part = filtered[keys.isna()] if pd.isna(key) else filtered[keys == key]
                group_value, _ = self._agg_pandas(part, spec)
                if group_value is not None:
                    by_group[group_key(key)] = group_value
        return value, by_group, used

    # DuckDB -----------------------------------------------------------------------------------

    @staticmethod
    def _q(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _connect(self, table: _Table, tmp_dir: str) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(":memory:")
        if table.kind == "csv":
            source = table.path
            if table.encoding not in ("utf-8", "utf-8-sig"):
                # DuckDB читает только UTF-8: перекодированная копия, разбор — всё равно свой
                source = str(Path(tmp_dir) / "table.csv")
                text = Path(table.path).read_text(encoding=table.encoding)
                Path(source).write_text(text, encoding="utf-8")
            con.execute(
                "CREATE TABLE t AS SELECT * FROM read_csv_auto(?, delim = ?, header = true, sample_size = -1)",
                [source, table.delimiter],
            )
        else:
            con.register("t_source", table.df)
            con.execute("CREATE TABLE t AS SELECT * FROM t_source")
            con.unregister("t_source")
        return con

    def _sql_numeric(self, column: str, types: dict[str, str]) -> str:
        col = self._q(column)
        kind = types.get(column, "VARCHAR").upper()
        if _DUCK_NUMERIC.match(kind):
            return f"CAST({col} AS DOUBLE)"
        if kind == "BOOLEAN":
            return f"CAST(CAST({col} AS INTEGER) AS DOUBLE)"
        return (
            f"TRY_CAST(REPLACE(REPLACE(REPLACE(CAST({col} AS VARCHAR), chr(160), ''), ' ', ''), ',', '.') "
            "AS DOUBLE)"
        )

    def _sql_filters(self, filters: list[MetricFilter], types: dict[str, str]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for f in filters:
            kind = types.get(f.column, "VARCHAR").upper()
            col = self._q(f.column)
            if _DUCK_NUMERIC.match(kind):
                convert: Callable[[Any], Any] = _parse_float
                expr = f"CAST({col} AS DOUBLE)"
            elif kind == "BOOLEAN":
                convert, expr = _parse_bool, col
            elif kind == "DATE":
                convert, expr = (lambda v: _parse_datetime(v).date()), col
            elif kind.startswith("TIMESTAMP"):
                convert, expr = _parse_datetime, f"CAST({col} AS TIMESTAMP)"
            else:
                convert, expr = group_key, f"CAST({col} AS VARCHAR)"
            raw = f.value if isinstance(f.value, list | tuple | set) else [f.value]
            if f.op == "in":
                values = [convert(v) for v in raw]
                if not values:
                    clauses.append("FALSE")
                    continue
                clauses.append(f"{expr} IN ({', '.join('?' for _ in values)})")
                params.extend(values)
            else:
                clauses.append(f"{expr} {'=' if f.op == '==' else f.op} ?")
                params.append(convert(f.value))
        return (" AND ".join(clauses) if clauses else "TRUE"), params

    def _sql_agg(self, spec: MetricSpec, types: dict[str, str]) -> str:
        if spec.agg == "count":
            return f"COUNT({self._q(spec.column)})" if spec.column else "COUNT(*)"
        x = self._sql_numeric(spec.column or "", types)
        if spec.agg == "ratio":
            d = self._sql_numeric(spec.denominator_column or "", types)
            both = f"{x} IS NOT NULL AND {d} IS NOT NULL"
            return (
                f"COALESCE(SUM({x}) FILTER (WHERE {both}), 0) / "
                f"NULLIF(COALESCE(SUM({d}) FILTER (WHERE {both}), 0), 0)"
            )
        return {
            "mean": f"AVG({x})",
            "sum": f"COALESCE(SUM({x}), 0)",
            "median": f"MEDIAN({x})",
            "min": f"MIN({x})",
            "max": f"MAX({x})",
        }[spec.agg]

    def _duckdb(self, table: _Table, spec: MetricSpec) -> tuple[float | None, dict[str, float]]:
        with tempfile.TemporaryDirectory(prefix="strategist-duck-") as tmp_dir:
            con = self._connect(table, tmp_dir)
            try:
                types = {row[0]: str(row[1]) for row in con.execute("DESCRIBE t").fetchall()}
                where, params = self._sql_filters(spec.filters, types)
                if spec.agg == "share":
                    expr = f"CAST(COUNT(*) FILTER (WHERE {where}) AS DOUBLE) / NULLIF(COUNT(*), 0)"
                    total_sql, group_where = f"SELECT {expr} FROM t", ""
                else:
                    expr = self._sql_agg(spec, types)
                    total_sql, group_where = f"SELECT {expr} FROM t WHERE {where}", f" WHERE {where}"
                row = con.execute(total_sql, params).fetchone()
                value = _clean_float(row[0]) if row else None
                by_group: dict[str, float] = {}
                if spec.group_by:
                    g = self._q(spec.group_by)
                    group_expr = expr
                    if spec.agg == "share" and not spec.filters:
                        group_expr = "CAST(COUNT(*) AS DOUBLE) / (SELECT COUNT(*) FROM t)"
                    sql = f"SELECT {g}, {group_expr} FROM t{group_where} GROUP BY {g}"
                    for key, group_value in con.execute(sql, params).fetchall():
                        cleaned = _clean_float(group_value)
                        if cleaned is not None:
                            by_group[group_key(key)] = cleaned
                return value, by_group
            finally:
                con.close()

    # адрес ячеек ------------------------------------------------------------------------------

    def _cells(self, table: _Table, spec: MetricSpec, used_rows: list[int]) -> list[str]:
        columns = list(table.df.columns)
        sheet = f"лист «{table.sheet}», " if table.sheet else ""
        rows = row_ranges([p + 2 for p in used_rows])  # строка 1 — заголовок
        cells: list[str] = []

        def ref(col: str) -> str:
            return f"{col} ({column_letter(columns.index(col))})"

        targets = [] if spec.agg == "share" else [c for c in (spec.column, spec.denominator_column) if c]
        if not targets:
            cells.append(f"{sheet}строки {rows} из 2–{len(table.df) + 1}")
        for col in targets:
            cells.append(f"{sheet}столбец {ref(col)}, строки {rows}")
        for f in spec.filters:
            cells.append(f"фильтр: {ref(f.column)} {f.op} {f.value!r}")
        if spec.group_by:
            cells.append(f"группировка: {ref(spec.group_by)}")
        return cells
