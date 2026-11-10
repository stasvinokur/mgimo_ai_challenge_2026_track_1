"""Контракты структурированного вывода моделей (JSON Schema для каждого шага).

Внутри одного ответа модель ссылается на объекты по ключам (key); сценарий переводит ключи в ID кейса.
Описания полей — на русском: они попадают в схему и подсказывают модели, что ждём.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# --- Постановка задачи --------------------------------------------------------


class InterpretationDTO(BaseModel):
    interpretation: str = Field(description="Одно возможное толкование запроса")
    changes_recommendation: bool = Field(description="Меняет ли выбор этого толкования рекомендацию")
    why: str = ""


class ClarifyingQuestionDTO(BaseModel):
    question: str
    why: str = Field(description="Почему ответ важен для следующего шага")
    interpretations: list[str] = Field(default_factory=list)


class FrameDTO(BaseModel):
    question: str = Field(description="Вопрос решения своими словами")
    neutral_question: str = Field(description="Тот же вопрос без встроенной посылки")
    goal: str = Field(description="Цель: какой результат нужен руководителю")
    recipient: str = Field(description="Кто получатель результата")
    decision_owner: str = Field(default="", description="Кто принимает решение")
    deadline: str | None = Field(default=None, description="Срок решения ГГГГ-ММ-ДД или null")
    constraints: list[str] = Field(default_factory=list)
    available_data: list[str] = Field(default_factory=list)
    premise: str = Field(default="", description="Готовая посылка, встроенная в запрос, или пустая строка")
    competing_frames: list[str] = Field(description="Хотя бы одна конкурирующая рамка ситуации")
    interpretations: list[InterpretationDTO] = Field(default_factory=list)
    clarifying_questions: list[ClarifyingQuestionDTO] = Field(
        default_factory=list,
        description="Не больше трёх; только если толкования расходятся и меняют рекомендацию",
    )


class PremiseDTO(BaseModel):
    verdict: str = Field(description="confirmed | partial | not_confirmed | untested")
    reasoning: str = Field(description="Обоснование со ссылками [CL-…]")
    claim_ids: list[str] = Field(default_factory=list)


# --- План исследования (ценность информации) -----------------------------------


class LevelDTO(BaseModel):
    name: str = Field(description="low | base | high")
    description: str = ""
    probability: float = Field(ge=0, le=1, description="Вербальная вероятность, переведённая в число")


class UncertaintyDTO(BaseModel):
    key: str = Field(description="Короткий ключ, например U1")
    name: str
    description: str = ""
    levels: list[LevelDTO] = Field(description="2–3 уровня с вероятностями")
    base_level: str = "base"
    research_question: str = Field(description="Вопрос исследования, который снимет неопределённость")
    resolution_method: str = Field(default="", description="Как узнать: какие данные, источник, расчёт")
    estimated_tokens: int = Field(default=20000, description="Грубая оценка токенов на исследование")


class CandidateOptionDTO(BaseModel):
    key: str = Field(description="Ключ варианта, например A")
    kind: str = Field(description="status_quo | kill | pivot | invest | focus | expand | other")
    title: str


class UtilityRowDTO(BaseModel):
    option_key: str
    base_utility: float = Field(description="Полезность 0–100 в базовом сценарии")
    effects: dict[str, dict[str, float]] = Field(
        default_factory=dict, description="{ключ неопределённости: {уровень: прибавка к полезности}}"
    )


class SkippedQuestionDTO(BaseModel):
    question: str
    why_not_needed: str


class ResearchPlanDTO(BaseModel):
    candidate_options: list[CandidateOptionDTO]
    uncertainties: list[UncertaintyDTO]
    utilities: list[UtilityRowDTO]
    nice_to_know: list[SkippedQuestionDTO] = Field(default_factory=list)


# --- Сбор материалов -----------------------------------------------------------


class QuoteDTO(BaseModel):
    fragment: int = Field(description="Номер фрагмента [F…], из которого взята цитата")
    quote: str = Field(description="Дословная подстрока фрагмента, без пересказа")
    topic: str = Field(default="", description="К какому вопросу исследования относится")


class EvidenceExtractionDTO(BaseModel):
    quotes: list[QuoteDTO] = Field(default_factory=list)
    instructions_detected: list[str] = Field(
        default_factory=list, description="Дословные команды/инструкции, найденные внутри материала"
    )


class MetricFilterDTO(BaseModel):
    column: str
    op: str = Field(description="== | != | > | >= | < | <= | in")
    value: str | float | int | list[str] | list[float]


class MetricProposalDTO(BaseModel):
    name: str = "metric"
    description: str = ""
    agg: str = Field(description="mean | sum | count | median | min | max | ratio | share | sum_share")
    column: str | None = None
    denominator_column: str | None = None
    filters: list[MetricFilterDTO] = Field(default_factory=list)
    group_by: str | None = None
    unit: str | None = None
    segment_column: str | None = Field(
        default=None, description="Крупный сегмент для проверки парадокса Симпсона"
    )


class MetricPlanDTO(BaseModel):
    metrics: list[MetricProposalDTO] = Field(default_factory=list)


class ResearchActionDTO(BaseModel):
    action: str = Field(description="compute_metric | fetch_url | search_knowledge | finish")
    reason: str
    question_id: str | None = None
    table_source_id: str | None = None
    metric: MetricProposalDTO | None = None
    url: str | None = None
    query: str | None = None


# --- Реестр утверждений --------------------------------------------------------


class ClaimDTO(BaseModel):
    key: str = Field(description="Ключ утверждения в ответе, например C1")
    text: str = Field(description="Атомарное утверждение, написанное из цитаты")
    type: str = Field(description="fact | hypothesis | forecast | assumption | unknown")
    evidence_ids: list[str] = Field(default_factory=list, description="ID цитат EV-…, на которых держится")
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    topic: str = ""
    probability: float | None = Field(default=None, description="Только для прогноза")
    horizon: str | None = Field(default=None, description="Дата разрешения прогноза ГГГГ-ММ-ДД")
    resolution_criterion: str | None = None
    falsification_test: str | None = Field(default=None, description="Для гипотезы: что её опровергнет")
    what_would_change: str | None = Field(default=None, description="Что изменило бы вывод")
    reason_unknown: str | None = Field(
        default=None, description="Для unknown: почему неизвестно и как узнать"
    )


class ClaimsDTO(BaseModel):
    claims: list[ClaimDTO]


class StrongVerifyDTO(BaseModel):
    label: str = Field(description="supported | partial | unsupported | contradicted")
    confidence: float = Field(ge=0, le=1)
    note: str = ""


# --- Варианты и стресс-тест ----------------------------------------------------


class KillCriterionDTO(BaseModel):
    signal: str
    threshold: str | None = None
    deadline: str | None = Field(default=None, description="ГГГГ-ММ-ДД")


class AssumptionDTO(BaseModel):
    text: str
    what_would_change: str = Field(description="Сигнал, который опроверг бы допущение")


class OptionDTO(BaseModel):
    key: str
    kind: str = Field(description="status_quo | kill | pivot | invest | focus | expand | other")
    title: str
    summary: str
    rationale: str = Field(description="Обоснование со ссылками [CL-…]")
    supporting_claim_ids: list[str] = Field(default_factory=list)
    assumptions: list[AssumptionDTO] = Field(default_factory=list)
    conscious_refusals: list[str] = Field(
        default_factory=list, description="От чего вариант сознательно отказывается"
    )
    kill_criterion: KillCriterionDTO
    costs: str = ""
    revenue: str = ""


class OptionsDTO(BaseModel):
    options: list[OptionDTO]


class ObjectionDTO(BaseModel):
    text: str
    claim_ids: list[str] = Field(
        default_factory=list, description="Опора на реестр; пусто — тогда это гипотеза"
    )
    is_hypothesis: bool = False


class PremortemDTO(BaseModel):
    reasons: list[ObjectionDTO] = Field(description="Пять причин провала через 12 месяцев")
    early_signals: list[str] = Field(default_factory=list, description="Ранние индикаторы провала")


class SteelmanDTO(BaseModel):
    competitor_option_id: str
    argument: str = Field(description="Сильнейший аргумент в пользу конкурирующего варианта")
    claim_ids: list[str] = Field(default_factory=list)
    is_hypothesis: bool = False


class RoleScoreDTO(BaseModel):
    option_label: str
    score: float = Field(ge=0, le=10)
    rationale: str = ""


class RolePanelDTO(BaseModel):
    scores: list[RoleScoreDTO]


class LevelUpdateDTO(BaseModel):
    uncertainty_id: str = Field(description="ID неопределённости, например U-001")
    observed_level: str = Field(description="Уровень, который показали новые данные — точно как в списке")
    evidence: list[str] = Field(
        default_factory=list, description="ID новых строк реестра CL-…, которые это показывают"
    )


class DecisionModelDTO(BaseModel):
    utilities: list[UtilityRowDTO] = Field(
        description="Полезность реальных вариантов (по ID OPT-…) и эффекты"
    )
    note: str = ""
    level_updates: list[LevelUpdateDTO] = Field(
        default_factory=list, description="Только при новых данных: какие неопределённости они разрешили"
    )


# --- Стратегии -----------------------------------------------------------------


class SegmentDTO(BaseModel):
    key: str
    name: str
    description: str = ""


class FeatureDTO(BaseModel):
    key: str
    name: str
    description: str = ""
    status: str = Field(description="existing | planned | hypothetical")
    claim_ids: list[str] = Field(default_factory=list)


class ActionStepDTO(BaseModel):
    when: str = Field(description="Дата ГГГГ-ММ-ДД или срок «до ГГГГ-ММ-ДД»")
    action: str = Field(description="Конкретное действие, которое можно поставить команде")
    owner: str = Field(default="", description="Роль ответственного")
    metric: str = Field(default="", description="Как поймём, что шаг сделан или дал результат")


class SegmentMessageDTO(BaseModel):
    segment_key: str | None = None
    message: str
    channel: str = ""
    timing: str = ""


class ProductStrategyDTO(BaseModel):
    audience: str
    segments: list[SegmentDTO]
    job_to_be_done: str
    alternatives: list[str]
    direction: str = Field(description="Выбранное направление со ссылками [CL-…]")
    conscious_refusals: list[str]
    value_logic: str = Field(description="Связь пользы с затратами и доходами")
    costs: str = ""
    revenue: str = ""
    features: list[FeatureDTO]
    metrics: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    action_plan: list[ActionStepDTO] = Field(
        default_factory=list, description="4–6 шагов на ближайший квартал"
    )


class PromiseDTO(BaseModel):
    key: str
    text: str
    location: str = Field(description="key_message | press_release | channel | segment_message")
    segment_key: str | None = None
    feature_keys: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)


class ChannelDTO(BaseModel):
    channel: str
    role: str = Field(description="awareness | explanation | conversion | retention")
    message: str = ""
    promise_keys: list[str] = Field(default_factory=list)


class CommsStrategyDTO(BaseModel):
    audience: str
    segment_key: str | None = None
    situation: str
    barrier: str = Field(description="understanding | choice | trust | habit_switching_cost")
    barrier_explanation: str
    key_message: str
    proof_claim_ids: list[str] = Field(default_factory=list)
    channels: list[ChannelDTO]
    press_release: str = Field(description="Пресс-релиз меньше страницы в духе Working Backwards")
    promises: list[PromiseDTO]
    segment_messages: list[SegmentMessageDTO] = Field(default_factory=list)
    timeline: list[ActionStepDTO] = Field(default_factory=list, description="3–6 шагов коммуникации с датами")


# --- План проверки и решение ---------------------------------------------------


class GuardrailDTO(BaseModel):
    metric: str
    threshold: str
    owner: str = ""


class ExperimentDTO(BaseModel):
    unknown_id: str | None = None
    assumption_claim_ids: list[str] = Field(default_factory=list)
    hypothesis: str = Field(description="«Мы считаем, что…»")
    metric: str
    baseline: float | None = None
    baseline_source: str | None = None
    baseline_step: str | None = Field(default=None, description="Если baseline неизвестен — как его получить")
    expected_change: str
    mde: str
    comparison: str = Field(description="С чем сравниваем: контроль, до/после, сегмент")
    observation_period_days: int
    behavior_cycle_days: int | None = None
    guardrails: list[GuardrailDTO]
    kill_criterion: KillCriterionDTO
    decision_rule: str
    owner: str = ""
    undesired_effects: list[str] = Field(default_factory=list)


class ExperimentsDTO(BaseModel):
    experiments: list[ExperimentDTO]


class ForecastDTO(BaseModel):
    probability: float = Field(ge=0, le=1)
    reasoning: str = ""


class DecisionRecordDTO(BaseModel):
    rationale: str = Field(description="Почему выбран вариант, со ссылками [CL-…]")
    confirmation: str = Field(description="Как проверим, что решение исполнено и работает")
    shaping_actions: list[str] = Field(
        default_factory=list, description="Действия, повышающие шансы допущений"
    )
    hedging_actions: list[str] = Field(
        default_factory=list, description="Страховка, если допущение не сработает"
    )
    commitments: list[str] = Field(default_factory=list)


# --- Пересмотр -----------------------------------------------------------------


class RelationDTO(BaseModel):
    new_claim_key: str
    existing_claim_id: str
    relation: str = Field(description="contradicts | supersedes | supports")
    explanation: str = ""


class RevisionClaimsDTO(BaseModel):
    claims: list[ClaimDTO]
    relations: list[RelationDTO] = Field(default_factory=list)


class NodeVerdictDTO(BaseModel):
    node_id: str
    verdict: str = Field(description="unchanged | revised | retracted")
    new_text: str = Field(default="", description="Новая формулировка, если revised")
    reason: str


class RevisionVerdictsDTO(BaseModel):
    verdicts: list[NodeVerdictDTO]


# --- Панель модельных респондентов (СИНТЕТИКА) --------------------------------------------


class PersonaDTO(BaseModel):
    key: str = ""
    name: str = Field(description="Вымышленное имя без персональных данных")
    profile: str = Field(description="2–3 предложения: кто это, какая задача, как решает её сейчас")
    segment: str = ""
    tech_attitude: str = Field(default="medium", description="low | medium | high")
    current_alternative: str = ""


class PanelPersonasDTO(BaseModel):
    personas: list[PersonaDTO] = Field(default_factory=list)


class ConceptReactionDTO(BaseModel):
    concept_label: str = Field(description="Как в списке: «Концепт A»")
    reaction: str = Field(description="2–3 предложения от первого лица с опорой на прошлый опыт персоны")
    concern: str = Field(default="", description="Главное, что остановит персону")
    score: float = Field(default=3.0, description="1–5: насколько концепт нужен персоне")


class FramingAgreementDTO(BaseModel):
    statement_key: str = Field(description="Ключ утверждения, например A+ или A-")
    agreement: float = Field(default=3.0, description="1 — совсем не согласен … 5 — полностью согласен")


class PanelResponseDTO(BaseModel):
    reactions: list[ConceptReactionDTO] = Field(default_factory=list)
    framing: list[FramingAgreementDTO] = Field(default_factory=list)


class PanelQuestionDTO(BaseModel):
    question: str
    concept_label: str = ""
    checks: str = Field(default="", description="Какое разногласие или гипотезу проверяет вопрос")


class PanelInterviewsDTO(BaseModel):
    questions: list[PanelQuestionDTO] = Field(default_factory=list)


class ExecutiveSummaryDTO(BaseModel):
    answer: str = Field(
        description="Прямой ответ на вопрос руководителя: 1–2 предложения, что делать и при каком условии"
    )
    reasons: list[str] = Field(default_factory=list, description="Ровно 3 довода, в каждом — число и [CL-…]")
    next_steps: list[ActionStepDTO] = Field(
        default_factory=list, description="3–5 шагов с датами и ответственными"
    )
    asks: list[str] = Field(
        default_factory=list, description="1–3 решения или ресурса, нужные от руководителя"
    )
    revisit: str = Field(default="", description="Когда и по какому сигналу пересматриваем решение")
    say_to_team: str = Field(default="", description="Одна фраза для команды")
