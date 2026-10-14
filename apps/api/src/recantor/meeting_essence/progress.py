from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CheckStatus,
    LayeredMeetingResult,
    ProcessingJobStatus,
    RecordRelation,
    StageOperation,
)
from recantor.meeting_essence.wire_payload import WirePayloadAudit


@dataclass
class StageCallTrace:
    operation: StageOperation
    started_at: datetime
    finished_at: datetime | None = None
    wire_audit: WirePayloadAudit | None = None
    requested_model: str | None = None
    observed_model: str | None = None
    error: str | None = None
    error_category: str | None = None
    attempt_count: int = 0
    attempts: list[dict[str, str | None]] = field(default_factory=list)
    serialized_utf8_bytes: int | None = None


@dataclass
class EngineCheckpoint:
    """In-memory resume state (#72 will persist)."""

    checkpoint_id: UUID = field(default_factory=uuid4)
    binding_version: str | None = None
    snapshot_id: str | None = None
    snapshot_content_hash: str | None = None
    prompt_cache_key: str | None = None
    schema_identity: str | None = None
    algorithm_identity: str | None = None
    input_fingerprint: str | None = None
    session_id: str | None = None
    requested_model: str | None = None
    requested_reasoning_effort: str | None = None
    limits_fingerprint: str | None = None
    stage: StageOperation = StageOperation.EXTRACT
    extract_page_index: int = 0
    extract_fragment_cursor: int = 0
    extract_complete: bool = False
    reconcile_complete: bool = False
    check_complete: bool = False
    render_complete: bool = False
    completed_fragment_indices: tuple[int, ...] = ()
    inspected_segment_ids: tuple[UUID, ...] = ()
    pending_extract_segment_indices: tuple[int, ...] = ()
    atomic_records: list[AtomicRecord] = field(default_factory=list)
    relations: list[RecordRelation] = field(default_factory=list)
    reconcile_page_index: int = 0
    check_page_index: int = 0
    source_check_cursor: int = 0
    source_check_record_cursor: int = 0
    source_check_claim_cursor: int = 0
    source_check_relation_cursor: int = 0
    output_check_cursor: int = 0
    output_support_cursor: int = 0
    output_check_record_cursor: int = 0
    output_check_claim_cursor: int = 0
    output_check_relation_cursor: int = 0
    relation_context_cursor: int = 0
    relation_slice_forward: dict[str, CheckStatus] = field(default_factory=dict)
    relation_slice_backward: dict[str, CheckStatus] = field(default_factory=dict)
    pending_check_fingerprints: set[str] = field(default_factory=set)
    acked_check_fingerprints: set[str] = field(default_factory=set)
    failed_check_fingerprints: set[str] = field(default_factory=set)
    source_fragment_forward: dict[str, CheckStatus] = field(default_factory=dict)
    source_fragment_backward: dict[str, CheckStatus] = field(default_factory=dict)
    source_record_forward: dict[str, CheckStatus] = field(default_factory=dict)
    source_record_backward: dict[str, CheckStatus] = field(default_factory=dict)
    output_claim_forward: dict[str, CheckStatus] = field(default_factory=dict)
    output_claim_backward: dict[str, CheckStatus] = field(default_factory=dict)
    render_record_cursor: int = 0
    source_pages_sent: int = 0
    layered_partial: LayeredMeetingResult | None = None
    forward_semantic_check_status: CheckStatus = CheckStatus.NOT_STARTED
    backward_semantic_check_status: CheckStatus = CheckStatus.NOT_STARTED
    semantic_check_method: str | None = None
    processing_status: ProcessingJobStatus = ProcessingJobStatus.RUNNING
    unprocessed_segment_ids: list[UUID] = field(default_factory=list)
    stage_traces: list[StageCallTrace] = field(default_factory=list)
    last_error_category: str | None = None
    last_error_message: str | None = None
    expected_fragment_count: int = 0
    processed_fragment_count: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(tz=UTC)
