"""Durable progress and publication invariants for the meeting ledger.

Semantic quality stays in the engine. These checks only refuse regression and
publication that is not backed by an accepted checkpoint, admitted receipts,
and the frozen manifest.
"""

from __future__ import annotations

from recantor.meeting_essence.check_binding import is_admitted_semantic_method
from recantor.meeting_essence.checkpoint_binding import CHECKPOINT_BINDING_VERSION
from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CheckStatus,
    LayeredMeetingResult,
    ProcessingJobStatus,
    RecordRelation,
)
from recantor.meeting_essence.progress import EngineCheckpoint

_MONOTONIC_CURSORS = (
    "extract_page_index",
    "extract_fragment_cursor",
    "reconcile_page_index",
    "check_page_index",
    "source_check_cursor",
    "relation_context_cursor",
    "render_record_cursor",
    "source_pages_sent",
    "processed_fragment_count",
    "expected_fragment_count",
)

_SOURCE_INNER_CURSORS = (
    "source_check_record_cursor",
    "source_check_claim_cursor",
    "source_check_relation_cursor",
)

_OUTPUT_INNER_CURSORS = (
    "output_check_record_cursor",
    "output_check_claim_cursor",
    "output_check_relation_cursor",
)

_STAGE_FLAGS = (
    "extract_complete",
    "reconcile_complete",
    "check_complete",
    "render_complete",
)

_STATUS_MAPS = (
    "relation_slice_forward",
    "relation_slice_backward",
    "source_fragment_forward",
    "source_fragment_backward",
    "source_record_forward",
    "source_record_backward",
    "output_claim_forward",
    "output_claim_backward",
)

_CLAIM_FIELDS = (
    "overview_bullets",
    "topics",
    "actions",
    "open_items",
    "atomic_records",
    "relations",
)


def checkpoint_accepts_success(checkpoint: EngineCheckpoint | None) -> bool:
    """Host-accepted completion: every stage, both directions, and an admitted receipt."""
    if checkpoint is None:
        return False
    return bool(
        checkpoint.binding_version == CHECKPOINT_BINDING_VERSION
        and checkpoint.snapshot_id
        and checkpoint.snapshot_content_hash
        and checkpoint.input_fingerprint
        and checkpoint.extract_complete
        and checkpoint.reconcile_complete
        and checkpoint.check_complete
        and checkpoint.render_complete
        and checkpoint.processing_status == ProcessingJobStatus.SUCCEEDED
        and checkpoint.forward_semantic_check_status == CheckStatus.COMPLETED
        and checkpoint.backward_semantic_check_status == CheckStatus.COMPLETED
        and is_admitted_semantic_method(checkpoint.semantic_check_method)
        and checkpoint.acked_check_fingerprints
        and not checkpoint.pending_check_fingerprints
        and not checkpoint.failed_check_fingerprints
        and not checkpoint.unprocessed_segment_ids
        and checkpoint.layered_partial is not None
    )


def checkpoint_regresses(previous: EngineCheckpoint, incoming: EngineCheckpoint) -> bool:
    """True when incoming drops accepted work.

    An outer source or output page advance may reset that page's inner cursors.
    A support-chunk advance may reset the output record, claim, and relation cursors.
    Other cursors and accepted identities do not move backwards.
    """
    for name in _STAGE_FLAGS:
        if getattr(previous, name) and not getattr(incoming, name):
            return True
    for name in _MONOTONIC_CURSORS:
        if getattr(incoming, name) < getattr(previous, name):
            return True
    if previous.source_check_cursor == incoming.source_check_cursor:
        for name in _SOURCE_INNER_CURSORS:
            if getattr(incoming, name) < getattr(previous, name):
                return True
    if incoming.output_check_cursor < previous.output_check_cursor:
        return True
    if incoming.output_check_cursor == previous.output_check_cursor:
        if incoming.output_support_cursor < previous.output_support_cursor:
            return True
        if incoming.output_support_cursor == previous.output_support_cursor:
            for name in _OUTPUT_INNER_CURSORS:
                if getattr(incoming, name) < getattr(previous, name):
                    return True
    if not set(previous.acked_check_fingerprints) <= set(incoming.acked_check_fingerprints):
        return True
    if not set(previous.completed_fragment_indices) <= set(incoming.completed_fragment_indices):
        return True
    if not set(previous.inspected_segment_ids) <= set(incoming.inspected_segment_ids):
        return True
    removed_failures = set(previous.failed_check_fingerprints) - set(
        incoming.failed_check_fingerprints
    )
    if not removed_failures <= set(incoming.acked_check_fingerprints):
        return True
    for name in _STATUS_MAPS:
        if not set(getattr(previous, name)) <= set(getattr(incoming, name)):
            return True
    if _records_regresses(previous.atomic_records, incoming.atomic_records):
        return True
    if _relations_regresses(previous.relations, incoming.relations):
        return True
    return _layered_claims_regresses(previous, incoming)


def records_match(left: list[AtomicRecord], right: list[AtomicRecord]) -> bool:
    if len(left) != len(right):
        return False
    left_map = {record.record_id: record.model_dump(mode="json") for record in left}
    right_map = {record.record_id: record.model_dump(mode="json") for record in right}
    return left_map == right_map and len(left_map) == len(left)


def relations_match(left: list[RecordRelation], right: list[RecordRelation]) -> bool:
    return _relation_keys(left) == _relation_keys(right)


def layered_claims_match(
    left: LayeredMeetingResult | None, right: LayeredMeetingResult | None
) -> bool:
    return _claim_body(left) == _claim_body(right)


def is_success_coverage(layered: LayeredMeetingResult) -> bool:
    coverage = layered.coverage
    return bool(
        coverage.snapshot_id == layered.snapshot_id
        and coverage.processing_job_status == ProcessingJobStatus.SUCCEEDED
        and coverage.forward_semantic_check_status == CheckStatus.COMPLETED
        and coverage.backward_semantic_check_status == CheckStatus.COMPLETED
        and coverage.structural_check_status == CheckStatus.COMPLETED
        and is_admitted_semantic_method(coverage.semantic_check_method)
    )


def partial_coverage_claims_success(
    layered: LayeredMeetingResult, processing_status: ProcessingJobStatus
) -> bool:
    """A partial artifact must not relabel itself as a succeeded publication.

    Completed semantic directions stay completed. Source, audio, and STT flags
    are independent of that check result.
    """
    coverage = layered.coverage
    if processing_status == ProcessingJobStatus.SUCCEEDED:
        return False
    if coverage.processing_job_status == ProcessingJobStatus.SUCCEEDED:
        return True
    return coverage.processing_job_status != processing_status


def _records_regresses(previous: list[AtomicRecord], incoming: list[AtomicRecord]) -> bool:
    incoming_by_id = {record.record_id: record for record in incoming}
    if len(incoming_by_id) != len(incoming):
        return True
    for record in previous:
        current = incoming_by_id.get(record.record_id)
        if current is None:
            return True
        if (
            current.claim_text != record.claim_text
            or current.kind != record.kind
            or current.content_status != record.content_status
        ):
            return True
        if not _evidence_keys(record) <= _evidence_keys(current):
            return True
    return False


def _relations_regresses(previous: list[RecordRelation], incoming: list[RecordRelation]) -> bool:
    return not _relation_keys(previous) <= _relation_keys(incoming)


def _layered_claims_regresses(previous: EngineCheckpoint, incoming: EngineCheckpoint) -> bool:
    if not previous.render_complete or previous.layered_partial is None:
        return False
    return _claim_body(previous.layered_partial) != _claim_body(incoming.layered_partial)


def _claim_body(layered: LayeredMeetingResult | None) -> dict[str, object] | None:
    if layered is None:
        return None
    payload = layered.model_dump(mode="json")
    return {name: payload.get(name) for name in ("snapshot_id", *_CLAIM_FIELDS)}


def _evidence_keys(record: AtomicRecord) -> set[tuple[object, ...]]:
    keys: set[tuple[object, ...]] = set()
    for evidence in record.evidence:
        span = None
        if evidence.text_span is not None:
            span = (evidence.text_span.start_cp, evidence.text_span.end_cp)
        keys.add((str(evidence.segment_id), evidence.start_ms, evidence.end_ms, span))
    return keys


def _relation_keys(relations: list[RecordRelation]) -> set[tuple[object, ...]]:
    keys: set[tuple[object, ...]] = set()
    for relation in relations:
        evidence = tuple(sorted(_evidence_keys_of_relation(relation)))
        keys.add(
            (
                relation.relation_type.value,
                relation.from_record_id,
                relation.to_record_id,
                evidence,
            )
        )
    return keys


def _evidence_keys_of_relation(relation: RecordRelation) -> set[tuple[object, ...]]:
    keys: set[tuple[object, ...]] = set()
    for evidence in relation.evidence:
        span = None
        if evidence.text_span is not None:
            span = (evidence.text_span.start_cp, evidence.text_span.end_cp)
        keys.add((str(evidence.segment_id), evidence.start_ms, evidence.end_ms, span))
    return keys
