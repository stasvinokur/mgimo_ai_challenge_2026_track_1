"""IngestMaterial: дословные цитаты из документов и расчёты по таблицам.

Читатель работает на карантине: текст материала — недоверенные данные (spotlighting в адаптере LLM),
модель возвращает только схему цитат, а найденные в тексте команды — в instructions_detected.
Цитата, которой нет в исходнике дословно, отбрасывается: утверждения пишутся из цитат, а не наоборот.
Числа по таблицам считает код: pandas и DuckDB независимо, с проверкой парадокса Симпсона.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from strategist.application.context import AppContext
from strategist.application.dto.llm_outputs import EvidenceExtractionDTO, MetricPlanDTO, MetricProposalDTO
from strategist.application.llm_calls import ask_structured
from strategist.application.ports import (
    Fragment,
    MetricFilter,
    MetricResult,
    MetricSpec,
    TablePassport,
    UntrustedBlock,
)
from strategist.application.quality import enabled
from strategist.domain.entities import (
    Claim,
    ClaimType,
    Computation,
    DecisionCase,
    Evidence,
    Origin,
    Reliability,
    Source,
    SourceKind,
    SupportLabel,
    Unknown,
)
from strategist.domain.services.metric_contract import (
    ContractIssue,
    IssueKind,
    churn_label_on_retention,
    contract_issues,
    is_aggregated_table,
    looks_like_id,
    period_of,
)
from strategist.domain.services.simpson import is_rate_column, mean_of_rates_warning, simpson_reversal

CHUNK_CHARS = 7000
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text.replace("«", '"').replace("»", '"').replace("ё", "е")).strip().lower()


CONTEXT_QUESTION = (
    "[КОНТЕКСТ] Ситуация в целом: рынок и конкуренты, поведение и мнения пользователей, условия продукта, "
    "партнёров и цены, позиции команд — всё, что поможет понять ситуацию и сравнить варианты"
)


def research_questions_text(case: DecisionCase, *, with_context: bool = False) -> str:
    plan = case.research_plan
    if plan is None or not plan.questions:
        base = "Общая картина ситуации по запросу руководителя."
    else:
        base = "\n".join(f"[{q.id}] {q.question}" for q in plan.questions if q.status.value != "skipped")
    return f"{base}\n{CONTEXT_QUESTION}" if with_context else base


def _chunks(fragments: list[Fragment]) -> list[list[tuple[int, Fragment]]]:
    chunks: list[list[tuple[int, Fragment]]] = []
    current: list[tuple[int, Fragment]] = []
    size = 0
    for idx, frag in enumerate(fragments, start=1):
        if current and size + len(frag.text) > CHUNK_CHARS:
            chunks.append(current)
            current, size = [], 0
        current.append((idx, frag))
        size += len(frag.text) + 8
    if current:
        chunks.append(current)
    return chunks


def _screen(ctx: AppContext, case_id: str, source: Source, fragments: list[Fragment]) -> list[Fragment]:
    if ctx.guard is None:
        return fragments
    screened = []
    for frag in fragments:
        result = ctx.guard.screen(frag.text)
        if result.pii_found:
            source.pii_masked += result.pii_found
        for hit in result.injections:
            if hit not in source.instructions_detected:
                source.instructions_detected.append(hit)
        screened.append(Fragment(text=result.text, locator=frag.locator))
    if source.pii_masked:
        ctx.emit("pii.masked", case_id, source_id=source.id, count=source.pii_masked)
    return screened


def _match_fragment(quote: str, fragment_no: int, chunk: list[tuple[int, Fragment]]) -> Fragment | None:
    wanted = normalize(quote)
    if len(wanted) < 12:
        return None
    by_no = dict(chunk)
    if fragment_no in by_no and wanted in normalize(by_no[fragment_no].text):
        return by_no[fragment_no]
    for _, frag in chunk:  # модель ошиблась номером фрагмента — ищем в соседних
        if wanted in normalize(frag.text):
            return frag
    return None


def ingest_document(ctx: AppContext, case: DecisionCase, source: Source) -> list[Evidence]:
    path = ctx.materials.absolute_path(case.id, source.path or "")
    parser = ctx.parser_for(path)
    if parser is None:
        ctx.emit("ingest.unsupported", case.id, source_id=source.id, path=source.path)
        return []
    parsed = parser.parse(path)
    fragments = _screen(ctx, case.id, source, parsed.fragments)
    added: list[Evidence] = []
    reserved: list[str] = []
    rejected = 0
    questions = research_questions_text(case, with_context=True)
    for chunk in _chunks(fragments):
        text = "\n".join(f"[F{idx}] {frag.text}" for idx, frag in chunk)
        dto, _ = ask_structured(
            ctx,
            task="extract_evidence",
            prompt="extract_evidence",
            values={"questions": questions, "source_title": source.title, "synthetic": source.synthetic},
            schema=EvidenceExtractionDTO,
            case_id=case.id,
            step=f"collect:{source.id}",
            untrusted=[UntrustedBlock(source_id=source.id, text=text, label=source.title)],
        )
        for hit in dto.instructions_detected:
            hit = hit.strip()
            if hit and hit not in source.instructions_detected:
                source.instructions_detected.append(hit[:300])
        for q in dto.quotes:
            frag = _match_fragment(q.quote, q.fragment, chunk)
            if frag is None:
                rejected += 1
                continue
            if ctx.guard is not None and ctx.guard.screen(q.quote).injections:
                # Цитата с командой не становится доказательством: иначе инструкция из источника
                # доехала бы до следующих промптов уже вне маркированного блока данных.
                if q.quote.strip()[:300] not in source.instructions_detected:
                    source.instructions_detected.append(q.quote.strip()[:300])
                rejected += 1
                continue
            if any(normalize(e.quote) == normalize(q.quote) for e in added):
                continue
            ev = Evidence(
                id=case.new_id("evidence", reserved=reserved),
                source_id=source.id,
                quote=q.quote.strip(),
                locator=frag.locator,
                extracted_at=ctx.clock.now(),
                origin=Origin.SOURCE_DOCUMENT,
                topic=q.topic or None,
            )
            reserved.append(ev.id)
            added.append(ev)
    if source.instructions_detected:
        ctx.emit("injection.detected", case.id, source_id=source.id, items=source.instructions_detected[:5])
    ctx.emit("ingest.document", case.id, source_id=source.id, quotes=len(added), rejected_quotes=rejected)
    return added


# --- Таблицы ---------------------------------------------------------------------


def passport_text(p: TablePassport) -> str:
    lines = [f"Строк: {p.rows}; столбцы: {', '.join(p.columns)}"]
    if p.dtypes:
        lines.append("Типы: " + ", ".join(f"{k}={v}" for k, v in p.dtypes.items()))
    if p.units:
        lines.append("Единицы: " + ", ".join(f"{k}={v}" for k, v in p.units.items()))
    if p.period:
        lines.append(f"Период: {p.period}")
    if p.missing_share:
        gaps = {k: v for k, v in p.missing_share.items() if v > 0}
        if gaps:
            lines.append("Доля пропусков: " + ", ".join(f"{k}={v:.0%}" for k, v in gaps.items()))
    if p.preview:
        lines.append("Первые строки:\n" + p.preview)
    lines.append(
        "Агрегаты: sum_share с group_by — доля суммы столбца по группам (например, доля подписчиков по каналам); "
        "share без фильтров и с group_by — распределение СТРОК таблицы по группам, а не пользователей; "
        "доля из счётчиков — ratio (sum числителя / sum знаменателя); не берите mean по столбцу процентов, если "
        "в группу попадает несколько строк разного размера — фильтром или group_by добейтесь одной строки в группе; "
        "share с фильтрами — доля строк, прошедших фильтры (внутри группы при group_by); "
        "ratio — sum(column) / sum(denominator_column), только существующие столбцы; "
        "count — число строк. Не дублируй один и тот же расчёт под разными именами."
    )
    return "\n".join(lines)


def spec_from_proposal(m: MetricProposalDTO) -> MetricSpec:
    filters = [
        MetricFilter(
            column=f.column,
            op=f.op if f.op in ("==", "!=", ">", ">=", "<", "<=", "in") else "==",
            value=f.value,
        )
        for f in m.filters
    ]
    known = ("mean", "sum", "count", "median", "min", "max", "ratio", "share", "sum_share")
    agg = m.agg if m.agg in known else "mean"
    return MetricSpec(
        name=m.name,
        agg=agg,
        column=m.column,
        denominator_column=m.denominator_column,
        filters=filters,
        group_by=m.group_by,
        unit=m.unit,
        description=m.description,
    )


def _fmt(value: float | None, unit: str | None) -> str:
    if value is None:
        return "—"
    if unit in ("доля", "share", "ratio") or (unit is None and 0 <= value <= 1 and value != int(value)):
        return f"{value:.1%}"
    return f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".") + (f" {unit}" if unit else "")


def formula(spec: MetricSpec) -> str:
    """Человекочитаемая формула расчёта: подпись модели может ошибаться, формула — нет."""
    col = spec.column or "строки"
    text = {
        "mean": f"среднее {col}",
        "sum": f"сумма {col}",
        "count": f"число строк{'' if not spec.column else ' с ' + spec.column}",
        "median": f"медиана {col}",
        "min": f"минимум {col}",
        "max": f"максимум {col}",
        "ratio": f"сумма {col} / сумма {spec.denominator_column} (строки, где есть оба значения)",
        "share": "доля строк" + (" с условием" if spec.filters else ""),
        "sum_share": f"доля суммы {col}",
    }[spec.agg]
    if spec.filters:
        text += "; фильтр: " + ", ".join(f"{f.column} {f.op} {f.value}" for f in spec.filters)
    if spec.group_by:
        text += f"; по «{spec.group_by}»"
    return text


def metric_claim(
    ctx: AppContext,
    case: DecisionCase,
    source: Source,
    result: MetricResult,
    reserved: list[str],
    note: str = "",
    *,
    period: str | None = None,
    rows: int | None = None,
    contract: list[str] | None = None,
) -> Claim:
    spec = result.spec
    if result.by_group:
        breakdown = "; ".join(f"{k}: {_fmt(v, spec.unit)}" for k, v in list(result.by_group.items())[:12])
        text = f"{spec.description or spec.name} [расчёт: {formula(spec)}]: {breakdown}"
        value = result.value if result.value is not None else float(len(result.by_group))
        check = (
            result.check_value if result.check_value is not None else (value if result.consistent else None)
        )
    else:
        text = f"{spec.description or spec.name} [расчёт: {formula(spec)}]: {_fmt(result.value, spec.unit)}"
        value = result.value if result.value is not None else 0.0
        check = result.check_value
    claim = Claim(
        id=case.new_id("claim", reserved=reserved),
        text=text + (f". {note}" if note else ""),
        # Таблица модельных респондентов: расчёт верен, но это синтетика — гипотеза, а не число о спросе
        type=ClaimType.SYNTHETIC if source.is_model_respondents else ClaimType.DERIVED_METRIC,
        computation=Computation(
            metric=spec.name,
            description=spec.description,
            spec=spec.model_dump(),
            value=value,
            unit=spec.unit,
            primary_engine=result.primary_engine,
            check_engine=result.check_engine,
            check_value=check if result.consistent or check is not None else None,
            source_id=source.id,
            cells=result.cells,
            period=period,
            rows=rows,
            contract=contract or [],
        ),
        support=SupportLabel.SUPPORTED
        if result.consistent and not source.is_model_respondents
        else SupportLabel.UNVERIFIED,
        verified_by="code: pandas+duckdb" if result.consistent else "code: расхождение движков",
        confidence=0.95 if result.consistent else 0.2,
        origin=Origin.SOURCE_DOCUMENT if not source.synthetic else Origin.EXTERNAL_DATA,
        reliability=(
            Reliability.VERY_LOW
            if source.is_model_respondents
            else (Reliability.LOW if source.synthetic else Reliability.HIGH)
        ),
        topic=spec.name,
        created_at=ctx.clock.now(),
    )
    reserved.append(claim.id)
    return claim


def _degenerate(result: MetricResult) -> bool:
    """Все группы с одинаковым значением 0/1 — почти всегда ошибка в спецификации, а не находка."""
    values = [round(v, 6) for v in result.by_group.values()]
    return len(values) > 2 and len(set(values)) == 1 and values[0] in (0.0, 1.0)


def auto_segment(ctx: AppContext, path: str, spec: MetricSpec) -> str | None:
    """Главный сегмент таблицы, если модель его не назвала: текстовый столбец с 2–6 значениями, кроме group_by
    и столбцов расчёта. Сравнение групп без разбивки по сегменту — частая причина неверного вывода."""
    try:
        passport = ctx.tables.passport(path)
    except Exception:
        return None
    used = {spec.group_by, spec.column, spec.denominator_column, *(f.column for f in spec.filters)}
    for column in passport.columns:
        dtype = passport.dtypes.get(column, "object")
        if column in used or not (
            dtype.startswith("object") or dtype.startswith("str") or dtype == "category"
        ):
            continue
        try:
            values = ctx.tables.distinct_values(path, column, limit=8)
        except Exception:
            continue
        if 2 <= len(values) <= 6:
            return column
    return None


def _sentence(note: str, addition: str) -> str:
    """Дописать предложение к примечанию без двойной точки («…состава).. Исправлено»)."""
    if not note:
        return addition
    return note.rstrip() + (" " if note.rstrip().endswith((".", "!", "?", "…")) else ". ") + addition


def _aggregated(ctx: AppContext, path: str, passport: TablePassport) -> bool:
    """Строка таблицы — группа, а не человек: проверка контракта для «доли строк» (см. is_aggregated_table)."""
    ids: set[str] = set()
    for column in passport.columns:
        dtype = passport.dtypes.get(column, "object")
        if not (dtype.startswith("object") or dtype.startswith("str") or dtype == "category"):
            continue
        try:
            values = ctx.tables.distinct_values(path, column, limit=passport.rows + 1)
        except Exception:
            continue
        if len(values) >= passport.rows and looks_like_id(values):
            ids.add(column)
    return is_aggregated_table(passport.columns, passport.dtypes, ids)


def _apply_contract(
    ctx: AppContext, path: str, spec: MetricSpec, result: MetricResult
) -> tuple[MetricSpec, MetricResult, list[ContractIssue], TablePassport | None]:
    """Контракт расчёта: исправить подпись или единицу (значение не меняется), отметить долю строк агрегата."""
    try:
        passport: TablePassport | None = ctx.tables.passport(path)
    except Exception:
        passport = None
    aggregated = spec.agg == "share" and passport is not None and _aggregated(ctx, path, passport)
    issues = contract_issues(
        label=f"{spec.name} {spec.description}",
        agg=spec.agg,
        column=spec.column,
        denominator_column=spec.denominator_column,
        unit=spec.unit,
        values=[v for v in (result.value, *result.by_group.values()) if v is not None],
        aggregated_table=aggregated,
    )
    update: dict[str, str] = {}
    for issue in issues:
        if issue.unit:
            update["unit"] = issue.unit
        if issue.label:
            update["description"] = issue.label
    if update:
        spec = spec.model_copy(update=update)
        result = result.model_copy(update={"spec": spec})
    return spec, result, issues, passport


def compute_metric(
    ctx: AppContext,
    case: DecisionCase,
    source: Source,
    proposal: MetricProposalDTO,
    reserved: list[str],
    errors: list[str] | None = None,
) -> Claim | None:
    path = ctx.materials.absolute_path(case.id, source.path or "")
    spec = spec_from_proposal(proposal)
    if spec.agg in ("ratio", "sum_share") and spec.column and spec.column == spec.denominator_column:
        # sum(x) / sum(x) = 100% в каждой группе — ошибка спецификации, а не находка
        ctx.emit("metric.degenerate", case.id, source_id=source.id, metric=spec.name)
        if errors is not None:
            errors.append(
                f"{spec.name}: отношение столбца «{spec.column}» к самому себе — выбери другой знаменатель"
            )
        return None
    try:
        result = ctx.tables.compute(path, spec)
    except Exception as exc:  # неверный столбец, тип и т. п. — честно фиксируем как пробел
        ctx.emit("metric.failed", case.id, source_id=source.id, metric=spec.name, error=str(exc)[:200])
        if errors is not None:
            errors.append(f"{spec.name}: {str(exc)[:240]}")
        return None
    if _degenerate(result):
        ctx.emit("metric.degenerate", case.id, source_id=source.id, metric=spec.name)
        if errors is not None:
            errors.append(
                f"{spec.name}: вырожденный результат (во всех группах одно значение) — проверь агрегат"
            )
        return None
    issues: list[ContractIssue] = []
    passport: TablePassport | None = None
    original_label = spec.description or spec.name
    if enabled(ctx.settings, "metric_contract"):
        spec, result, issues, passport = _apply_contract(ctx, path, spec, result)
        for issue in issues:
            ctx.emit("metric.contract", case.id, source_id=source.id, metric=spec.name, kind=issue.kind.value)
    kinds = {i.kind for i in issues}
    note = ""
    named = proposal.segment_column if proposal.segment_column != spec.group_by else None  # сегмент ≠ группа
    segment = named or (auto_segment(ctx, path, spec) if spec.group_by else None)
    try:
        seg_values = ctx.tables.distinct_values(path, segment, limit=8) if spec.group_by and segment else []
    except Exception:  # модель назвала несуществующий столбец сегмента — метрику считаем без разбивки
        seg_values = []
        if errors is not None:
            errors.append(f"{spec.name}: нет столбца сегмента «{segment}» — разбивка пропущена")
    if seg_values and result.by_group:
        by_segment: dict[str, dict[str, float]] = {}
        for seg_value in seg_values:
            seg_spec = spec.model_copy(
                update={"filters": [*spec.filters, MetricFilter(column=segment, op="==", value=seg_value)]}
            )
            try:
                by_segment[seg_value] = ctx.tables.compute(path, seg_spec).by_group
            except Exception:
                continue
        by_segment = {k: v for k, v in by_segment.items() if v}
        if len(by_segment) > 1:
            # Разбивка в тексте строки: сравнение групп в целом может скрывать противоположное внутри сегментов
            note = f"По «{segment}»: " + "; ".join(
                f"{seg}: " + ", ".join(f"{g} {_fmt(v, spec.unit)}" for g, v in groups.items())
                for seg, groups in list(by_segment.items())[:6]
            )
        warning = simpson_reversal(result.by_group, by_segment)
        if warning:
            note = (note + ". " if note else "") + "Внимание: " + warning
            ctx.emit("metric.simpson", case.id, source_id=source.id, metric=spec.name)
    binary = False
    if spec.agg == "mean" and is_rate_column(spec.column, spec.unit) and spec.column:
        try:
            values = set(ctx.tables.distinct_values(path, spec.column, limit=4))
            binary = bool(values) and values <= {"0", "1", "0.0", "1.0", "True", "False", "true", "false"}
        except Exception:
            binary = False
    rates = mean_of_rates_warning(
        spec.agg, spec.column, spec.unit, result.rows_used, len(result.by_group), binary
    )
    shares = [result.value, *result.by_group.values()] if spec.agg == "ratio" else []
    named_share = is_rate_column(f"{spec.name} {spec.description}", spec.unit)
    if named_share and shares and all(v is not None and v > 1.0 for v in shares):
        # «доля оттока» = 131,6% — перепутаны числитель и знаменатель
        rates = (rates + "; " if rates else "") + (
            "подпись говорит о доле, а отношение больше 100% во всех группах — проверь числитель и знаменатель"
        )
    flipped = (
        "; ".join(f"{k} {_fmt(1 - v, spec.unit)}" for k, v in list(result.by_group.items())[:8])
        if result.by_group
        else (_fmt(1 - result.value, spec.unit) if result.value is not None else "—")
    )
    if IssueKind.CHURN_LABEL not in kinds and churn_label_on_retention(
        f"{spec.name} {spec.description}", spec.agg, spec.column
    ):  # v0.1.1: подпись остаётся, к строке — предупреждение с пересчётом
        rates = (rates + "; " if rates else "") + (
            f"подпись говорит об оттоке, а считается доля оставшихся ({spec.column} / {spec.denominator_column}); "
            f"отток = 1 − значение: {flipped}"
        )
        ctx.emit("metric.churn_label", case.id, source_id=source.id, metric=spec.name)
    if rates:
        note = (note + " " if note else "") + "Внимание: " + rates
        ctx.emit("metric.mean_of_rates", case.id, source_id=source.id, metric=spec.name)
    fixes = [
        f"подпись «{original_label}» исправлена: считается доля оставшихся, отток = 1 − значение: {flipped}"
        if i.kind == IssueKind.CHURN_LABEL
        else i.message
        for i in issues
        if i.kind != IssueKind.ROW_SHARE_AGGREGATED
    ]
    if fixes:
        note = _sentence(note, "Исправлено по контракту расчёта: " + "; ".join(fixes))
    if IssueKind.ROW_SHARE_AGGREGATED in kinds:
        blocked = next(i.message for i in issues if i.kind == IssueKind.ROW_SHARE_AGGREGATED)
        note = _sentence(note, "Внимание: " + blocked)
    if not result.consistent:
        ctx.emit("metric.mismatch", case.id, source_id=source.id, metric=spec.name)
    contract_on = enabled(ctx.settings, "metric_contract")
    claim = metric_claim(
        ctx,
        case,
        source,
        result,
        reserved,
        note,
        period=period_of(
            [(f.column, f.op, f.value) for f in spec.filters], passport.period if passport else None
        )
        if contract_on
        else None,
        rows=result.rows_used if contract_on else None,
        contract=[i.kind.value for i in issues],
    )
    if "Внимание" in note:  # разбивка по сегменту — справка, а предупреждение делает основание уязвимым
        claim.vulnerable = True
    return claim


def analyze_table(ctx: AppContext, case: DecisionCase, source: Source) -> list[Claim]:
    path = ctx.materials.absolute_path(case.id, source.path or "")
    if not ctx.tables.supports(path):
        return []
    passport = ctx.tables.passport(path)
    source.note = (
        source.note + "\n" if source.note else ""
    ) + f"Паспорт таблицы: {passport_text(passport)[:600]}"
    plan, _ = ask_structured(
        ctx,
        task="extract_claims",
        prompt="metric_plan",
        values={
            "questions": research_questions_text(case),
            "passport": passport_text(passport),
            "source_title": source.title,
            "synthetic": source.synthetic,
        },
        schema=MetricPlanDTO,
        case_id=case.id,
        step=f"collect:{source.id}",
    )
    reserved: list[str] = []
    claims = []
    seen: set[str] = set()
    for proposal in plan.metrics[:8]:
        signature = spec_from_proposal(proposal).model_dump_json(exclude={"name", "description", "unit"})
        if signature in seen:  # одинаковые расчёты под разными именами — одна строка реестра
            continue
        seen.add(signature)
        claim = compute_metric(ctx, case, source, proposal, reserved)
        if claim is not None:
            claims.append(claim)
    ctx.emit("ingest.table", case.id, source_id=source.id, metrics=len(claims))
    return claims


class IngestResult(BaseModel):
    """Результат одного читателя. ID временные: окончательные назначаются при слиянии (merge_ingest)."""

    source: Source
    evidence: list[Evidence] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    error: str = ""


def ingest_source(ctx: AppContext, case: DecisionCase, source_id: str) -> IngestResult:
    """Один читатель на один источник: цитаты (документы) или расчётные метрики (таблицы)."""
    source = case.source(source_id)
    if source is None:
        raise ValueError(f"Нет источника {source_id}")
    source = source.model_copy(deep=True)
    try:
        if source.kind == SourceKind.TABLE:
            result = IngestResult(source=source, claims=analyze_table(ctx, case, source))
        else:
            result = IngestResult(source=source, evidence=ingest_document(ctx, case, source))
        result.source.ingested = True
        return result
    except Exception as exc:  # недоступный инструмент — честный пробел, а не выдумка
        ctx.emit("ingest.failed", case.id, source_id=source.id, error=str(exc)[:300])
        return IngestResult(source=source, error=f"{type(exc).__name__}: {exc}"[:500])


def merge_ingest(ctx: AppContext, case: DecisionCase, results: list[IngestResult]) -> dict[str, int]:
    """Сливает результаты читателей в кейс с окончательными ID; сбой источника → запись в реестр неизвестного."""
    evidence_added = claims_added = failed = 0
    for result in sorted(results, key=lambda r: r.source.id):
        for i, src in enumerate(case.sources):
            if src.id == result.source.id:
                case.sources[i] = result.source
        id_map: dict[str, str] = {}
        for ev in result.evidence:
            new_id = case.new_id("evidence")
            id_map[ev.id] = new_id
            ev.id = new_id
            case.evidence.append(ev)
            evidence_added += 1
        for claim in result.claims:
            claim.id = case.new_id("claim")
            claim.evidence_ids = [id_map.get(e, e) for e in claim.evidence_ids]
            case.claims.append(claim)
            claims_added += 1
        if result.error:
            failed += 1
            case.unknowns.append(
                Unknown(
                    id=case.new_id("unknown"),
                    question=f"Что содержит источник «{result.source.title}»?",
                    why_it_matters="Материал не удалось обработать — выводы не опираются на него",
                    reason=f"Сбой обработки: {result.error}",
                    resolution_method="Проверить формат файла и повторить загрузку (strategist add)",
                    created_at=ctx.clock.now(),
                )
            )
    return {"evidence": evidence_added, "claims": claims_added, "failed": failed}
