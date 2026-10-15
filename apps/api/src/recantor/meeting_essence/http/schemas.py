from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from recantor.meeting_essence.contracts import (
    ActionFollowUpItem,
    AtomicRecord,
    CheckStatus,
    CoverageLimitation,
    EvidenceReference,
    OpenItem,
    OverviewBullet,
    ProcessingJobStatus,
    RecordRelation,
    TopicDetail,
)


class MeetingEssenceDeriveRequest(BaseModel):
    client_request_id: UUID
    operation_version: str | None = None


class MeetingEssenceDeriveResponse(BaseModel):
    dispatch_id: UUID
    client_request_id: UUID
    status: str
    reused: bool = False
    operation_version: str


class MeetingEssenceSourceState(BaseModel):
    publication_high_water: int = 0
    highest_processed_publication: int = 0
    work_intent: str = "open"
    audio_final_sequence: int | None = None
    frozen_transcript_sequence: int | None = None
    transcript_publication_complete: bool = False
    recording_audio_complete: bool | None = None
    stt_backlog_pending: bool = False
    source_pending: bool = False


class MeetingEssenceCheckState(BaseModel):
    structural_check_status: CheckStatus = CheckStatus.NOT_STARTED
    forward_semantic_check_status: CheckStatus = CheckStatus.NOT_STARTED
    backward_semantic_check_status: CheckStatus = CheckStatus.NOT_STARTED
    semantic_check_method: str | None = None


class MeetingEssenceArtifactIdentity(BaseModel):
    artifact_id: UUID
    is_current: bool
    is_stale: bool
    snapshot_id: str
    snapshot_content_hash: str
    processing_status: ProcessingJobStatus | None = None


class MeetingEssenceDerivationStatusResponse(BaseModel):
    dispatch_id: UUID
    session_id: UUID
    dispatch_status: str
    disposition: str | None = None
    job_id: UUID | None = None
    attempt_count: int = 0
    last_error_category: str | None = None
    last_error_code: str | None = None
    processing_status: ProcessingJobStatus | None = None
    source: MeetingEssenceSourceState
    checks: MeetingEssenceCheckState = Field(default_factory=MeetingEssenceCheckState)
    limitations: list[CoverageLimitation] = Field(default_factory=list)
    current_artifact: MeetingEssenceArtifactIdentity | None = None
    stale_artifact: MeetingEssenceArtifactIdentity | None = None
    prior_useful_artifact: MeetingEssenceArtifactIdentity | None = None
    stale_artifact_id: UUID | None = None
    recording_state: str | None = None
    operation_version: str | None = None
    updated_at: datetime | None = None


class _ResultPageBase(BaseModel):
    session_id: UUID
    artifact_id: UUID
    next_cursor: str | None = None
    total_count: int
    processing_status: ProcessingJobStatus
    limitations: list[CoverageLimitation] = Field(default_factory=list)
    is_current: bool
    is_stale: bool


class MeetingEssenceOverviewPage(_ResultPageBase):
    layer: Literal["overview"] = "overview"
    items: list[OverviewBullet]


class MeetingEssenceTopicsPage(_ResultPageBase):
    layer: Literal["topics"] = "topics"
    items: list[TopicDetail]


class MeetingEssenceActionsPage(_ResultPageBase):
    layer: Literal["actions"] = "actions"
    items: list[ActionFollowUpItem]


class MeetingEssenceOpenItemsPage(_ResultPageBase):
    layer: Literal["open_items"] = "open_items"
    items: list[OpenItem]


class MeetingEssenceRecordsPage(_ResultPageBase):
    layer: Literal["records"] = "records"
    items: list[AtomicRecord]


class MeetingEssenceRelationsPage(_ResultPageBase):
    layer: Literal["relations"] = "relations"
    items: list[RecordRelation]


MeetingEssenceResultPageResponse = Annotated[
    MeetingEssenceOverviewPage
    | MeetingEssenceTopicsPage
    | MeetingEssenceActionsPage
    | MeetingEssenceOpenItemsPage
    | MeetingEssenceRecordsPage
    | MeetingEssenceRelationsPage,
    Field(discriminator="layer"),
]


class MeetingEssenceEvidenceResponse(BaseModel):
    session_id: UUID
    snapshot_content_hash: str
    record_id: str
    evidence: list[EvidenceReference]
    record: AtomicRecord | None = None


class MeetingEssenceRetryResponse(BaseModel):
    dispatch_id: UUID
    accepted: bool


class MeetingEssenceCancelResponse(BaseModel):
    dispatch_id: UUID
    cancelled: bool
