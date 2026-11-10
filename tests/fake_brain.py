"""Фейковая модель для сквозных тестов: валидные ответы по каждому контракту шага.

Ответ выбирается по имени схемы (schema_name), ссылки — на реальные ID, найденные в тексте запроса.
"""

from __future__ import annotations

import json
import re
from typing import Any

from strategist.application.ports import LLMRequest, LLMResponse, LLMUsage


def _ids(text: str, prefix: str) -> list[str]:
    return list(dict.fromkeys(re.findall(rf"\b{prefix}-\d+\b", text)))


class FakeBrain:
    def __init__(self, clarify: bool = False, placebo_love: bool = False) -> None:
        self.clarify = clarify
        self.placebo_love = placebo_love  # панель «любит» плацебо → доверие к панели должно упасть
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        text = request.system + "\n" + "\n".join(m.content for m in request.messages)
        untrusted = "\n".join(b.text for b in request.untrusted)
        handler = getattr(self, f"_{request.schema_name}", None)
        if handler is None:
            raise AssertionError(f"FakeBrain: нет ответа для схемы {request.schema_name}")
        data = handler(text, untrusted)
        body = json.dumps(data, ensure_ascii=False)
        return LLMResponse(
            text=body,
            data=data,
            usage=LLMUsage(
                model="fake-brain", role="test", input_tokens=len(text) // 4, output_tokens=len(body) // 4
            ),
        )

    # --- постановка ------------------------------------------------------------------
    def _FrameDTO(self, text: str, _: str) -> dict[str, Any]:
        return {
            "question": "Что делать с падающим удержанием сервиса",
            "neutral_question": "Как изменилось удержание и что на него влияет",
            "goal": "Решение о развитии сервиса на квартал",
            "recipient": "Руководитель продукта",
            "decision_owner": "Руководитель продукта",
            "deadline": "2026-12-15",
            "constraints": ["Бюджет квартала фиксирован"],
            "available_data": ["когорты", "обзор рынка"],
            "premise": "Удержание падает из-за слабого онбординга",
            "competing_frames": ["Сменился состав привлечённых пользователей"],
            "interpretations": [
                {"interpretation": "Проблема в продукте", "changes_recommendation": True, "why": "—"}
            ],
            "clarifying_questions": (
                [
                    {
                        "question": "Важнее выручка или удержание?",
                        "why": "меняет вариант",
                        "interpretations": ["a", "b"],
                    }
                ]
                if self.clarify
                else []
            ),
        }

    def _PremiseDTO(self, text: str, _: str) -> dict[str, Any]:
        return {
            "verdict": "partial",
            "reasoning": "Данные частично подтверждают",
            "claim_ids": _ids(text, "CL")[:1],
        }

    def _ResearchPlanDTO(self, text: str, _: str) -> dict[str, Any]:
        levels = [
            {"name": "low", "probability": 0.3},
            {"name": "base", "probability": 0.5},
            {"name": "high", "probability": 0.2},
        ]
        return {
            "candidate_options": [
                {"key": "A", "kind": "status_quo", "title": "Оставить"},
                {"key": "B", "kind": "kill", "title": "Закрыть"},
                {"key": "C", "kind": "invest", "title": "Улучшить онбординг"},
                {"key": "D", "kind": "focus", "title": "Сфокусироваться на семьях"},
            ],
            "uncertainties": [
                {
                    "key": "U1",
                    "name": "Готовность платить",
                    "levels": levels,
                    "base_level": "base",
                    "research_question": "Сколько готовы платить?",
                    "resolution_method": "опрос",
                    "estimated_tokens": 20000,
                },
                {
                    "key": "U2",
                    "name": "Цвет кнопки",
                    "levels": levels,
                    "base_level": "base",
                    "research_question": "Влияет ли цвет кнопки?",
                    "resolution_method": "—",
                    "estimated_tokens": 30000,
                },
            ],
            "utilities": [
                {"option_key": "A", "base_utility": 50, "effects": {}},
                {"option_key": "B", "base_utility": 30, "effects": {}},
                {"option_key": "C", "base_utility": 60, "effects": {"U1": {"low": -25, "high": 10}}},
                {"option_key": "D", "base_utility": 45, "effects": {"U2": {"low": 1}}},
            ],
            "nice_to_know": [{"question": "Каков TAM рынка?", "why_not_needed": "не меняет выбор"}],
        }

    # --- сбор -------------------------------------------------------------------------
    def _EvidenceExtractionDTO(self, _: str, untrusted: str) -> dict[str, Any]:
        quotes, injections = [], []
        for line in untrusted.splitlines():
            m = re.match(r"\[F(\d+)\]\s+(.*)", line)
            if not m or len(m.group(2)) < 15:
                continue
            if "игнорируй" in m.group(2).lower():
                injections.append(m.group(2))
                continue
            quotes.append({"fragment": int(m.group(1)), "quote": m.group(2), "topic": "удержание"})
        return {"quotes": quotes[:4], "instructions_detected": injections}

    def _MetricPlanDTO(self, text: str, _: str) -> dict[str, Any]:
        return {
            "metrics": [
                {
                    "name": "m3",
                    "description": "Среднее удержание M3",
                    "agg": "mean",
                    "column": "m3",
                    "unit": "доля",
                }
            ]
        }

    def _ResearchActionDTO(self, text: str, _: str) -> dict[str, Any]:
        return {"action": "finish", "reason": "Данных достаточно для выбора"}

    # --- реестр -----------------------------------------------------------------------
    def _ClaimsDTO(self, text: str, _: str) -> dict[str, Any]:
        claims = []
        for i, m in enumerate(re.finditer(r"\[(EV-\d+)\] «([^»]+)»", text)):
            claims.append({"key": f"C{i}", "text": m.group(2), "type": "fact", "evidence_ids": [m.group(1)]})
        claims.append(
            {
                "key": "H",
                "text": "Причина оттока — сложная настройка",
                "type": "hypothesis",
                "falsification_test": "Если когорты с новым онбордингом не лучше — гипотеза отвергнута",
            }
        )
        claims.append(
            {
                "key": "U",
                "text": "Неизвестна готовность платить",
                "type": "unknown",
                "reason_unknown": "Нет опроса цены",
            }
        )
        return {"claims": claims}

    def _StrongVerifyDTO(self, text: str, _: str) -> dict[str, Any]:
        return {"label": "supported", "confidence": 0.9, "note": "подтверждено"}

    def __NLIAnswer(self, text: str, _: str) -> dict[str, Any]:
        return {"label": "entailment", "confidence": 0.92, "rationale": "совпадает"}

    # --- варианты ---------------------------------------------------------------------
    def _OptionsDTO(self, text: str, _: str) -> dict[str, Any]:
        cl = _ids(text, "CL")[:1]
        kinds = [
            ("status_quo", "Оставить как есть"),
            ("kill", "Закрыть направление"),
            ("invest", "Переделать онбординг"),
            ("focus", "Сфокусироваться на семьях"),
        ]
        return {
            "options": [
                {
                    "key": k,
                    "kind": kind,
                    "title": title,
                    "summary": f"{title}: подробности варианта {k} для руководителя",
                    "rationale": f"Обоснование {title} [{cl[0]}]" if cl else f"Обоснование {title}",
                    "supporting_claim_ids": cl,
                    "assumptions": [
                        {"text": f"Допущение варианта {title}", "what_would_change": "данные опроса"}
                    ],
                    "conscious_refusals": ["Не расширяем аудиторию"],
                    "kill_criterion": {
                        "signal": "M3 не растёт",
                        "threshold": "< 35%",
                        "deadline": "2027-02-01",
                    },
                    "costs": "2 спринта",
                    "revenue": "+5% выручки",
                }
                for k, (kind, title) in zip("ABCD", kinds, strict=True)
            ]
        }

    def _PremortemDTO(self, text: str, _: str) -> dict[str, Any]:
        cl = _ids(text, "CL")[:1]
        return {
            "reasons": [
                {"text": f"Причина провала {i}", "claim_ids": cl if i % 2 else [], "is_hypothesis": not i % 2}
                for i in range(5)
            ],
            "early_signals": ["падает активация"],
        }

    def _SteelmanDTO(self, text: str, _: str) -> dict[str, Any]:
        competitors = text.split("Конкуренты:")[-1]
        opt = (_ids(competitors, "OPT") or ["OPT-001"])[0]
        return {
            "competitor_option_id": opt,
            "argument": "Конкурент дешевле",
            "claim_ids": _ids(text, "CL")[:1],
        }

    def _RolePanelDTO(self, text: str, _: str) -> dict[str, Any]:
        labels = re.findall(r"(Вариант [A-Z]):", text)
        return {
            "scores": [
                {
                    "option_label": label,
                    "score": 8 if "Переделать" in text.split(label + ":")[1][:60] else 5,
                    "rationale": "оценка",
                }
                for label in labels
            ]
        }

    def _DecisionModelDTO(self, text: str, _: str) -> dict[str, Any]:
        opts = _ids(text, "OPT")
        rows = []
        for i, o in enumerate(opts):
            effects = {"U-001": {"low": -30, "high": 10}} if i == 2 else {}
            rows.append({"option_key": o, "base_utility": 60 if i == 2 else 40 + i, "effects": effects})
        return {"utilities": rows, "note": "решает готовность платить"}

    # --- стратегии --------------------------------------------------------------------
    def _ProductStrategyDTO(self, text: str, _: str) -> dict[str, Any]:
        cl = _ids(text, "CL")[:1]
        return {
            "audience": "Семьи с детьми",
            "segments": [{"key": "S1", "name": "Родители школьников"}],
            "job_to_be_done": "Знать, что ребёнок добрался",
            "alternatives": ["звонок", "ничего не делать"],
            "direction": f"Упростить онбординг [{cl[0]}]" if cl else "Упростить онбординг",
            "conscious_refusals": ["Не делаем B2B"],
            "value_logic": "Быстрая настройка повышает удержание и выручку",
            "features": [
                {
                    "key": "F1",
                    "name": "Мастер настройки",
                    "description": "3 шага",
                    "status": "existing",
                    "claim_ids": cl,
                },
                {"key": "F2", "name": "Голосовой помощник", "description": "идея", "status": "planned"},
            ],
            "metrics": ["Удержание M3"],
            "action_plan": [
                {
                    "when": "2026-11-20",
                    "action": "Упростить мастер настройки",
                    "owner": "продакт",
                    "metric": "M1",
                },
            ],
            "claim_ids": cl,
        }

    def _CommsStrategyDTO(self, text: str, _: str) -> dict[str, Any]:
        blocked = "заблокировала" in text
        promises = [
            {
                "key": "P1",
                "text": "Настройка за три шага",
                "location": "key_message",
                "segment_key": "SEG-001",
                "feature_keys": ["FEAT-001"],
            }
        ]
        if not blocked:
            promises.append(
                {
                    "key": "P2",
                    "text": "Управляйте голосом",
                    "location": "channel",
                    "segment_key": "SEG-001",
                    "feature_keys": ["FEAT-002"],
                }
            )
        return {
            "audience": "Родители",
            "segment_key": "SEG-001",
            "situation": "Первый день использования",
            "barrier": "understanding",
            "barrier_explanation": "Не понимают ценность",
            "key_message": "Спокойствие за ребёнка за три шага",
            "proof_claim_ids": _ids(text, "CL")[:1],
            "channels": [
                {"channel": "push", "role": "retention", "message": "Напоминание", "promise_keys": ["P1"]}
            ],
            "press_release": "Сервис стал проще",
            "segment_messages": [
                {"segment_key": "SEG-001", "message": "Настройка за три шага", "channel": "push"}
            ],
            "timeline": [
                {"when": "2026-12-01", "action": "Письмо новым пользователям", "owner": "маркетинг"}
            ],
            "promises": promises,
        }

    def _ExperimentsDTO(self, text: str, _: str) -> dict[str, Any]:
        unk = (_ids(text, "UNK") or [None])[0]
        return {
            "experiments": [
                {
                    "unknown_id": unk,
                    "hypothesis": "Мы считаем, что новый онбординг поднимет M1 на 3 п.п.",
                    "metric": "retention_m1",
                    "baseline": 0.52,
                    "expected_change": "+3 п.п.",
                    "mde": "2 п.п.",
                    "comparison": "A/B",
                    "observation_period_days": 45,
                    "behavior_cycle_days": 30,
                    "guardrails": [{"metric": "жалобы", "threshold": "не выше 2%", "owner": "поддержка"}],
                    "kill_criterion": {
                        "signal": "M1 не вырос",
                        "threshold": "< 1 п.п.",
                        "deadline": "2027-01-20",
                    },
                    "decision_rule": "Раскатываем при +2 п.п. и guardrail в норме",
                    "owner": "продукт",
                }
            ]
        }

    def _ForecastDTO(self, text: str, _: str) -> dict[str, Any]:
        return {"probability": 0.6, "reasoning": "базовая частота"}

    def _DecisionRecordDTO(self, text: str, _: str) -> dict[str, Any]:
        cl = _ids(text, "CL")[:1]
        return {
            "rationale": f"Выбран вариант с лучшей устойчивостью [{cl[0]}]" if cl else "Выбран вариант",
            "confirmation": "M1 через 45 дней",
            "shaping_actions": ["подготовить онбординг"],
            "hedging_actions": ["держать старый поток"],
            "commitments": ["выгрузить когорты"],
        }

    # --- выжимка для руководителя ----------------------------------------------------------
    def _ExecutiveSummaryDTO(self, text: str, _: str) -> dict[str, Any]:
        cl = _ids(text, "CL") or ["CL-001"]
        reopened = "Решение открыто заново" in text
        return {
            "answer": ("Решение открыто заново: " if reopened else "")
            + "Доводим новых пользователей до подключения (OPT-002) и проверяем эффект за квартал.",
            "reasons": [
                f"Удержание снизилось на 4 процентных пункта [{cl[0]}].",
                f"Пользователи называют сложную настройку причиной отказа [{cl[min(1, len(cl) - 1)]}].",
                f"Главный риск — эффект может не проявиться за квартал [{cl[-1]}].",
            ],
            "next_steps": [
                {"when": "2026-11-20", "action": "Упростить первичную настройку", "owner": "продакт"},
                {"when": "2026-12-01", "action": "Запустить A/B-тест онбординга", "owner": "аналитик"},
            ],
            "asks": ["Утвердить тест онбординга до 2026-11-15"],
            "revisit": "Если M1 не вырастет к 2027-01-31 — возвращаемся к выбору варианта.",
            "say_to_team": "Сначала доводим людей до ценности, потом продвигаем.",
        }

    # --- панель модельных респондентов (СИНТЕТИКА) --------------------------------------
    PERSONAS = (
        ("Анна", "high", 5.0),  # любит новинки
        ("Борис", "low", 1.0),  # скептик
        ("Вера", "low", 3.0),  # экономит
    )

    def _PanelPersonasDTO(self, text: str, _: str) -> dict[str, Any]:
        return {
            "personas": [
                {
                    "key": name,
                    "name": name,
                    "profile": f"{name}: родитель, решает задачу вручную",
                    "tech_attitude": tech,
                }
                for name, tech, _ in self.PERSONAS
            ]
        }

    def _PanelResponseDTO(self, text: str, _: str) -> dict[str, Any]:
        name, _, score = next((p for p in self.PERSONAS if p[0] in text), self.PERSONAS[-1])
        flat = text.replace("\\n", "\n")
        labels = sorted(set(re.findall(r"Концепт ([A-Z]):", flat)))
        placebo = {m for m in re.findall(r"Концепт ([A-Z]): Тот же сервис", flat)}
        reactions, framing = [], []
        for letter in labels:
            s = (5.0 if self.placebo_love else 1.0) if letter in placebo else score
            reactions.append(
                {
                    "concept_label": f"Концепт {letter}",
                    "reaction": f"{name}: {'пригодится' if s >= 3.5 else 'не нужно'}",
                    "concern": "" if s >= 3.5 else f"{name} не видит пользы",
                    "score": s,
                }
            )
            framing += [
                {"statement_key": f"{letter}+", "agreement": s},
                {"statement_key": f"{letter}-", "agreement": 6.0 - s},
            ]
        return {"reactions": reactions, "framing": framing}

    def _PanelInterviewsDTO(self, text: str, _: str) -> dict[str, Any]:
        return {
            "questions": [
                {
                    "question": "Расскажите, когда вы в последний раз настраивали похожий сервис.",
                    "checks": "барьер",
                },
                {"question": "Купили бы вы такую подписку?", "checks": "спрос"},
            ]
        }

    # --- пересмотр --------------------------------------------------------------------
    def _RevisionClaimsDTO(self, text: str, _: str) -> dict[str, Any]:
        ev = re.findall(r"\[(EV-\d+)\] «([^»]+)»", text.split("Новые цитаты:")[-1].split("Новые расчёты")[0])
        claims = [
            {"key": f"N{i}", "text": q, "type": "fact", "evidence_ids": [e]} for i, (e, q) in enumerate(ev)
        ]
        existing = _ids(text.split("Прежний реестр:")[-1], "CL")
        relations = (
            [
                {
                    "new_claim_key": "N0",
                    "existing_claim_id": existing[0],
                    "relation": "contradicts",
                    "explanation": "новые данные",
                }
            ]
            if claims and existing
            else []
        )
        return {"claims": claims, "relations": relations}

    def _RevisionVerdictsDTO(self, text: str, _: str) -> dict[str, Any]:
        nodes = re.findall(r"^(\S+) \((\w+)\):", text.split("Узлы под подозрением:")[-1], re.MULTILINE)
        verdicts = []
        for i, (node_id, _kind) in enumerate(nodes):
            verdict = "revised" if i == 0 else "unchanged"
            verdicts.append(
                {
                    "node_id": node_id,
                    "verdict": verdict,
                    "new_text": "Обновлено по новым данным" if i == 0 else "",
                    "reason": "новые данные",
                }
            )
        return {"verdicts": verdicts}
