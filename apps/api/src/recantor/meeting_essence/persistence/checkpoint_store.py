from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from recantor.meeting_essence.checkpoint_binding import (
    CheckpointBinding,
    assert_checkpoint_compatible,
)
from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CheckStatus,
    LayeredMeetingResult,
    ProcessingJobStatus,
    RecordRelation,
    StageOperation,
)
from recantor.meeting_essence.progress import EngineCheckpoint, StageCallTrace
from recantor.meeting_essence.wire_payload import WirePayloadAudit

CHECKPOINT_SERIALIZATION_VERSION = "meeting-intelligence-checkpoint.v1"


class CheckpointSerializationError(ValueError):
    pass


_TRACE_KEYS = frozenset(
    {
        "operation",
        "started_at",
        "finished_at",
        "wire_audit",
        "requested_model",
        "observed_model",
        "error",
        "error_category",
        "attempt_count",
        "attempts",
        "serialized_utf8_bytes",
    }
)

_CHECKPOINT_KEYS = frozenset(
    {
        "serialization_version",
        "checkpoint_id",
        "binding_version",
        "snapshot_id",
        "snapshot_content_hash",
        "prompt_cache_key",
        "schema_identity",
        "algorithm_identity",
        "input_fingerprint",
        "session_id",
        "requested_model",
        "requested_reasoning_effort",
        "limits_fingerprint",
        "stage",
        "extract_page_index",
        "extract_fragment_cursor",
        "extract_complete",
        "reconcile_complete",
        "check_complete",
        "render_complete",
        "completed_fragment_indices",
        "inspected_segment_ids",
        "pending_extract_segment_indices",
        "atomic_records",
        "relations",
        "reconcile_page_index",
        "check_page_index",
        "source_check_cursor",
        "source_check_record_cursor",
        "source_check_claim_cursor",
        "source_check_relation_cursor",
        "output_check_cursor",
        "output_support_cursor",
        "output_check_record_cursor",
        "output_check_claim_cursor",
        "output_check_relation_cursor",
        "relation_context_cursor",
        "relation_slice_forward",
        "relation_slice_backward",
        "pending_check_fingerprints",
        "acked_check_fingerprints",
        "failed_check_fingerprints",
        "source_fragment_forward",
        "source_fragment_backward",
        "source_record_forward",
        "source_record_backward",
        "output_claim_forward",
        "output_claim_backward",
        "render_record_cursor",
        "source_pages_sent",
        "layered_partial",
        "forward_semantic_check_status",
        "backward_semantic_check_status",
        "semantic_check_method",
        "processing_status",
        "unprocessed_segment_ids",
        "stage_traces",
        "last_error_category",
        "last_error_message",
        "expected_fragment_count",
        "processed_fragment_count",
        "updated_at",
    }
)


def _reject_unknown(raw: dict[str, Any], allowed: frozenset[str], *, label: str) -> None:
    extra = set(raw) - allowed
    missing = allowed - set(raw)
    if extra or missing:
        msg = f"{label} keys mismatch extra={sorted(extra)} missing={sorted(missing)}"
        raise CheckpointSerializationError(msg)


def _require_int(raw: dict[str, Any], key: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{key} must be an integer"
        raise CheckpointSerializationError(msg)
    return value


def _require_bool(raw: dict[str, Any], key: str) -> bool:
    value = raw[key]
    if not isinstance(value, bool):
        msg = f"{key} must be a boolean"
        raise CheckpointSerializationError(msg)
    return value


def _require_str_list(raw: dict[str, Any], key: str) -> list[str]:
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{key} must be a list of strings"
        raise CheckpointSerializationError(msg)
    return value


def _require_int_list(raw: dict[str, Any], key: str) -> list[int]:
    value = raw[key]
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        msg = f"{key} must be a list of integers"
        raise CheckpointSerializationError(msg)
    return value


def _check_status_map(raw: object, *, key: str) -> dict[str, CheckStatus]:
    if not isinstance(raw, dict):
        msg = f"{key} must be an object"
        raise CheckpointSerializationError(msg)
    parsed: dict[str, CheckStatus] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            msg = f"{key} entries must be strings"
            raise CheckpointSerializationError(msg)
        try:
            parsed[name] = CheckStatus(value)
        except ValueError as exc:
            msg = f"{key} has an unknown check status"
            raise CheckpointSerializationError(msg) from exc
    return parsed


def _serialize_stage_trace(trace: StageCallTrace) -> dict[str, Any]:
    return {
        "operation": trace.operation.value,
        "started_at": trace.started_at.isoformat(),
        "finished_at": trace.finished_at.isoformat() if trace.finished_at else None,
        "wire_audit": trace.wire_audit.model_dump(mode="json") if trace.wire_audit else None,
        "requested_model": trace.requested_model,
        "observed_model": trace.observed_model,
        "error": trace.error,
        "error_category": trace.error_category,
        "attempt_count": trace.attempt_count,
        "attempts": trace.attempts,
        "serialized_utf8_bytes": trace.serialized_utf8_bytes,
    }


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw[key]
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{key} must be a string or null"
        raise CheckpointSerializationError(msg)
    return value


def _deserialize_stage_trace(raw: object) -> StageCallTrace:
    if not isinstance(raw, dict):
        msg = "stage trace must be an object"
        raise CheckpointSerializationError(msg)
    _reject_unknown(raw, _TRACE_KEYS, label="stage trace")
    wire = raw["wire_audit"]
    finished = raw["finished_at"]
    attempts = raw["attempts"]
    if not isinstance(attempts, list):
        msg = "stage trace attempts must be a list"
        raise CheckpointSerializationError(msg)
    byte_length = raw["serialized_utf8_bytes"]
    if byte_length is not None and (
        isinstance(byte_length, bool) or not isinstance(byte_length, int)
    ):
        msg = "serialized_utf8_bytes must be an integer or null"
        raise CheckpointSerializationError(msg)
    try:
        operation = StageOperation(raw["operation"])
    except ValueError as exc:
        msg = "stage trace operation is unknown"
        raise CheckpointSerializationError(msg) from exc
    return StageCallTrace(
        operation=operation,
        started_at=datetime.fromisoformat(raw["started_at"]),
        finished_at=datetime.fromisoformat(finished) if finished else None,
        wire_audit=WirePayloadAudit.model_validate(wire) if wire else None,
        requested_model=_optional_str(raw, "requested_model"),
        observed_model=_optional_str(raw, "observed_model"),
        error=_optional_str(raw, "error"),
        error_category=_optional_str(raw, "error_category"),
        attempt_count=_require_int(raw, "attempt_count"),
        attempts=list(attempts),
        serialized_utf8_bytes=byte_length,
    )


def serialize_checkpoint(checkpoint: EngineCheckpoint) -> str:
    payload: dict[str, Any] = {
        "serialization_version": CHECKPOINT_SERIALIZATION_VERSION,
        "checkpoint_id": str(checkpoint.checkpoint_id),
        "binding_version": checkpoint.binding_version,
        "snapshot_id": checkpoint.snapshot_id,
        "snapshot_content_hash": checkpoint.snapshot_content_hash,
        "prompt_cache_key": checkpoint.prompt_cache_key,
        "schema_identity": checkpoint.schema_identity,
        "algorithm_identity": checkpoint.algorithm_identity,
        "input_fingerprint": checkpoint.input_fingerprint,
        "session_id": checkpoint.session_id,
        "requested_model": checkpoint.requested_model,
        "requested_reasoning_effort": checkpoint.requested_reasoning_effort,
        "limits_fingerprint": checkpoint.limits_fingerprint,
        "stage": checkpoint.stage.value,
        "extract_page_index": checkpoint.extract_page_index,
        "extract_fragment_cursor": checkpoint.extract_fragment_cursor,
        "extract_complete": checkpoint.extract_complete,
        "reconcile_complete": checkpoint.reconcile_complete,
        "check_complete": checkpoint.check_complete,
        "render_complete": checkpoint.render_complete,
        "completed_fragment_indices": list(checkpoint.completed_fragment_indices),
        "inspected_segment_ids": [str(item) for item in checkpoint.inspected_segment_ids],
        "pending_extract_segment_indices": list(checkpoint.pending_extract_segment_indices),
        "atomic_records": [item.model_dump(mode="json") for item in checkpoint.atomic_records],
        "relations": [item.model_dump(mode="json") for item in checkpoint.relations],
        "reconcile_page_index": checkpoint.reconcile_page_index,
        "check_page_index": checkpoint.check_page_index,
        "source_check_cursor": checkpoint.source_check_cursor,
        "source_check_record_cursor": checkpoint.source_check_record_cursor,
        "source_check_claim_cursor": checkpoint.source_check_claim_cursor,
        "source_check_relation_cursor": checkpoint.source_check_relation_cursor,
        "output_check_cursor": checkpoint.output_check_cursor,
        "output_support_cursor": checkpoint.output_support_cursor,
        "output_check_record_cursor": checkpoint.output_check_record_cursor,
        "output_check_claim_cursor": checkpoint.output_check_claim_cursor,
        "output_check_relation_cursor": checkpoint.output_check_relation_cursor,
        "relation_context_cursor": checkpoint.relation_context_cursor,
        "relation_slice_forward": {
            key: value.value for key, value in checkpoint.relation_slice_forward.items()
        },
        "relation_slice_backward": {
            key: value.value for key, value in checkpoint.relation_slice_backward.items()
        },
        "pending_check_fingerprints": sorted(checkpoint.pending_check_fingerprints),
        "acked_check_fingerprints": sorted(checkpoint.acked_check_fingerprints),
        "failed_check_fingerprints": sorted(checkpoint.failed_check_fingerprints),
        "source_fragment_forward": {
            key: value.value for key, value in checkpoint.source_fragment_forward.items()
        },
        "source_fragment_backward": {
            key: value.value for key, value in checkpoint.source_fragment_backward.items()
        },
        "source_record_forward": {
            key: value.value for key, value in checkpoint.source_record_forward.items()
        },
        "source_record_backward": {
            key: value.value for key, value in checkpoint.source_record_backward.items()
        },
        "output_claim_forward": {
            key: value.value for key, value in checkpoint.output_claim_forward.items()
        },
        "output_claim_backward": {
            key: value.value for key, value in checkpoint.output_claim_backward.items()
        },
        "render_record_cursor": checkpoint.render_record_cursor,
        "source_pages_sent": checkpoint.source_pages_sent,
        "layered_partial": (
            checkpoint.layered_partial.model_dump(mode="json")
            if checkpoint.layered_partial
            else None
        ),
        "forward_semantic_check_status": checkpoint.forward_semantic_check_status.value,
        "backward_semantic_check_status": checkpoint.backward_semantic_check_status.value,
        "semantic_check_method": checkpoint.semantic_check_method,
        "processing_status": checkpoint.processing_status.value,
        "unprocessed_segment_ids": [str(item) for item in checkpoint.unprocessed_segment_ids],
        "stage_traces": [_serialize_stage_trace(item) for item in checkpoint.stage_traces],
        "last_error_category": checkpoint.last_error_category,
        "last_error_message": checkpoint.last_error_message,
        "expected_fragment_count": checkpoint.expected_fragment_count,
        "processed_fragment_count": checkpoint.processed_fragment_count,
        "updated_at": checkpoint.updated_at.isoformat(),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def deserialize_checkpoint(
    text: str, *, binding: CheckpointBinding | None = None
) -> EngineCheckpoint:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = "checkpoint JSON is invalid"
        raise CheckpointSerializationError(msg) from exc
    if not isinstance(raw, dict):
        msg = "checkpoint JSON must be an object"
        raise CheckpointSerializationError(msg)
    _reject_unknown(raw, _CHECKPOINT_KEYS, label="checkpoint")
    if raw["serialization_version"] != CHECKPOINT_SERIALIZATION_VERSION:
        msg = "unsupported checkpoint serialization version"
        raise CheckpointSerializationError(msg)
    layered_raw = raw["layered_partial"]
    records = raw["atomic_records"]
    relations = raw["relations"]
    traces = raw["stage_traces"]
    if (
        not isinstance(records, list)
        or not isinstance(relations, list)
        or not isinstance(traces, list)
    ):
        msg = "checkpoint collections must be lists"
        raise CheckpointSerializationError(msg)
    try:
        stage = StageOperation(raw["stage"])
        forward = CheckStatus(raw["forward_semantic_check_status"])
        backward = CheckStatus(raw["backward_semantic_check_status"])
        processing = ProcessingJobStatus(raw["processing_status"])
    except ValueError as exc:
        msg = "checkpoint contains an unknown enum value"
        raise CheckpointSerializationError(msg) from exc
    checkpoint = EngineCheckpoint(
        checkpoint_id=UUID(raw["checkpoint_id"]),
        binding_version=_optional_str(raw, "binding_version"),
        snapshot_id=_optional_str(raw, "snapshot_id"),
        snapshot_content_hash=_optional_str(raw, "snapshot_content_hash"),
        prompt_cache_key=_optional_str(raw, "prompt_cache_key"),
        schema_identity=_optional_str(raw, "schema_identity"),
        algorithm_identity=_optional_str(raw, "algorithm_identity"),
        input_fingerprint=_optional_str(raw, "input_fingerprint"),
        session_id=_optional_str(raw, "session_id"),
        requested_model=_optional_str(raw, "requested_model"),
        requested_reasoning_effort=_optional_str(raw, "requested_reasoning_effort"),
        limits_fingerprint=_optional_str(raw, "limits_fingerprint"),
        stage=stage,
        extract_page_index=_require_int(raw, "extract_page_index"),
        extract_fragment_cursor=_require_int(raw, "extract_fragment_cursor"),
        extract_complete=_require_bool(raw, "extract_complete"),
        reconcile_complete=_require_bool(raw, "reconcile_complete"),
        check_complete=_require_bool(raw, "check_complete"),
        render_complete=_require_bool(raw, "render_complete"),
        completed_fragment_indices=tuple(_require_int_list(raw, "completed_fragment_indices")),
        inspected_segment_ids=tuple(
            UUID(item) for item in _require_str_list(raw, "inspected_segment_ids")
        ),
        pending_extract_segment_indices=tuple(
            _require_int_list(raw, "pending_extract_segment_indices")
        ),
        atomic_records=[AtomicRecord.model_validate(item) for item in records],
        relations=[RecordRelation.model_validate(item) for item in relations],
        reconcile_page_index=_require_int(raw, "reconcile_page_index"),
        check_page_index=_require_int(raw, "check_page_index"),
        source_check_cursor=_require_int(raw, "source_check_cursor"),
        source_check_record_cursor=_require_int(raw, "source_check_record_cursor"),
        source_check_claim_cursor=_require_int(raw, "source_check_claim_cursor"),
        source_check_relation_cursor=_require_int(raw, "source_check_relation_cursor"),
        output_check_cursor=_require_int(raw, "output_check_cursor"),
        output_support_cursor=_require_int(raw, "output_support_cursor"),
        output_check_record_cursor=_require_int(raw, "output_check_record_cursor"),
        output_check_claim_cursor=_require_int(raw, "output_check_claim_cursor"),
        output_check_relation_cursor=_require_int(raw, "output_check_relation_cursor"),
        relation_context_cursor=_require_int(raw, "relation_context_cursor"),
        relation_slice_forward=_check_status_map(
            raw["relation_slice_forward"], key="relation_slice_forward"
        ),
        relation_slice_backward=_check_status_map(
            raw["relation_slice_backward"], key="relation_slice_backward"
        ),
        pending_check_fingerprints=set(_require_str_list(raw, "pending_check_fingerprints")),
        acked_check_fingerprints=set(_require_str_list(raw, "acked_check_fingerprints")),
        failed_check_fingerprints=set(_require_str_list(raw, "failed_check_fingerprints")),
        source_fragment_forward=_check_status_map(
            raw["source_fragment_forward"], key="source_fragment_forward"
        ),
        source_fragment_backward=_check_status_map(
            raw["source_fragment_backward"], key="source_fragment_backward"
        ),
        source_record_forward=_check_status_map(
            raw["source_record_forward"], key="source_record_forward"
        ),
        source_record_backward=_check_status_map(
            raw["source_record_backward"], key="source_record_backward"
        ),
        output_claim_forward=_check_status_map(
            raw["output_claim_forward"], key="output_claim_forward"
        ),
        output_claim_backward=_check_status_map(
            raw["output_claim_backward"], key="output_claim_backward"
        ),
        render_record_cursor=_require_int(raw, "render_record_cursor"),
        source_pages_sent=_require_int(raw, "source_pages_sent"),
        layered_partial=LayeredMeetingResult.model_validate(layered_raw) if layered_raw else None,
        forward_semantic_check_status=forward,
        backward_semantic_check_status=backward,
        semantic_check_method=_optional_str(raw, "semantic_check_method"),
        processing_status=processing,
        unprocessed_segment_ids=[
            UUID(item) for item in _require_str_list(raw, "unprocessed_segment_ids")
        ],
        stage_traces=[_deserialize_stage_trace(item) for item in traces],
        last_error_category=_optional_str(raw, "last_error_category"),
        last_error_message=_optional_str(raw, "last_error_message"),
        expected_fragment_count=_require_int(raw, "expected_fragment_count"),
        processed_fragment_count=_require_int(raw, "processed_fragment_count"),
        updated_at=datetime.fromisoformat(raw["updated_at"]),
    )
    if binding is not None:
        assert_checkpoint_compatible(checkpoint, binding)
    return checkpoint
