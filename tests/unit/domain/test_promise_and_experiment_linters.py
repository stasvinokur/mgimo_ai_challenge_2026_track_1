from datetime import date

from factories import add_claim, add_option, empty_case, grounded_fact
from strategist.domain.entities import (
    BarrierType,
    ClaimType,
    CommsStrategy,
    Experiment,
    Feature,
    FeatureStatus,
    Guardrail,
    KillCriterion,
    OptionKind,
    ProductStrategy,
    Promise,
    PromiseLocation,
    PromiseStatus,
)
from strategist.domain.services.experiment_linter import experiment_ok, lint_experiment
from strategist.domain.services.promise_linter import apply_findings, lint_promises


def _case_with_strategies(promises):
    case = empty_case()
    fact = grounded_fact(case, "Настройка занимает 3 минуты")
    opt = add_option(case, OptionKind.INVEST)
    case.product = ProductStrategy(
        option_id=opt.id,
        audience="семьи с детьми",
        segment_ids=["SEG-001"],
        job_to_be_done="знать, что ребёнок добрался",
        direction="упростить настройку",
        features=[
            Feature(id="FEAT-001", name="Мастер настройки", claim_ids=[fact.id]),
            Feature(id="FEAT-002", name="Голосовой ассистент", status=FeatureStatus.PLANNED),
        ],
    )
    case.comms = CommsStrategy(
        audience="родители",
        segment_id="SEG-001",
        situation="первый запуск",
        barrier=BarrierType.UNDERSTANDING,
        key_message="Настройка за 3 минуты",
        promises=promises,
    )
    return case, fact


def test_supported_promise_passes():
    case, _ = _case_with_strategies(
        [
            Promise(
                id="PRM-001",
                text="Настройка за 3 минуты",
                location=PromiseLocation.KEY_MESSAGE,
                feature_ids=["FEAT-001"],
            )
        ]
    )
    findings = lint_promises(case)
    assert [f.status for f in findings] == [PromiseStatus.SUPPORTED]


def test_planted_promise_without_feature_is_caught():
    case, _ = _case_with_strategies(
        [Promise(id="PRM-001", text="Работает за 30 секунд", location=PromiseLocation.PRESS_RELEASE)]
    )
    findings = lint_promises(case)
    assert findings[0].status == PromiseStatus.UNSUPPORTED
    assert apply_findings(case, findings) == 1
    assert case.comms.promises[0].issue


def test_promise_on_planned_feature_only_is_unsupported():
    case, _ = _case_with_strategies(
        [
            Promise(
                id="PRM-001",
                text="Управляйте голосом",
                location=PromiseLocation.CHANNEL,
                feature_ids=["FEAT-002"],
            )
        ]
    )
    assert lint_promises(case)[0].status == PromiseStatus.UNSUPPORTED


def test_segment_mismatch_is_caught():
    case, _ = _case_with_strategies(
        [
            Promise(
                id="PRM-001",
                text="Для бизнеса",
                location=PromiseLocation.CHANNEL,
                segment_id="SEG-009",
                feature_ids=["FEAT-001"],
            )
        ]
    )
    assert lint_promises(case)[0].status == PromiseStatus.SEGMENT_MISMATCH


def test_synthetic_only_promise_is_caught():
    case, _ = _case_with_strategies([])
    synthetic = add_claim(case, "Персонам нравится функция", ClaimType.SYNTHETIC)
    case.comms.promises = [
        Promise(
            id="PRM-001",
            text="Всем понравится",
            location=PromiseLocation.KEY_MESSAGE,
            claim_ids=[synthetic.id],
        )
    ]
    assert lint_promises(case)[0].status == PromiseStatus.SYNTHETIC_ONLY


def _experiment(**kw):
    base = dict(
        id="EXP-001",
        hypothesis="Мы считаем, что новый онбординг поднимет M1 на 3 п.п.",
        metric="retention_m1",
        baseline=0.52,
        guardrails=[Guardrail(metric="жалобы", threshold="не выше 2%")],
        kill_criterion=KillCriterion(signal="M1 не вырос", deadline=date(2027, 2, 1)),
        decision_rule="Раскатываем, если +3 п.п. и guardrail в норме",
        observation_period_days=45,
        behavior_cycle_days=30,
    )
    base.update(kw)
    return Experiment(**base)


def test_complete_experiment_passes():
    assert experiment_ok(_experiment())


def test_experiment_without_baseline_or_step_is_rejected():
    codes = {i.code for i in lint_experiment(_experiment(baseline=None))}
    assert "no_baseline" in codes
    assert experiment_ok(_experiment(baseline=None, baseline_step="Выгрузить M1 за 3 месяца до запуска"))


def test_experiment_without_guardrail_or_kill_date_is_rejected():
    assert "no_guardrail" in {i.code for i in lint_experiment(_experiment(guardrails=[]))}
    codes = {i.code for i in lint_experiment(_experiment(kill_criterion=KillCriterion(signal="M1 не вырос")))}
    assert "kill_without_deadline" in codes


def test_planned_feature_is_not_rescued_by_claims_about_the_problem():
    """«Напоминание перед списанием» (в плане) + строка «жалобы на неожиданные списания» — всё равно блок."""
    case, fact = _case_with_strategies([])
    problem = grounded_fact(case, "Жалобы на неожиданные списания выросли вдвое")
    case.comms.promises = [
        Promise(
            id="PRM-001",
            text="Напомним перед списанием",
            location=PromiseLocation.CHANNEL,
            feature_ids=["FEAT-002"],
            claim_ids=[problem.id],
        ),
        Promise(
            id="PRM-002",
            text="Настройка за 3 минуты",
            location=PromiseLocation.KEY_MESSAGE,
            feature_ids=["FEAT-001", "FEAT-002"],
        ),
        Promise(
            id="PRM-003",
            text="Настройка занимает 3 минуты",
            location=PromiseLocation.PRESS_RELEASE,
            claim_ids=[fact.id],
        ),
    ]
    findings = {f.promise_id: f for f in lint_promises(case)}
    assert (
        findings["PRM-001"].status == PromiseStatus.UNSUPPORTED and "ещё нет" in findings["PRM-001"].message
    )
    assert findings["PRM-002"].status == PromiseStatus.SUPPORTED  # есть существующая функция
    assert findings["PRM-003"].status == PromiseStatus.SUPPORTED  # обещание-факт с подтверждённой строкой
