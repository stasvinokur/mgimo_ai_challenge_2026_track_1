"""CLI «Стратег Фабрики проектов» (Typer + Rich). Только use cases и bootstrap — без infrastructure."""

from __future__ import annotations

import json
import re
import shutil
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from strategist import __version__, bootstrap
from strategist.application.errors import StrategistError
from strategist.application.ports import (
    Event,
    GateAction,
    GateKind,
    GateRequest,
    GateResponse,
    HumanTimeRecord,
    RunStatus,
)
from strategist.application.use_cases.report import build_report
from strategist.application.use_cases.start_case import start_case
from strategist.domain.services.revisit_rules import stale_grounds, what_to_decide_now
from strategist.interfaces.demo import collect_paths, demo_inputs, strip_front_matter

app = typer.Typer(
    name="strategist",
    help="ИИ-стратег: от размытого запроса к проверяемой продуктовой и коммуникационной стратегии.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

QUIET_EVENTS = {"step.skipped", "llm.cache_hit"}


def _progress(event: Event) -> None:
    d = event.data
    if event.type == "step.started":
        console.print(f"[cyan]▶ {d.get('step')}[/]")
    elif event.type == "step.completed":
        console.print(f"[green]✓ {d.get('step')}[/]")
    elif event.type == "llm.called":
        console.print(
            f"   [dim]{d.get('task')} → {d.get('model')} · {d.get('input_tokens')}/{d.get('output_tokens')} ток."
            f" · {d.get('latency_ms', 0) / 1000:.1f} с[/]"
        )
    elif event.type in ("route.fallback", "route.escalated", "route.misrouted"):
        console.print(f"   [yellow]↻ {event.type}: {d}[/]")
    elif event.type == "injection.detected":
        console.print(f"   [red]⚠ инъекция в {d.get('source_id')}: не исполняется, записана[/]")
    elif event.type == "step.failed":
        console.print(f"[red]✗ {d.get('step')}: {d.get('error')}[/]")


def _app(
    case_id: str | None = None,
    *,
    auto: bool = False,
    record: bool = False,
    replay: bool = False,
    web: bool | None = None,
    cases_dir: Path | None = None,
    verbose: bool = True,
    cassettes: Path | None = None,
    update: bool = False,
) -> bootstrap.App:
    mode = "replay" if replay else ("record" if record else ("update" if update else "live"))
    if mode != "live" and cassettes is None:
        if case_id is None:
            raise typer.BadParameter("для --record/--replay/--update нужен идентификатор кейса")
        base = cases_dir or bootstrap.settings().paths.cases_dir
        cassettes = base / case_id / "cassettes"
    application = bootstrap.build_app(
        mode=mode, cassette_dir=cassettes, interactive=not auto, allow_web=web, cases_dir=cases_dir
    )
    if verbose:
        application.subscribe(_progress)
    return application


def _ask_gate(gate: GateRequest) -> GateResponse:
    console.print(Panel(Markdown(gate.summary or "—"), title=f"Шлюз: {gate.title}", border_style="magenta"))
    if gate.gate == GateKind.CLARIFY:
        answers = [Prompt.ask(f"[bold]{q}[/]", default="") for q in gate.questions]
        return GateResponse(answers=answers, comment=Prompt.ask("Комментарий (необязательно)", default=""))
    if gate.gate == GateKind.RESEARCH_PLAN:
        action = Prompt.ask(
            "Принять план? (a — принять, e — править, r — отклонить)", choices=["a", "e", "r"], default="a"
        )
        if action == "r":
            return GateResponse(action=GateAction.REJECT, comment=Prompt.ask("Что изменить в плане"))
        if action == "e":
            exclude = [
                x.strip()
                for x in Prompt.ask("Исключить вопросы (ID через запятую)", default="").split(",")
                if x.strip()
            ]
            include = [
                x.strip() for x in Prompt.ask("Вернуть пропущенные (ID)", default="").split(",") if x.strip()
            ]
            add = [
                x.strip()
                for x in Prompt.ask("Добавить вопросы (через ;)", default="").split(";")
                if x.strip()
            ]
            return GateResponse(
                action=GateAction.EDIT, edits={"exclude": exclude, "include": include, "add": add}
            )
        return GateResponse(action=GateAction.APPROVE)
    if gate.gate == GateKind.OPTION_CHOICE:
        choice = Prompt.ask("Выберите вариант", choices=gate.choices, default=gate.default_choice)
        return GateResponse(choice=choice, comment=Prompt.ask("Почему (необязательно)", default=""))
    if gate.gate == GateKind.DECISION_COMMIT:
        if Confirm.ask("Зафиксировать решение?", default=True):
            return GateResponse(
                action=GateAction.APPROVE,
                approver=Prompt.ask("Кто утверждает", default="руководитель продукта"),
            )
        return GateResponse(action=GateAction.REJECT, comment=Prompt.ask("Почему отклоняете", default=""))
    return GateResponse()


def _drive(application: bootstrap.App, case_id: str, status: RunStatus, *, auto: bool) -> RunStatus:
    while status.waiting_gate is not None and not auto:
        response = _ask_gate(status.waiting_gate)
        status = application.runner.resume(case_id, response)
    return status


def _print_status(status: RunStatus) -> None:
    table = Table(title=f"Кейс {status.case_id}")
    table.add_column("Поле")
    table.add_column("Значение")
    table.add_row("Этап", status.stage_label or status.stage)
    table.add_row("Ждёт решения человека", status.waiting_gate.title if status.waiting_gate else "—")
    table.add_row("Завершено", "да" if status.finished else "нет")
    table.add_row("Шагов выполнено", str(len(status.completed_steps)))
    if status.cost:
        c = status.cost
        table.add_row("Вызовов моделей", str(c.calls))
        table.add_row("Токены вход/выход", f"{c.input_tokens:,} / {c.output_tokens:,}".replace(",", " "))
        table.add_row("Стоимость по прайсу", f"${c.usd:.4f} ≈ {c.rub:.2f} ₽")
    console.print(table)


def _run(application: bootstrap.App, case_id: str, *, auto: bool) -> RunStatus:
    try:
        status = application.runner.resume(case_id)
        status = _drive(application, case_id, status, auto=auto)
    except KeyboardInterrupt:
        console.print(
            f"\n[yellow]Остановлено. Завершённые шаги сохранены. Продолжить: strategist resume {case_id}[/]"
        )
        raise typer.Exit(code=130) from None
    except (
        EOFError
    ):  # ввод закрыт (скрипт, CI, другой агент): кейс ждёт на шлюзе, ответ — позже или через MCP
        console.print(
            f"\n[yellow]Кейс ждёт решения человека на шлюзе. Ответить: strategist resume {case_id} "
            "или из IDE через MCP (case_status, answer_gate).[/]"
        )
        raise typer.Exit(code=3) from None
    except StrategistError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from None
    _print_status(status)
    if status.finished:
        out = application.cases_dir / case_id / "out" / "report.md"
        console.print(f"[green]Готово. Отчёт: {out}[/]")
    return status


@app.command()
def version() -> None:
    """Версия агента."""
    console.print(f"strategist {__version__}")


@app.command()
def doctor() -> None:
    """Проверить окружение: Ollama, модели ролей, decide()."""
    checks = bootstrap.diagnostics()
    table = Table(title="Проверка окружения")
    table.add_column("Проверка")
    table.add_column("Статус")
    table.add_column("Детали")
    failed_required = False
    for check in checks:
        status = "[green]OK[/]" if check.ok else ("[red]FAIL[/]" if check.required else "[yellow]WARN[/]")
        failed_required |= check.required and not check.ok
        table.add_row(check.name, status, check.detail)
    console.print(table)
    if failed_required:
        console.print("Есть обязательные проблемы. См. README → «Быстрый старт».")
        raise typer.Exit(code=1)


@app.command()
def cases() -> None:
    """Список кейсов."""
    application = _app(verbose=False)
    table = Table(title="Кейсы")
    for col in ("ID", "Название", "Этап", "Версия", "Обновлён"):
        table.add_column(col)
    for c in application.ctx.cases.list_cases():
        table.add_row(c.id, c.title, c.stage, str(c.version), c.updated_at.strftime("%Y-%m-%d %H:%M"))
    console.print(table)


@app.command()
def new(
    case_id: str = typer.Argument(..., help="Идентификатор кейса (латиница, дефисы)"),
    title: str = typer.Option(..., "--title", "-t", help="Название кейса"),
    brief: str = typer.Option(None, "--brief", "-b", help="Текст запроса руководителя"),
    brief_file: Path = typer.Option(None, "--brief-file", help="Файл с запросом"),
    materials: list[Path] = typer.Option(None, "--material", "-m", help="Файл материала (можно несколько)"),
    materials_dir: Path = typer.Option(None, "--materials-dir", help="Каталог материалов"),
    run: bool = typer.Option(False, "--run", help="Сразу запустить работу"),
    auto: bool = typer.Option(False, "--auto", help="Автоответы на шлюзах (скриптовый прогон)"),
) -> None:
    """Создать кейс из запроса и материалов."""
    text = brief or (strip_front_matter(brief_file.read_text(encoding="utf-8")) if brief_file else None)
    if not text:
        raise typer.BadParameter("Нужен --brief или --brief-file")
    application = _app(auto=auto)
    case = start_case(
        application.ctx,
        case_id=case_id,
        title=title,
        brief=text,
        material_paths=collect_paths(materials, materials_dir),
    )
    console.print(f"[green]Кейс {case.id} создан: материалов {len(case.sources)}[/]")
    if run:
        _run(application, case_id, auto=auto)


@app.command()
def run(
    case_id: str,
    auto: bool = typer.Option(False, "--auto", help="Автоответы на шлюзах"),
    record: bool = typer.Option(False, "--record", help="Записывать ответы моделей в кассеты"),
    replay: bool = typer.Option(False, "--replay", help="Воспроизвести записанный прогон без моделей"),
    web: bool = typer.Option(None, "--web/--no-web", help="Разрешить загрузку сайтов из allowlist"),
) -> None:
    """Запустить или продолжить работу над кейсом (до следующего шлюза или до конца)."""
    _run(_app(case_id, auto=auto, record=record, replay=replay, web=web), case_id, auto=auto)


@app.command()
def resume(
    case_id: str,
    auto: bool = typer.Option(False, "--auto"),
    record: bool = typer.Option(False, "--record"),
    replay: bool = typer.Option(False, "--replay"),
) -> None:
    """Продолжить с сохранённого места — в том числе из нового терминала или чата."""
    _run(_app(case_id, auto=auto, record=record, replay=replay), case_id, auto=auto)


@app.command()
def status(case_id: str) -> None:
    """Статус кейса: этап, шлюз, стоимость."""
    _print_status(_app(verbose=False).runner.status(case_id))


@app.command()
def add(
    case_id: str,
    files: list[Path] = typer.Argument(..., help="Новые материалы"),
    record: bool = typer.Option(False, "--record"),
    replay: bool = typer.Option(False, "--replay"),
) -> None:
    """Добавить новые данные и точечно пересмотреть затронутые выводы."""
    application = _app(case_id, record=record, replay=replay)
    try:
        st = application.runner.revise(case_id, [str(f) for f in files])
    except StrategistError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from None
    case = application.ctx.cases.load(case_id)
    if case.revisions:
        console.print(Panel(case.revisions[-1].summary, title="Что изменилось", border_style="yellow"))
    _print_status(st)


@app.command()
def now(case_id: str) -> None:
    """Что решать сейчас и какие основания устарели."""
    application = _app(verbose=False)
    case = application.ctx.cases.load(case_id)
    today = application.ctx.clock.today()
    agenda = what_to_decide_now(case, today)
    console.print(
        Panel(
            "\n".join(f"• {a.title}" + (f" (до {a.due})" if a.due else "") for a in agenda) or "Срочного нет",
            title="Что решать сейчас",
        )
    )
    stale = stale_grounds(case, today)
    console.print(
        Panel(
            "\n".join(f"• [{s.claim_id}] {s.text[:100]} — {'; '.join(s.reasons)}" for s in stale)
            or "Устаревших оснований нет",
            title="Устаревшие основания",
        )
    )


@app.command()
def audit(
    case_id: str,
    limit: int = typer.Option(12, "--limit", help="Сколько несущих утверждений показать"),
    as_json: bool = typer.Option(False, "--json", help="Вывести JSON"),
) -> None:
    """Проверка оснований: на чём держится решение — цитата, «за» и «против» по каждой несущей строке."""
    from strategist.application.use_cases.challenge import audit_view

    rows = audit_view(_app(verbose=False).ctx, case_id, limit)
    if as_json:
        typer.echo(json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False, indent=1))
        return
    for r in rows:
        src = "; ".join(
            f"«{s.quote[:100]}» — {s.source_id}" if not s.is_calculation else f"расчёт по {s.source_id}"
            for s in r.sources
        )
        body = [
            f"{r.text[:220]}",
            f"[dim]{r.type_label} · опора: {r.support} · зависят узлов: {r.dependents}[/]",
        ]
        body += [f"↳ {src}"] if src else []
        body += [f"[green]за:[/] {p}" for p in r.pro] + [f"[red]против:[/] {c}" for c in r.con]
        console.print(Panel("\n".join(body), title=f"[{r.claim_id}] {r.why_first}", border_style="cyan"))


@app.command()
def challenge(
    case_id: str,
    node_ids: list[str] = typer.Argument(
        ..., help="ID строк, цитат или источников, которые считаем неверными"
    ),
    as_json: bool = typer.Option(False, "--json", help="Вывести JSON"),
) -> None:
    """Что потеряет опору, если считать основания неверными. Кейс не меняется, модели не вызываются."""
    from strategist.application.use_cases.challenge import preview_challenge

    preview = preview_challenge(_app(verbose=False).ctx, case_id, node_ids)
    if as_json:
        typer.echo(json.dumps(preview.model_dump(mode="json"), ensure_ascii=False, indent=1))
        return
    lines = [
        f"• {x.kind} {x.node_id} — {x.effect}" + ("" if x.direct else " (косвенно)") for x in preview.lost
    ]
    lines += [f"[green]• {x.kind} {x.node_id} — {x.effect}[/]" for x in preview.holding]
    if preview.unknown:
        lines.append(f"[yellow]Нет в кейсе: {', '.join(preview.unknown)}[/]")
    console.print(Panel("\n".join(lines) or "—", title=preview.summary, border_style="magenta"))


@app.command()
def contest(
    case_id: str,
    claim_ids: list[str] = typer.Argument(
        ..., help="ID строк реестра, которые руководитель считает неверными"
    ),
    reason: str = typer.Option(..., "--reason", "-r", help="Почему основание оспорено"),
) -> None:
    """Оспорить основания без новых файлов: точечный пересмотр зависимых выводов (вызывает модели)."""
    application = _app(case_id)
    try:
        st = application.runner.contest(case_id, claim_ids, reason)
    except (StrategistError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from None
    case = application.ctx.cases.load(case_id)
    if case.revisions:
        console.print(Panel(case.revisions[-1].summary, title="Что изменилось", border_style="yellow"))
    _print_status(st)


@app.command()
def cost(case_id: str) -> None:
    """Чек стоимости: вызовы, токены, деньги по шагам и моделям, время человека."""
    application = _app(verbose=False)
    s = application.ctx.costs.summary(case_id)
    table = Table(title=f"Чек стоимости — {case_id}")
    for col in ("Шаг", "Вызовы", "Вход", "Выход", "$"):
        table.add_column(col)
    for step, v in sorted(s.by_step.items(), key=lambda kv: (-kv[1].get("usd", 0), kv[0])):
        table.add_row(
            step,
            str(int(v["calls"])),
            str(int(v["input_tokens"])),
            str(int(v["output_tokens"])),
            f"{v['usd']:.5f}",
        )
    console.print(table)
    models = Table(title="По моделям")
    for col in ("Модель", "Вызовы", "Токены", "$"):
        models.add_column(col)
    for model, v in s.by_model.items():
        models.add_row(
            model, str(int(v["calls"])), str(int(v["input_tokens"] + v["output_tokens"])), f"{v['usd']:.5f}"
        )
    console.print(models)
    console.print(
        f"Итого: {s.calls} вызовов, {s.input_tokens + s.output_tokens:,} токенов; ${s.usd:.4f} по прайсу ≈ {s.rub:.2f} ₽; "
        f"на подписке ≈ ${s.subscription_share_usd:.4f}; человек {s.human_minutes:.0f} мин ≈ {s.human_rub:.0f} ₽; "
        f"всего ≈ {s.total_rub:.2f} ₽".replace(",", " ")
    )


@app.command()
def time(case_id: str, minutes: float, note: str = typer.Option("", "--note")) -> None:
    """Записать время человека (для полной стоимости)."""
    application = _app(verbose=False)
    application.ctx.costs.record_human(
        HumanTimeRecord(case_id=case_id, minutes=minutes, note=note, at=application.ctx.clock.now())
    )
    console.print(f"Записано {minutes} мин.")


@app.command()
def report(case_id: str, open_: bool = typer.Option(False, "--open", help="Открыть HTML в браузере")) -> None:
    """Пересобрать отчёт руководителя (Markdown + HTML)."""
    application = _app(verbose=False)
    paths = build_report(application.ctx, application.ctx.cases.load(case_id))
    console.print(f"Отчёт: {paths.get('markdown')}\nHTML: {paths.get('html')}")
    if open_ and paths.get("html"):
        webbrowser.open(Path(paths["html"]).as_uri())


@app.command()
def ledger(case_id: str, type_: str = typer.Option(None, "--type", help="fact | hypothesis | …")) -> None:
    """Реестр утверждений кейса."""
    application = _app(verbose=False)
    case = application.ctx.cases.load(case_id)
    table = Table(title=f"Реестр — {case_id}")
    for col in ("ID", "Тип", "Опора", "Надёжность", "Статус", "Утверждение"):
        table.add_column(col)
    for c in case.claims:
        if type_ and c.type.value != type_:
            continue
        table.add_row(
            c.id, c.type.label_ru, c.support.value, c.reliability.label_ru, c.status.value, c.text[:120]
        )
    console.print(table)


@app.command()
def events(case_id: str, limit: int = typer.Option(40, "--limit")) -> None:
    """Что агент сделал: журнал событий кейса."""
    application = _app(verbose=False)
    for e in application.event_log.read(case_id, limit=limit):
        console.print(f"[dim]{e.at:%H:%M:%S}[/] {e.type} {e.data}")


def _demo_paths(source: Path) -> tuple[str, list[str], list[str]]:
    inputs = demo_inputs(source)
    return inputs.brief, inputs.materials, inputs.new_evidence


@app.command()
def demo(
    name: str = typer.Option("demo-retention", "--case", help="Каталог демо-кейса в cases/"),
    record: bool = typer.Option(
        False, "--record", help="Прогон с моделями и запись кассет в каталог демо-кейса"
    ),
    replay: bool = typer.Option(False, "--replay", help="Воспроизвести записанный прогон без моделей"),
    update: bool = typer.Option(
        False,
        "--update",
        help="Дозапись кассет: есть ответ — воспроизвести, нет — вызвать модель и дописать (кассеты не стираются)",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="С --replay: код 1, если шаг упал или отчёт разошёлся с записанным (кроме чека стоимости)",
    ),
    work_dir_opt: Path | None = typer.Option(
        None, "--work-dir", help="Каталог воспроизведения (по умолчанию cases/scratch-replay)"
    ),
    revise_after: bool = typer.Option(
        True, "--revise/--no-revise", help="После решения подать «новые данные»"
    ),
    panel_after: bool = typer.Option(
        True, "--panel/--no-panel", help="После решения — панель модельных респондентов (СИНТЕТИКА)"
    ),
) -> None:
    """Сквозной прогон демо-кейса: запрос → исследование → варианты → стратегии → решение → панель → пересмотр."""
    if sum((record, replay, update)) > 1:
        raise typer.BadParameter("--record, --replay и --update взаимоисключающие")
    if check and not replay:
        raise typer.BadParameter("--check — только вместе с --replay")
    base = bootstrap.settings().paths.cases_dir
    source = base / name
    if not (source / "brief.md").exists():
        raise typer.BadParameter(f"Нет {source / 'brief.md'}")
    cassettes = source / "cassettes"
    run_id = f"{name}-run"
    # Воспроизведение — в отдельном каталоге (не трогает записанный прогон); запись — рядом с кейсом.
    work_dir = (work_dir_opt or base / "scratch-replay") if replay else base
    if (replay or update) and not (cassettes / "meta.json").exists():
        raise typer.BadParameter(f"Нет записанного прогона: {cassettes}. Запустите: strategist demo --record")
    if replay or update:
        # update: прогон кейса заново, но ответы берутся из кассет — модель зовётся только для новых запросов
        shutil.rmtree(work_dir / run_id, ignore_errors=True)
    if record:
        # Запись — всегда с чистого листа: иначе продолжится прежний прогон, а старые кассеты смешаются с новыми.
        shutil.rmtree(work_dir / run_id, ignore_errors=True)
        shutil.rmtree(cassettes, ignore_errors=True)
    application = _app(
        run_id,
        auto=True,
        record=record,
        replay=replay,
        update=update,
        cases_dir=work_dir,
        cassettes=cassettes if (record or replay or update) else None,
    )
    failures: list[str] = []
    application.subscribe(lambda e: failures.append(str(e.data.get("step"))), "step.failed")
    brief, materials, new_ev = _demo_paths(source)
    if not application.ctx.cases.exists(run_id):
        title = next((line.lstrip("# ").strip() for line in brief.splitlines() if line.strip()), name)
        start_case(application.ctx, case_id=run_id, title=title, brief=brief, material_paths=materials)
    status_ = _run(application, run_id, auto=True)
    if panel_after and status_.finished:
        from strategist.application.use_cases.panel import run_panel_for_case

        run = run_panel_for_case(application.ctx, run_id, 6)
        console.print(
            Panel(
                f"{len(run.personas)} персон · доверие: {run.trust} · разногласий: {len(run.disagreements)} · "
                f"вопросов для интервью: {len(run.interview_questions)}",
                title=f"Модельные респонденты — {run.label}",
            )
        )
    if revise_after and new_ev and status_.finished:
        st = application.runner.revise(run_id, new_ev)
        case = application.ctx.cases.load(run_id)
        console.print(Panel(case.revisions[-1].summary if case.revisions else "—", title="Что изменилось"))
        _print_status(st)
        build_report(application.ctx, application.ctx.cases.load(run_id))
    report = work_dir / run_id / "out" / "report.md"
    console.print(f"[green]Отчёт: {report}[/]")
    if update:
        llm, decision = application.ctx.llm, application.ctx.decision
        stored = {p.name for p in cassettes.glob("*.json")} - {"meta.json", "embeddings.json"}
        unused = len(stored - getattr(llm, "used", set()))
        console.print(
            f"Кассеты: новых ответов моделей {getattr(llm, 'recorded', 0)}, decide() — "
            f"{getattr(decision, 'recorded', 0)}; записей, к которым прогон не обращался: {unused}"
        )
    if check:
        recorded = base / run_id / "out" / "report.md"
        problems = [f"упал шаг {s}" for s in failures]
        if not status_.finished:
            problems.append("прогон не дошёл до конца")
        if recorded.exists() and report.exists():
            if comparable_report(report.read_text(encoding="utf-8")) != comparable_report(
                recorded.read_text(encoding="utf-8")
            ):
                problems.append(f"отчёт разошёлся с записанным ({recorded})")
        else:
            problems.append("нет отчёта для сравнения")
        if problems:
            console.print("[red]Проверка демо не пройдена: " + "; ".join(problems) + "[/]")
            raise typer.Exit(code=1)
        console.print(
            "[green]Проверка демо пройдена: воспроизведение без промахов, отчёт совпал с записанным.[/]"
        )


def comparable_report(text: str) -> str:
    """Отчёт без того, что честно отличается у воспроизведения: чек стоимости (в воспроизведении $0 и меньше вызовов)
    и токены пересмотра (повторные вызовы при записи)."""
    text = re.sub(r"\n## Чек стоимости\n.*?(?=\n## |\Z)", "\n", text, flags=re.S)
    return re.sub(r"(?m)^Токенов на пересмотр: .*$", "", text)


@app.command()
def ui(
    port: int = typer.Option(8501, "--port"),
    host: str = typer.Option("127.0.0.1", "--host"),
    demo: bool = typer.Option(
        False, "--demo", help="Демо по записанным ответам моделей: «Открыть демо» и шлюзы без моделей"
    ),
) -> None:
    """Панель руководителя в браузере: решение, факты, отчёт; шлюзы и новые данные."""
    import uvicorn

    from strategist.interfaces.web.api import create_app

    if demo:
        base = bootstrap.settings().paths.cases_dir
        source = base / "demo-retention"
        cassettes = source / "cassettes"
        if not (cassettes / "meta.json").exists():
            raise typer.BadParameter(f"Нет записанного прогона: {cassettes}")
        application = bootstrap.build_app(
            mode="replay", cassette_dir=cassettes, interactive=True, cases_dir=base / "scratch-ui"
        )
        web = create_app(application, demo_source=source)
    else:
        web = create_app(bootstrap.build_app(interactive=True))
    console.print(f"[green]Панель: http://{host}:{port}[/]" + (" (демо без моделей)" if demo else ""))
    uvicorn.run(web, host=host, port=port, log_level="warning")


@app.command("interview-lint")
def interview_lint(guide: Path = typer.Argument(..., help="Файл гайда: по вопросу на строку")) -> None:
    """Линтер гайда интервью (Mom Test): гипотетические, наводящие, двойные и общие вопросы."""
    from strategist.domain.services.synthetic_panel import lint_interview_guide

    questions = [q.strip() for q in guide.read_text(encoding="utf-8").splitlines() if q.strip()]
    table = Table(title="Гайд интервью")
    for col in ("Вопрос", "Проблемы", "Как спросить о прошлом поведении"):
        table.add_column(col)
    for f in lint_interview_guide(questions):
        table.add_row(
            f.question, ", ".join(i.value for i in f.issues) or "ок", f.suggestion if f.issues else "—"
        )
    console.print(table)


@app.command()
def panel(
    case_id: str,
    personas: int = typer.Option(6, "--personas", min=3, max=12, help="Сколько модельных респондентов"),
) -> None:
    """Панель модельных респондентов (СИНТЕТИКА): разногласия профилей и вопросы для реальных интервью."""
    from strategist.application.use_cases.panel import run_panel_for_case

    application = _app(verbose=False)
    try:
        run = run_panel_for_case(application.ctx, case_id, personas)
    except StrategistError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from None
    names = {p.id: p.name for p in run.personas}
    table = Table(title=f"{run.id} · {run.label} · доверие: {run.trust}")
    for col in ("Концепт", "Разброс", "«За»", "«Против»"):
        table.add_column(col)
    for d in run.disagreements:
        table.add_row(
            run.concepts.get(d.concept_id, d.concept_id),
            f"{d.spread:.1f}",
            ", ".join(names.get(p, p) for p in d.supporters) or "—",
            ", ".join(names.get(p, p) for p in d.opponents) or "—",
        )
    console.print(table)
    if run.placebo_liked:
        console.print("[yellow]Плацебо-концепт понравился персонам — доверие к панели низкое.[/]")
    for q in run.interview_questions:
        console.print(f"• {q.question}" + (f" [yellow]⚠ {', '.join(q.issues)}[/]" if q.issues else ""))
    console.print(f"[dim]Отчёт обновлён: {application.cases_dir / case_id / 'out' / 'report.md'}[/]")


@app.command()
def mcp() -> None:
    """MCP-сервер (stdio) для IDE-агентов: Claude Code, Cursor, Codex — подключение в .mcp.json."""
    from strategist.interfaces.mcp.server import main as mcp_main

    mcp_main()


kb_app = typer.Typer(
    help="База знаний: методика, материалы и реестр кейсов (поиск по формулировке, смыслу, связям)."
)
memory_app = typer.Typer(help="Память между сеансами с происхождением записей.")
app.add_typer(kb_app, name="kb")
app.add_typer(memory_app, name="memory")


@kb_app.command("index")
def kb_index(
    case: list[str] = typer.Option(None, "--case", help="Кейсы для индексации (по умолчанию все)"),
) -> None:
    """Пересобрать индекс из файлов методики, навыков и кейсов."""
    application = _app(verbose=False)
    counts = bootstrap.reindex_knowledge(application, case or None)
    console.print("Проиндексировано: " + ", ".join(f"{k}: {v}" for k, v in counts.items()))


@kb_app.command("search")
def kb_search(query: str, case: str = typer.Option(None, "--case"), k: int = typer.Option(8, "-k")) -> None:
    """Поиск (FTS5 + векторы + связи, RRF). Видно, что найдено: методика, материал или вывод."""
    application = _app(verbose=False)
    table = Table(title=f"Поиск: {query}")
    for col in ("Документ", "Вид", "Ранги", "Фрагмент"):
        table.add_column(col)
    for hit in application.ctx.knowledge.search(query, k=k, case_id=case):
        table.add_row(hit.title or hit.doc_id, hit.kind, str(hit.ranks), hit.snippet[:160])
    console.print(table)


@memory_app.command("list")
def memory_list(case: str = typer.Option(None, "--case"), all_: bool = typer.Option(False, "--all")) -> None:
    """Записи памяти: происхождение, статус, можно ли использовать как доказательство."""
    from strategist.application.use_cases.memory import evidence_block_reason

    application = _app(verbose=False)
    table = Table(title="Память")
    for col in ("ID", "Происхождение", "Статус", "Доказательство?", "Текст"):
        table.add_column(col)
    today = application.ctx.clock.today()
    for m in application.ctx.knowledge.memories(case, include_inactive=all_):
        reason = evidence_block_reason(m, today)
        table.add_row(
            m.id, m.origin.value, m.status.value, "да" if not reason else f"нет: {reason}", m.text[:100]
        )
    console.print(table)


@memory_app.command("add")
def memory_add(
    text: str, origin: str = typer.Option("user_stated", "--origin"), case: str = typer.Option(None, "--case")
) -> None:
    """Записать сведение в память (origin: user_stated | source_document | external_data | agent_inference)."""
    from strategist.application.use_cases.memory import remember

    record = remember(_app(verbose=False).ctx, case, text, origin)
    console.print(f"Записано {record.id} ({record.origin.value}, {record.epistemic_status})")


@memory_app.command("approve")
def memory_approve(memory_id: str) -> None:
    """Одобрить вывод агента: после этого он может служить доказательством (одобрение журналируется)."""
    from strategist.application.use_cases.memory import approve_memory

    approve_memory(_app(verbose=False).ctx, memory_id)
    console.print(f"{memory_id}: одобрено")


@memory_app.command("retract")
def memory_retract(memory_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """Отозвать устаревшую запись (не удаляется, а помечается)."""
    from strategist.application.use_cases.memory import retract_memory

    retract_memory(_app(verbose=False).ctx, memory_id, reason)
    console.print(f"{memory_id}: отозвано")


@memory_app.command("correct")
def memory_correct(memory_id: str, text: str) -> None:
    """Исправить запись: старая помечается исправленной, новая — активна."""
    from strategist.application.use_cases.memory import correct_memory

    record = correct_memory(_app(verbose=False).ctx, memory_id, text)
    console.print(f"{memory_id} → {record.id}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
