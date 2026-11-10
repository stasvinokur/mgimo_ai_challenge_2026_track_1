from strategist.domain.entities import DecisionMatrix, RoleScore, ScoreOrder, Uncertainty, UncertaintyLevel
from strategist.domain.services.decision_analysis import assess_factors, stress_test, winner
from strategist.domain.services.ranking import aggregate_panel


def _matrix():
    levels = [
        UncertaintyLevel(name="low", probability=0.3),
        UncertaintyLevel(name="base", probability=0.5),
        UncertaintyLevel(name="high", probability=0.2),
    ]
    return DecisionMatrix(
        uncertainties=[
            Uncertainty(id="U-001", name="Готовность платить", levels=levels, base_level="base"),
            Uncertainty(id="U-002", name="Цвет кнопки", levels=levels, base_level="base"),
        ],
        base_utility={"OPT-001": 50, "OPT-002": 60, "OPT-003": 40},
        effects={
            "OPT-002": {"U-001": {"low": -25, "high": 10}},
            "OPT-001": {"U-002": {"low": 1, "high": -1}},
        },
    )


def test_winner_and_flip_detection():
    m = _matrix()
    assert winner(m, {"U-001": "base", "U-002": "base"})[0] == "OPT-002"
    factors = {f.uncertainty_id: f for f in assess_factors(m)}
    assert factors["U-001"].flips_winner is True
    assert factors["U-001"].winners_by_level["low"] == "OPT-001"
    assert factors["U-002"].flips_winner is False
    assert factors["U-001"].evpi > factors["U-002"].evpi


def test_stress_test_counts_survival_and_boundary():
    result = stress_test(_matrix())
    assert result.base_winner == "OPT-002"
    assert result.scenarios_total == 9
    assert result.scenarios_survived == 6
    assert result.critical_uncertainty_ids == ["U-001"]
    assert "Готовность платить" in result.boundaries[0].statement
    assert 0 < result.survival_probability < 1


def test_panel_detects_order_sensitivity_and_votes():
    stable = [
        RoleScore(role=r, option_id=o, score=s, order=order)
        for r in ("CFO", "CMO")
        for order in (ScoreOrder.FORWARD, ScoreOrder.REVERSE)
        for o, s in (("OPT-001", 7), ("OPT-002", 4))
    ]
    result = aggregate_panel(stable)
    assert result.winner == "OPT-001" and not result.order_sensitive
    flipping = [
        RoleScore(role="CFO", option_id="OPT-001", score=8, order=ScoreOrder.FORWARD),
        RoleScore(role="CFO", option_id="OPT-002", score=5, order=ScoreOrder.FORWARD),
        RoleScore(role="CFO", option_id="OPT-001", score=4, order=ScoreOrder.REVERSE),
        RoleScore(role="CFO", option_id="OPT-002", score=7, order=ScoreOrder.REVERSE),
    ]
    assert aggregate_panel(flipping).order_sensitive is True
