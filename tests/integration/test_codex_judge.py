"""Третий судья стенда: полные документы без обрезки и направление ошибок в доводах судей."""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parents[2] / "evals"
sys.path.insert(0, str(EVALS))
sys.path.insert(0, str(EVALS / "runners"))
import judge  # noqa: E402
from codex_judge import error_direction  # noqa: E402


def test_normalize_without_limit_keeps_the_whole_document_but_drops_service_sections():
    body = "# Отчёт\n\n## Варианты\n" + "строка варианта\n" * 3000
    text = body + "\n## Приложение: реестр утверждений\nCL-001 …\n"
    full = judge.normalize(text, None)
    assert full == body
    assert len(judge.normalize(text)) <= judge.LIMIT + 100  # по умолчанию — прежний бюджет длины


def test_error_direction_counts_who_the_refuted_claim_favoured(tmp_path):
    claims = [
        {"status": "refuted", "claim": "В X нет варианта «закрыть»."},  # занижает чат → в пользу агента
        {"status": "refuted", "claim": "Y задаёт MDE экспериментов."},  # завышает агента → в пользу агента
        {"status": "refuted", "claim": "X ограничивается только списком метрик."},  # в пользу агента
        {
            "status": "refuted",
            "claim": "Проценты X воспроизводятся по данным.",
        },  # завышает чат → в пользу чата
        {"status": "confirmed", "claim": "В X нет плана проверки."},  # подтверждённые не считаются
        {"status": "refuted", "claim": "В обоих документах есть ответственные."},
    ]
    path = tmp_path / "audit.json"
    path.write_text(json.dumps({"answer": {"claims": claims}}, ensure_ascii=False), encoding="utf-8")
    assert error_direction([path]) == {"агент": 3, "чат": 1, "неясно": 1}
