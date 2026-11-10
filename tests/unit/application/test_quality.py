"""Выключатели качества v0.2.0: флага нет в настройках — поведение v0.1.1."""

from __future__ import annotations

import pytest

from strategist.application.quality import FLAGS, enabled, switch_off


def test_missing_flag_means_v011_behaviour_and_unknown_flag_is_an_error():
    assert not enabled({}, "numbers_v2")
    assert not enabled({"quality": None}, "numbers_v2")
    assert enabled({"quality": {"numbers_v2": True}}, "numbers_v2")
    with pytest.raises(KeyError):
        enabled({"quality": {}}, "numbers_v3")


def test_switch_off_all_or_a_list():
    on = dict.fromkeys(FLAGS, True)
    assert switch_off(on, None) == on
    assert switch_off(on, "all") == dict.fromkeys(FLAGS, False)
    assert switch_off(on, "model_labels") == {**on, "model_labels": False}
    with pytest.raises(ValueError):
        switch_off(on, "all,model_labels,typo")


def test_ru_labels_flag_switches_report_enum_values_to_russian():
    from strategist.application.use_cases import report
    from strategist.domain.entities import FeatureStatus, NodeVerdict, SupportLabel

    assert report.ru(SupportLabel.SUPPORTED) == "подтверждено"  # вне отчёта подписи русские
    assert report.ru(FeatureStatus.PLANNED) == "в плане"
    assert report.ru(NodeVerdict.REVISED) == "пересмотрен"
    assert report.ru_node("objection") == "возражение"
    token = report._RU_LABELS.set(False)  # флаг выключен — значения перечислений, как в v0.1.1
    try:
        assert report.ru(SupportLabel.SUPPORTED) == "supported"
        assert report.ru_node("objection") == "objection"
    finally:
        report._RU_LABELS.reset(token)
