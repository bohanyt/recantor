from __future__ import annotations

import time
from uuid import UUID

from recantor.meeting_essence.budget import EngineLimits
from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CheckStatus,
    RecordRelation,
)


def monotonic_deadline(
    max_wall_clock_seconds: float | None, started_monotonic: float
) -> float | None:
    if max_wall_clock_seconds is None:
        return None
    return started_monotonic + max_wall_clock_seconds


def remaining_call_seconds(
    *,
    deadline: float | None,
    limits: EngineLimits,
) -> float | None:
    """None means the job deadline has already passed; do not start a call."""
    if deadline is None:
        return limits.max_provider_call_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(limits.max_provider_call_seconds, remaining)


def note_check_status(store: dict[str, CheckStatus], key: str, incoming: CheckStatus) -> None:
    """Record a page result. An explicit failure or not-started result stays sticky."""
    current = store.get(key)
    if current is None:
        store[key] = incoming
        return
    if current in {CheckStatus.FAILED, CheckStatus.NOT_STARTED}:
        return
    if incoming in {CheckStatus.FAILED, CheckStatus.NOT_STARTED}:
        store[key] = incoming
        return
    store[key] = merge_check_status(current, incoming)


def aggregate_check_status(keys: list[str], store: dict[str, CheckStatus]) -> CheckStatus:
    if not keys:
        return CheckStatus.COMPLETED
    folded = store.get(keys[0], CheckStatus.NOT_STARTED)
    for key in keys[1:]:
        folded = merge_check_status(folded, store.get(key, CheckStatus.NOT_STARTED))
    return folded


def combine_provider_and_annotation(
    provider_status: CheckStatus,
    annotation_status: CheckStatus | None,
) -> CheckStatus:
    """Fixture annotations can fail a completed provider check. They cannot create one."""
    if annotation_status is None:
        return provider_status
    if provider_status == CheckStatus.FAILED or annotation_status == CheckStatus.FAILED:
        return CheckStatus.FAILED
    if provider_status != CheckStatus.COMPLETED:
        return provider_status
    if annotation_status == CheckStatus.COMPLETED:
        return CheckStatus.COMPLETED
    return CheckStatus.IN_PROGRESS


def merge_check_status(current: CheckStatus, incoming: CheckStatus) -> CheckStatus:
    """Page merge. A later not-started page must not leave an earlier pass completed."""
    if current == CheckStatus.NOT_STARTED:
        return incoming
    if current == CheckStatus.FAILED or incoming == CheckStatus.FAILED:
        return CheckStatus.FAILED
    if current == CheckStatus.UNKNOWN or incoming == CheckStatus.UNKNOWN:
        return CheckStatus.UNKNOWN
    if incoming == CheckStatus.NOT_STARTED:
        if current == CheckStatus.COMPLETED:
            return CheckStatus.IN_PROGRESS
        return current
    if current == CheckStatus.IN_PROGRESS or incoming == CheckStatus.IN_PROGRESS:
        return CheckStatus.IN_PROGRESS
    if current == incoming:
        return current
    return CheckStatus.IN_PROGRESS


def resolve_semantic_status(
    provider_status: CheckStatus, annotation_status: CheckStatus
) -> CheckStatus:
    if provider_status == CheckStatus.FAILED or annotation_status == CheckStatus.FAILED:
        return CheckStatus.FAILED
    if provider_status in {CheckStatus.NOT_STARTED, CheckStatus.UNKNOWN, CheckStatus.IN_PROGRESS}:
        return provider_status
    if annotation_status == CheckStatus.COMPLETED and provider_status == CheckStatus.COMPLETED:
        return CheckStatus.COMPLETED
    if annotation_status == CheckStatus.NOT_STARTED:
        return CheckStatus.NOT_STARTED
    return annotation_status


def relations_touching(
    relations: list[RecordRelation],
    record_ids: set[str],
) -> list[RecordRelation]:
    return [
        relation
        for relation in relations
        if relation.from_record_id in record_ids or relation.to_record_id in record_ids
    ]


def page_records(records: list[AtomicRecord], page_ids: tuple[str, ...]) -> list[AtomicRecord]:
    id_set = set(page_ids)
    return [record for record in records if record.record_id in id_set]


def same_topic_dependencies(
    page: list[AtomicRecord],
    ledger: list[AtomicRecord],
) -> list[AtomicRecord]:
    page_ids = {record.record_id for record in page}
    topics = {record.topic for record in page}
    return [
        record for record in ledger if record.record_id not in page_ids and record.topic in topics
    ]


def track_inspected_segments(
    inspected: tuple[UUID, ...],
    fragments: tuple[object, ...],
) -> tuple[UUID, ...]:
    seen = set(inspected)
    ordered = list(inspected)
    for fragment in fragments:
        segment_id = getattr(fragment, "segment_id", None)
        if segment_id is None or segment_id in seen:
            continue
        seen.add(segment_id)
        ordered.append(segment_id)
    return tuple(ordered)


def relation_key(relation: RecordRelation) -> tuple[str, str, str]:
    return (relation.relation_type.value, relation.from_record_id, relation.to_record_id)
