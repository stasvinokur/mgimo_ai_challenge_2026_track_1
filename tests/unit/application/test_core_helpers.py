import pytest
from pydantic import BaseModel

from fakes import InMemoryMaterialStore, ScriptedLLM, make_context
from strategist.application.errors import NeedsConfirmation, SchemaError
from strategist.application.llm_calls import ask_structured
from strategist.application.steps import run_step
from strategist.application.use_cases.start_case import start_case
from strategist.domain.entities import Reliability, SourceKind


def test_start_case_on_fakes(tmp_path):
    (tmp_path / "market.md").write_text("Рынок подписок растёт", encoding="utf-8")
    (tmp_path / "cohorts.csv").write_text("month,m3\n2026-01,0.41\n", encoding="utf-8")
    ctx = make_context()
    ctx.materials = InMemoryMaterialStore(
        ctx.clock,
        sidecar_meta={"cohorts.csv": {"title": "Когорты", "kind": "table", "synthetic": True}},
    )
    case = start_case(
        ctx,
        case_id="demo",
        title="Удержание",
        brief="Почему падает удержание?",
        material_paths=[str(tmp_path / "market.md"), str(tmp_path / "cohorts.csv")],
    )
    loaded = ctx.cases.load("demo")
    assert loaded.version == 1
    assert [s.id for s in loaded.sources] == ["SRC-001", "SRC-002"]
    cohorts = loaded.sources[1]
    assert cohorts.kind == SourceKind.TABLE and cohorts.synthetic
    assert cohorts.synthetic_kind == "simulated_data"
    assert cohorts.reliability == Reliability.LOW  # смоделированные данные — обозначены, но не «вывод модели»
    assert case.changelog[0].summary == "Кейс создан"
    assert len(ctx.events.of("material.registered")) == 2
    with pytest.raises(ValueError):
        start_case(ctx, case_id="demo", title="x", brief="x", material_paths=[])


def test_run_step_is_idempotent():
    ctx = make_context()
    calls = []

    def work():
        calls.append(1)
        return {"value": 42}

    assert run_step(ctx, "c", "frame", {"a": 1}, work) == {"value": 42}
    assert run_step(ctx, "c", "frame", {"a": 1}, work) == {"value": 42}
    assert len(calls) == 1
    run_step(ctx, "c", "frame", {"a": 2}, work)  # другой вход → выполняется заново
    assert len(calls) == 2


def test_interrupted_side_effect_step_requires_confirmation():
    ctx = make_context()

    def boom():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_step(ctx, "c", "publish", {}, boom, side_effect=True)
    # Эмулируем обрыв процесса: intent есть, done нет.
    ctx.journal.intent("c", ctx.run_id, "publish", "x", side_effect=True, at=ctx.clock.now())
    with pytest.raises(NeedsConfirmation):
        run_step(ctx, "c", "publish", {}, lambda: {"ok": True}, side_effect=True)
    assert run_step(ctx, "c", "publish", {}, lambda: {"ok": True}, side_effect=True, confirmed=True) == {
        "ok": True
    }


class Answer(BaseModel):
    verdict: str
    score: int


def test_ask_structured_retries_then_escalates():
    llm = ScriptedLLM({"frame": [{"verdict": "x"}, {"wrong": 1}, {"verdict": "ok", "score": 3}]})
    ctx = make_context(llm=llm)
    obj, usage = ask_structured(ctx, task="frame", prompt="frame", values={"brief": "b"}, schema=Answer)
    assert obj.score == 3
    assert [c.escalation for c in llm.calls] == [0, 0, 1]
    assert usage.escalated is True
    assert len(ctx.events.of("llm.schema_error")) == 2


def test_ask_structured_gives_up_with_schema_error():
    ctx = make_context(llm=ScriptedLLM({"frame": [{"bad": 1}]}))
    with pytest.raises(SchemaError):
        ask_structured(ctx, task="frame", prompt="frame", values={}, schema=Answer)


def test_formula_shows_what_was_actually_computed():
    from strategist.application.ports.tools import MetricFilter, MetricSpec
    from strategist.application.use_cases.collect import formula, spec_from_proposal

    share = MetricSpec(name="channel_share", agg="share", group_by="channel")
    assert formula(share) == "доля строк; по «channel»"
    users = MetricSpec(name="users_share", agg="sum_share", column="users", group_by="channel")
    assert formula(users) == "доля суммы users; по «channel»"
    ratio = MetricSpec(
        name="m3",
        agg="ratio",
        column="active_m3",
        denominator_column="new_users",
        filters=[MetricFilter(column="plan", op="==", value="basic")],
    )
    assert formula(ratio).startswith("сумма active_m3 / сумма new_users")
    assert "фильтр: plan == basic" in formula(ratio)
    from strategist.application.dto.llm_outputs import MetricProposalDTO

    kept = spec_from_proposal(MetricProposalDTO(agg="sum_share", column="users", group_by="channel"))
    assert kept.agg == "sum_share"
    assert spec_from_proposal(MetricProposalDTO(agg="p95", column="users")).agg == "mean"


def test_missing_prompt_value_fails_before_calling_model():
    from fakes import DictPrompts
    from strategist.application.ports import PromptTemplate

    class Answer(BaseModel):
        ok: bool = True

    llm = ScriptedLLM({"t": {"ok": True}})
    ctx = make_context(llm=llm)
    ctx.prompts = DictPrompts(
        {"panel": PromptTemplate(name="panel", version="1", system="Ты — {{role}}.", user="{{options}}")}
    )
    with pytest.raises(ValueError, match="role"):
        ask_structured(ctx, task="t", prompt="panel", values={"options": "A"}, schema=Answer)
    obj, _ = ask_structured(
        ctx, task="t", prompt="panel", values={"options": "A", "role": "CFO"}, schema=Answer
    )
    assert obj.ok and llm.calls[-1].system == "Ты — CFO."
    assert len(llm.calls) == 1  # без значения модель не вызывалась
