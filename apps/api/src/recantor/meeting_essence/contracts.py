from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from recantor.meeting_essence.identities import (
    assert_known_algorithm_identity,
    assert_known_prompt_bundle_id,
    assert_known_schema_identity,
)
from recantor.meeting_essence.strict import StrictModel

# --- Content vs processing (distinct axes) ---


class RecordContentStatus(StrEnum):
    PROPOSAL = "proposal"
    REQUEST = "request"
    ASSIGNMENT = "assignment"
    ACCEPTANCE = "acceptance"
    DECISION = "decision"
    CANCELLATION = "cancellation"
    REPORTED_COMPLETION = "reported_completion"
    FINDING = "finding"
    UNRESOLVED = "unresolved"


class RecordKind(StrEnum):
    FINDING = "finding"
    PROPOSAL = "proposal"
    DECISION = "decision"
    ACTION = "action"
    REASON = "reason"
    CONDITION = "condition"
    RISK = "risk"
    OBJECTION = "objection"
    UNRESOLVED_QUESTION = "unresolved_question"


class ProcessingJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CheckStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class RecordRelationType(StrEnum):
    DUPLICATES = "duplicates"
    SUPERSEDES = "supersedes"
    CANCELS = "cancels"
    ACCEPTS = "accepts"
    ASSIGNS = "assigns"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class StageOperation(StrEnum):
    EXTRACT = "extract"
    RECONCILE = "reconcile"
    CHECK = "check"
    RENDER = "render"


class DeadlineNormalizationBasis(StrEnum):
    """How deadline_normalized was derived from deadline_raw and manifest anchors."""

    NONE = "none"
    MEETING_DATE_TIMEZONE = "meeting_date_timezone"
    ABSOLUTE_SOURCE = "absolute_source"


_ACTION_CONTENT_STATUSES = frozenset(
    {
        RecordContentStatus.REQUEST,
        RecordContentStatus.ASSIGNMENT,
        RecordContentStatus.ACCEPTANCE,
        RecordContentStatus.REPORTED_COMPLETION,
        RecordContentStatus.CANCELLATION,
    }
)

_KIND_STATUS_ALLOWLIST: dict[RecordKind, frozenset[RecordContentStatus]] = {
    RecordKind.FINDING: frozenset({RecordContentStatus.FINDING}),
    RecordKind.PROPOSAL: frozenset({RecordContentStatus.PROPOSAL}),
    RecordKind.DECISION: frozenset({RecordContentStatus.PROPOSAL, RecordContentStatus.DECISION}),
    RecordKind.ACTION: frozenset(_ACTION_CONTENT_STATUSES),
    RecordKind.REASON: frozenset({RecordContentStatus.FINDING}),
    RecordKind.CONDITION: frozenset({RecordContentStatus.FINDING, RecordContentStatus.DECISION}),
    RecordKind.RISK: frozenset({RecordContentStatus.FINDING}),
    RecordKind.OBJECTION: frozenset({RecordContentStatus.FINDING, RecordContentStatus.UNRESOLVED}),
    RecordKind.UNRESOLVED_QUESTION: frozenset({RecordContentStatus.UNRESOLVED}),
}


def _validate_assignee_deadline_fields(
    *,
    assignee: str | None,
    assignee_explicitly_undetermined: bool,
    deadline_raw: str | None,
    deadline_normalized: datetime | None,
    deadline_unmentioned: bool,
    deadline_normalization_basis: DeadlineNormalizationBasis,
) -> None:
    if assignee_explicitly_undetermined and assignee is not None:
        msg = "assignee must be null when assignee_explicitly_undetermined is true"
        raise ValueError(msg)
    if deadline_unmentioned and (deadline_raw is not None or deadline_normalized is not None):
        msg = "deadline fields must be null when deadline_unmentioned is true"
        raise ValueError(msg)
    if deadline_normalized is not None and deadline_raw is None:
        msg = "deadline_normalized requires deadline_raw with supported anchors"
        raise ValueError(msg)
    if deadline_normalized is None:
        if deadline_normalization_basis != DeadlineNormalizationBasis.NONE:
            msg = "deadline_normalization_basis must be none when deadline_normalized is null"
            raise ValueError(msg)
        return
    if deadline_normalization_basis == DeadlineNormalizationBasis.NONE:
        msg = "deadline_normalized requires an explicit deadline_normalization_basis"
        raise ValueError(msg)
    if deadline_normalized.tzinfo is None:
        msg = "deadline_normalized must be timezone-aware"
        raise ValueError(msg)


class UnicodeTextSpan(StrictModel):
    """Inclusive start, exclusive end, measured in Unicode code points."""

    start_cp: StrictInt = Field(ge=0)
    end_cp: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def _end_after_start(self) -> UnicodeTextSpan:
        if self.end_cp <= self.start_cp:
            msg = "text span end_cp must be greater than start_cp"
            raise ValueError(msg)
        return self


class SourceFragmentRef(StrictModel):
    """Bounded source excerpt for oversized segments (contract-only; engine in #71)."""

    segment_id: UUID
    text_span: UnicodeTextSpan | None = None


class EvidenceReference(StrictModel):
    segment_id: UUID
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    text_span: UnicodeTextSpan | None = None

    @model_validator(mode="after")
    def _time_range_valid(self) -> EvidenceReference:
        if self.end_ms <= self.start_ms:
            msg = "evidence end_ms must be greater than start_ms"
            raise ValueError(msg)
        return self


class ManifestSegmentEntry(StrictModel):
    """Immutable canonical segment row in a frozen snapshot."""

    id: UUID
    sequence: StrictInt = Field(ge=1)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    text: str
    language: str | None = None
    content_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _content_hash_matches_text(self) -> ManifestSegmentEntry:
        from recantor.meeting_essence.identity import segment_content_hash

        expected = segment_content_hash(self.text)
        if self.content_hash != expected:
            msg = "content_hash does not match exact UTF-8 segment text"
            raise ValueError(msg)
        if self.end_ms <= self.start_ms:
            msg = "segment end_ms must be greater than start_ms"
            raise ValueError(msg)
        return self


class TranscriptGapMarker(StrictModel):
    sequence_start: StrictInt | None = Field(default=None, ge=1)
    sequence_end: StrictInt | None = Field(default=None, ge=1)
    reason: str


class SourceCoverageFlags(StrictModel):
    transcript_publication_complete: bool = False
    recording_audio_complete: bool | None = None
    stt_backlog_pending: bool = False
    source_pending: bool = False
    explicit_gaps: list[TranscriptGapMarker] = Field(default_factory=list)


class SourceSnapshotManifest(StrictModel):
    contract_version: Literal["meeting-intelligence.snapshot.v1"] = (
        "meeting-intelligence.snapshot.v1"
    )
    session_id: UUID
    snapshot_id: str = Field(min_length=8)
    snapshot_content_hash: str = Field(min_length=64, max_length=64)
    publication_boundary_sequence: StrictInt = Field(ge=0)
    captured_at: datetime | None = None
    meeting_metadata: dict[str, str] = Field(default_factory=dict)
    segments: list[ManifestSegmentEntry] = Field(default_factory=list)
    coverage: SourceCoverageFlags = Field(default_factory=SourceCoverageFlags)

    @model_validator(mode="after")
    def _segments_and_boundary(self) -> SourceSnapshotManifest:
        if not self.segments:
            if not self.coverage.source_pending:
                msg = "empty segments require coverage.source_pending=true"
                raise ValueError(msg)
            if self.publication_boundary_sequence != 0:
                msg = "source_pending snapshots must use publication_boundary_sequence=0"
                raise ValueError(msg)
            return self

        if self.publication_boundary_sequence <= 0:
            msg = "non-empty snapshots require publication_boundary_sequence >= 1"
            raise ValueError(msg)

        segment_ids = [segment.id for segment in self.segments]
        if len(segment_ids) != len(set(segment_ids)):
            msg = "segment ids must be unique"
            raise ValueError(msg)

        for segment in self.segments:
            if segment.sequence > self.publication_boundary_sequence:
                msg = (
                    f"segment {segment.id} sequence {segment.sequence} exceeds "
                    f"publication boundary {self.publication_boundary_sequence}"
                )
                raise ValueError(msg)

        if self.coverage.transcript_publication_complete:
            present = {segment.sequence for segment in self.segments}
            gap_covered: set[int] = set()
            for gap in self.coverage.explicit_gaps:
                if gap.sequence_start is None or gap.sequence_end is None:
                    msg = (
                        "transcript_publication_complete requires bounded explicit_gaps "
                        "(sequence_start and sequence_end)"
                    )
                    raise ValueError(msg)
                gap_covered.update(range(gap.sequence_start, gap.sequence_end + 1))
            for sequence in range(1, self.publication_boundary_sequence + 1):
                if sequence in present or sequence in gap_covered:
                    continue
                msg = (
                    f"complete publication missing coverage for sequence {sequence} "
                    f"through boundary {self.publication_boundary_sequence}"
                )
                raise ValueError(msg)
        return self


class AtomicRecord(StrictModel):
    record_id: str = Field(min_length=4)
    topic: str = Field(min_length=1)
    kind: RecordKind
    claim_text: str = Field(min_length=1)
    content_status: RecordContentStatus
    evidence: list[EvidenceReference] = Field(min_length=1)
    assignee: str | None = Field(
        default=None,
        description="PIC when supported; null if unknown or explicitly undetermined.",
    )
    assignee_explicitly_undetermined: bool = False
    deadline_raw: str | None = None
    deadline_normalized: datetime | None = None
    deadline_unmentioned: bool = False
    deadline_normalization_basis: DeadlineNormalizationBasis = DeadlineNormalizationBasis.NONE
    condition_text: str | None = None
    rationale: str | None = None
    uncertainty: str | None = None

    @model_validator(mode="after")
    def _status_and_null_fields(self) -> AtomicRecord:
        allowed = _KIND_STATUS_ALLOWLIST.get(self.kind)
        if allowed is not None and self.content_status not in allowed:
            msg = f"content_status {self.content_status} is not valid for kind {self.kind}"
            raise ValueError(msg)
        _validate_assignee_deadline_fields(
            assignee=self.assignee,
            assignee_explicitly_undetermined=self.assignee_explicitly_undetermined,
            deadline_raw=self.deadline_raw,
            deadline_normalized=self.deadline_normalized,
            deadline_unmentioned=self.deadline_unmentioned,
            deadline_normalization_basis=self.deadline_normalization_basis,
        )
        return self


class RecordRelation(StrictModel):
    relation_type: RecordRelationType
    from_record_id: str = Field(min_length=4)
    to_record_id: str = Field(min_length=4)
    evidence: list[EvidenceReference] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_self_relation(self) -> RecordRelation:
        if self.from_record_id == self.to_record_id:
            msg = "self-relations are not allowed"
            raise ValueError(msg)
        return self


class TopicDetailPoint(StrictModel):
    """Accessible topic detail. Optional fields default so existing fixtures still load."""

    text: str = Field(min_length=1)
    evidence: list[EvidenceReference] = Field(min_length=1)
    kind: RecordKind | None = None
    content_status: RecordContentStatus | None = None
    condition_text: str | None = None
    rationale: str | None = None
    uncertainty: str | None = None
    assignee: str | None = None
    assignee_explicitly_undetermined: bool = False
    deadline_raw: str | None = None
    deadline_normalized: datetime | None = None
    deadline_unmentioned: bool = False
    deadline_normalization_basis: DeadlineNormalizationBasis = DeadlineNormalizationBasis.NONE
    superseded: bool = False


class TopicDetail(StrictModel):
    topic: str = Field(min_length=1)
    points: list[TopicDetailPoint] = Field(min_length=1)


class ActionFollowUpItem(StrictModel):
    task: str = Field(min_length=1)
    assignee: str | None = None
    assignee_explicitly_undetermined: bool = False
    deadline_raw: str | None = None
    deadline_normalized: datetime | None = None
    deadline_unmentioned: bool = False
    deadline_normalization_basis: DeadlineNormalizationBasis = DeadlineNormalizationBasis.NONE
    content_status: RecordContentStatus
    condition_text: str | None = None
    evidence: list[EvidenceReference] = Field(min_length=1)

    @model_validator(mode="after")
    def _action_status_and_null_fields(self) -> ActionFollowUpItem:
        if self.content_status not in _ACTION_CONTENT_STATUSES:
            msg = f"content_status {self.content_status} is not valid for action follow-ups"
            raise ValueError(msg)
        _validate_assignee_deadline_fields(
            assignee=self.assignee,
            assignee_explicitly_undetermined=self.assignee_explicitly_undetermined,
            deadline_raw=self.deadline_raw,
            deadline_normalized=self.deadline_normalized,
            deadline_unmentioned=self.deadline_unmentioned,
            deadline_normalization_basis=self.deadline_normalization_basis,
        )
        return self


class OpenItem(StrictModel):
    text: str = Field(min_length=1)
    evidence: list[EvidenceReference] = Field(min_length=1)


class CoverageLimitation(StrictModel):
    code: str
    message: str
    evidence_segment_ids: list[UUID] = Field(default_factory=list)


class OverviewBullet(StrictModel):
    text: str = Field(min_length=1)
    evidence: list[EvidenceReference] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _overview_text_substantive(cls, value: str) -> str:
        if not value.strip():
            msg = "overview bullet text must be substantive"
            raise ValueError(msg)
        return value


class ResultCoverageReport(StrictModel):
    snapshot_id: str
    source_segments_enumerated: StrictInt = Field(ge=0)
    source_segments_referenced: StrictInt = Field(ge=0)
    processing_job_status: ProcessingJobStatus = ProcessingJobStatus.PENDING
    structural_check_status: CheckStatus = CheckStatus.NOT_STARTED
    forward_semantic_check_status: CheckStatus = CheckStatus.NOT_STARTED
    backward_semantic_check_status: CheckStatus = CheckStatus.NOT_STARTED
    semantic_check_method: str | None = None
    limitations: list[CoverageLimitation] = Field(default_factory=list)
    overview_word_count_target: StrictInt = Field(default=180, ge=1)
    overview_bullet_target_min: StrictInt = Field(default=3, ge=1)
    overview_bullet_target_max: StrictInt = Field(default=7, ge=1)

    @model_validator(mode="after")
    def _semantic_method_required_when_completed(self) -> ResultCoverageReport:
        completed_semantic = {
            self.forward_semantic_check_status,
            self.backward_semantic_check_status,
        }
        if CheckStatus.COMPLETED in completed_semantic and not self.semantic_check_method:
            msg = "semantic_check_method is required when a semantic check is completed"
            raise ValueError(msg)
        return self


class LayeredMeetingResult(StrictModel):
    contract_version: Literal["meeting-intelligence.result.v1"] = "meeting-intelligence.result.v1"
    snapshot_id: str = Field(min_length=8)
    overview_bullets: list[OverviewBullet] = Field(min_length=1, max_length=20)
    topics: list[TopicDetail] = Field(min_length=1)
    actions: list[ActionFollowUpItem] = Field(default_factory=list)
    open_items: list[OpenItem] = Field(default_factory=list)
    atomic_records: list[AtomicRecord] = Field(default_factory=list)
    relations: list[RecordRelation] = Field(default_factory=list)
    coverage: ResultCoverageReport


class CheckConfiguration(StrictModel):
    forward_check_enabled: bool = True
    backward_check_enabled: bool = True
    max_attempts: StrictInt = Field(default=3, ge=1, le=20)


class TimingMetadata(StrictModel):
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model_calls: StrictInt = Field(default=0, ge=0)


class MeetingIntelligenceJobMetadata(StrictModel):
    job_id: UUID
    session_id: UUID
    snapshot_id: str
    stage: StageOperation | None = None
    processing_status: ProcessingJobStatus = ProcessingJobStatus.PENDING
    check_status: CheckStatus = CheckStatus.NOT_STARTED
    prompt_bundle_id: str
    schema_identity: str
    algorithm_identity: str
    input_fingerprint: str = Field(min_length=64, max_length=64)
    check_configuration: CheckConfiguration = Field(default_factory=CheckConfiguration)
    timing: TimingMetadata = Field(default_factory=TimingMetadata)
    failure_reason: str | None = None
    requested_model: str | None = None
    requested_reasoning_effort: str | None = None
    observed_model: str | None = None
    observed_reasoning_effort: str | None = None

    @field_validator("prompt_bundle_id")
    @classmethod
    def _known_prompt_bundle(cls, value: str) -> str:
        return assert_known_prompt_bundle_id(value)

    @field_validator("schema_identity")
    @classmethod
    def _known_schema(cls, value: str) -> str:
        return assert_known_schema_identity(value)

    @field_validator("algorithm_identity")
    @classmethod
    def _known_algorithm(cls, value: str) -> str:
        return assert_known_algorithm_identity(value)


class MeetingIntelligenceExampleBundle(StrictModel):
    """Public fixture wrapper: manifest + layered result + optional ledger."""

    manifest: SourceSnapshotManifest
    result: LayeredMeetingResult
    records: list[AtomicRecord] = Field(default_factory=list)
    relations: list[RecordRelation] = Field(default_factory=list)
