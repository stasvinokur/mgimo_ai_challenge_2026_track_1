"""Сверка «обещание = продукт»: структурное ограничение между продуктовой и коммуникационной стратегией.

Гейт срабатывает, если обещание:
- не подкреплено ни функцией продукта, ни подтверждённой строкой реестра (unsupported);
- говорит о функции, которой ещё нет (planned/hypothetical), — даже если к нему приложены подтверждённые строки:
  строка о проблеме («жалобы на неожиданные списания») не делает функцию («напоминание перед списанием»)
  существующей (unsupported);
- адресовано сегменту, которому продукт не продаётся (segment_mismatch);
- держится только на синтетике (synthetic_only).
"""

from __future__ import annotations

from strategist.domain.common import Entity
from strategist.domain.entities.case import DecisionCase
from strategist.domain.entities.claims import ClaimType
from strategist.domain.entities.strategy import FeatureStatus, Promise, PromiseStatus


class PromiseFinding(Entity):
    promise_id: str
    status: PromiseStatus
    message: str


def _claim_is_synthetic(case: DecisionCase, claim_id: str) -> bool:
    claim = case.claim(claim_id)
    if claim is None:
        return False
    if claim.type == ClaimType.SYNTHETIC:
        return True
    flags = []
    for ev_id in claim.evidence_ids:
        ev = case.evidence_item(ev_id)
        src = case.source(ev.source_id) if ev else None
        if src is not None:
            flags.append(src.is_model_respondents)
    return bool(flags) and all(flags)


def check_promise(promise: Promise, case: DecisionCase) -> PromiseFinding:
    product = case.product
    if product is None:
        return PromiseFinding(
            promise_id=promise.id, status=PromiseStatus.UNSUPPORTED, message="Нет продуктовой стратегии"
        )

    features = {f.id: f for f in product.features}
    linked_features = [features[f] for f in promise.feature_ids if f in features]
    existing_features = [f for f in linked_features if f.status == FeatureStatus.EXISTING]
    planned_features = [f for f in linked_features if f.status != FeatureStatus.EXISTING]

    grounded_claims = []
    for cid in promise.claim_ids:
        claim = case.claim(cid)
        if claim is not None and claim.is_active and claim.is_grounded:
            grounded_claims.append(cid)
    support_ids = [*grounded_claims]
    for feature in linked_features:
        support_ids += [
            cid
            for cid in feature.claim_ids
            if (c := case.claim(cid)) is not None and c.is_active and c.is_grounded
        ]

    segments = set(product.segment_ids)
    if promise.segment_id and segments and promise.segment_id not in segments:
        return PromiseFinding(
            promise_id=promise.id,
            status=PromiseStatus.SEGMENT_MISMATCH,
            message=f"Обещание адресовано сегменту {promise.segment_id}, а продукт делается для {sorted(segments)}",
        )

    all_refs = [*promise.claim_ids, *(cid for f in linked_features for cid in f.claim_ids)]
    if all_refs and all(_claim_is_synthetic(case, cid) for cid in all_refs) and not existing_features:
        return PromiseFinding(
            promise_id=promise.id,
            status=PromiseStatus.SYNTHETIC_ONLY,
            message="Обещание держится только на синтетике (модельные респонденты или сгенерированные данные)",
        )

    if linked_features and not existing_features:
        return PromiseFinding(
            promise_id=promise.id,
            status=PromiseStatus.UNSUPPORTED,
            message=(
                f"Обещание о функции, которой ещё нет: {', '.join(f.id for f in planned_features)} "
                "(в плане или идея). Строки реестра о проблеме функцию не создают — обещание переносится "
                "в план запуска с датой"
            ),
        )

    if not existing_features and not support_ids:
        detail = (
            f"функции {', '.join(f.id for f in planned_features)} ещё не существуют"
            if planned_features
            else "нет соответствующей функции продукта и подтверждённой строки реестра"
        )
        return PromiseFinding(
            promise_id=promise.id,
            status=PromiseStatus.UNSUPPORTED,
            message=f"Неподкреплённое обещание: {detail}",
        )

    return PromiseFinding(promise_id=promise.id, status=PromiseStatus.SUPPORTED, message="Подкреплено")


def lint_promises(case: DecisionCase) -> list[PromiseFinding]:
    if case.comms is None:
        return []
    findings = [check_promise(p, case) for p in case.comms.promises]
    product = case.product
    if (
        product
        and case.comms.segment_id
        and product.segment_ids
        and case.comms.segment_id not in product.segment_ids
    ):
        findings.append(
            PromiseFinding(
                promise_id="comms",
                status=PromiseStatus.SEGMENT_MISMATCH,
                message=(
                    f"Коммуникация говорит с сегментом {case.comms.segment_id}, "
                    f"а продукт делается для {product.segment_ids}"
                ),
            )
        )
    return findings


def apply_findings(case: DecisionCase, findings: list[PromiseFinding]) -> int:
    """Записывает статусы в обещания; возвращает число заблокированных."""
    if case.comms is None:
        return 0
    by_id = {f.promise_id: f for f in findings}
    blocked = 0
    for promise in case.comms.promises:
        finding = by_id.get(promise.id)
        if finding is None:
            continue
        promise.status = finding.status
        promise.issue = "" if finding.status == PromiseStatus.SUPPORTED else finding.message
        blocked += finding.status != PromiseStatus.SUPPORTED
    blocked += sum(1 for f in findings if f.promise_id == "comms")
    return blocked
