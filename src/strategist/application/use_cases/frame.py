"""FrameDecision: постановка задачи, выбор плейбука, проверка посылки запроса.

Агент спрашивает только то, что меняет рекомендацию (CLAM, «Clarify When Necessary»): не больше трёх
вопросов и только если толкования расходятся. Посылка размытого запроса («удержание падает из-за
онбординга») сначала проверяется по данным — защита от поддакивания (SycEval: 58%).
"""

from __future__ import annotations

from strategist.application.context import AppContext
from strategist.application.digests import claims_digest, frame_digest, materials_overview, today_line
from strategist.application.dto.llm_outputs import FrameDTO, PremiseDTO
from strategist.application.llm_calls import ask_structured
from strategist.application.ports import DecisionKind, DecisionQuestion, GateKind, GateRequest
from strategist.application.utils import enum_or, known_ids, parse_date
from strategist.domain.entities import ClarifyingQuestion, DecisionCase, DecisionFrame, PremiseVerdict

PLAYBOOKS = {
    "retention_diagnosis": "Диагностика удержания: почему уходят пользователи, менять продукт или коммуникацию",
    "go_to_market": "Выход на рынок: кому и как продавать новый сервис",
    "communication": "Коммуникация ценности: что сказать аудитории, чтобы она поняла ценность",
    "portfolio_kill": "Судьба направления: развивать, заморозить или закрыть",
    "pricing": "Цена и упаковка: тарифы, пакеты, монетизация",
    "generic": "Другая продуктовая задача",
}


def choose_playbook(ctx: AppContext, case: DecisionCase) -> str:
    result = ctx.decision.decide(
        DecisionQuestion(
            id=f"{case.id}:playbook",
            kind=DecisionKind.CHOICE,
            instructions="Какой процесс подходит к запросу руководителя продукта?",
            state=case.brief,
            options=PLAYBOOKS,
            task="playbook",
            case_id=case.id,
            run_id=ctx.run_id,
            step="intake",
        )
    )
    playbook = result.choice if result.choice in PLAYBOOKS else "generic"
    ctx.emit(
        "route.playbook", case.id, playbook=playbook, confidence=result.confidence, backend=result.backend
    )
    return playbook


def frame_decision(ctx: AppContext, case: DecisionCase) -> DecisionFrame:
    playbook = choose_playbook(ctx, case)
    max_q = int(ctx.setting("max_clarifying_questions", 3))
    dto, _ = ask_structured(
        ctx,
        task="frame",
        prompt="frame",
        values={
            "brief": case.brief,
            "materials": materials_overview(case),
            "playbook": PLAYBOOKS[playbook],
            "today": today_line(ctx.clock.today()),
            "max_questions": max_q,
        },
        schema=FrameDTO,
        case_id=case.id,
        step="intake",
    )
    # Вопрос задаётся, только если толкования расходятся и это меняет рекомендацию.
    changing = [i for i in dto.interpretations if i.changes_recommendation]
    questions = dto.clarifying_questions[:max_q] if len(changing) >= 1 else []
    frame = DecisionFrame(
        question=dto.question,
        neutral_question=dto.neutral_question,
        goal=dto.goal,
        recipient=dto.recipient,
        decision_owner=dto.decision_owner,
        deadline=parse_date(dto.deadline),
        constraints=dto.constraints,
        available_data=dto.available_data,
        premise=dto.premise,
        competing_frames=dto.competing_frames
        or ["Ситуация стабильна, изменился состав аудитории или измерение"],
        clarifying_questions=[
            ClarifyingQuestion(question=q.question, why=q.why, interpretations=q.interpretations)
            for q in questions
        ],
        playbook=playbook,
    )
    case.frame = frame
    ctx.emit("frame.created", case.id, questions=len(frame.clarifying_questions), playbook=playbook)
    return frame


def ask_clarifications(ctx: AppContext, case: DecisionCase) -> int:
    """Шлюз уточнений: задаёт вопросы, только если они есть. Возвращает число ответов."""
    frame = case.frame
    if frame is None or not frame.clarifying_questions:
        return 0
    response = ctx.ask_gate(
        GateRequest(
            gate=GateKind.CLARIFY,
            case_id=case.id,
            title="Уточнение задачи",
            summary="Ответы меняют рекомендацию — поэтому спрашиваю до исследования.",
            questions=[q.question for q in frame.clarifying_questions],
            payload={"why": [q.why for q in frame.clarifying_questions]},
        )
    )
    answered = 0
    for q, answer in zip(frame.clarifying_questions, response.answers, strict=False):
        if answer and answer.strip():
            q.answer = answer.strip()
            answered += 1
    if response.comment:
        frame.constraints.append(f"Комментарий руководителя: {response.comment}")
    return answered


def check_premise(ctx: AppContext, case: DecisionCase) -> PremiseVerdict:
    """Вердикт по посылке запроса по данным реестра: подтверждена / частично / не подтверждена."""
    frame = case.frame
    if frame is None:
        return PremiseVerdict.UNTESTED
    if not frame.premise.strip():
        frame.premise_verdict = PremiseVerdict.UNTESTED
        frame.premise_reasoning = "В запросе нет встроенной посылки."
        return frame.premise_verdict
    dto, _ = ask_structured(
        ctx,
        task="frame",
        prompt="premise_check",
        values={"frame": frame_digest(case), "premise": frame.premise, "claims": claims_digest(case)},
        schema=PremiseDTO,
        case_id=case.id,
        step="situation",
    )
    frame.premise_verdict = enum_or(PremiseVerdict, dto.verdict, PremiseVerdict.UNTESTED)
    frame.premise_reasoning = dto.reasoning
    frame.premise_claim_ids = known_ids(dto.claim_ids, {c.id for c in case.claims})
    ctx.emit("premise.checked", case.id, verdict=frame.premise_verdict.value)
    return frame.premise_verdict
