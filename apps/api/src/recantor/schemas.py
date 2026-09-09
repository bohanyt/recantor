from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recantor.models import SessionKind, SessionState


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "recantor-api"


class ReadinessResponse(BaseModel):
    status: Literal["ready"] = "ready"
    database: Literal["ok"] = "ok"


class ApiMetaResponse(BaseModel):
    name: str = "Recantor API"
    api_version: Literal["v1"] = "v1"


class CreateLiveSessionRequest(BaseModel):
    client_request_id: UUID
    writer_id: str = Field(min_length=8, max_length=128)


class CaptureClaimRequest(BaseModel):
    writer_id: str = Field(min_length=8, max_length=128)
    expected_epoch: int | None = Field(default=None, ge=0)


class HeartbeatRequest(BaseModel):
    writer_id: str = Field(min_length=8, max_length=128)
    capture_epoch: int = Field(ge=1)
    client_monotonic_ms: int | None = Field(default=None, ge=0)


class RecordingSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_request_id: UUID
    kind: SessionKind
    state: SessionState
    active_writer_id: str | None
    capture_epoch: int
    last_heartbeat_at: datetime | None
    interrupted_at: datetime | None
    final_sequence: int | None
    final_monotonic_end_ms: int | None
    finalized_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SequenceRange(BaseModel):
    start: int = Field(ge=1)
    end: int = Field(ge=1)


class RecordingGapResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    client_gap_id: UUID
    sequence_start: int | None
    sequence_end: int | None
    wall_started_at: datetime | None
    wall_ended_at: datetime | None
    reason: str
    created_at: datetime


class RecordingStateResponse(BaseModel):
    session: RecordingSessionResponse
    accepted_ranges: list[SequenceRange]
    accepted_count: int
    highest_contiguous_sequence: int
    gaps: list[RecordingGapResponse]


class ChunkAckResponse(BaseModel):
    session_id: UUID
    sequence: int
    sha256: str
    byte_length: int
    storage_key: str
    idempotent: bool


class GapDeclarationRequest(BaseModel):
    client_gap_id: UUID
    writer_id: str = Field(min_length=8, max_length=128)
    capture_epoch: int = Field(ge=1)
    sequence_start: int | None = Field(default=None, ge=1)
    sequence_end: int | None = Field(default=None, ge=1)
    wall_started_at: datetime | None = None
    wall_ended_at: datetime | None = None
    reason: str = Field(min_length=3, max_length=160)

    @model_validator(mode="after")
    def validate_gap(self) -> "GapDeclarationRequest":
        sequence_gap = self.sequence_start is not None or self.sequence_end is not None
        wall_gap = self.wall_started_at is not None or self.wall_ended_at is not None
        if not sequence_gap and not wall_gap:
            raise ValueError("a sequence range or wall-clock interval is required")
        if sequence_gap and (
            self.sequence_start is None
            or self.sequence_end is None
            or self.sequence_end < self.sequence_start
        ):
            raise ValueError("sequence_start and sequence_end must form a valid range")
        if wall_gap and (
            self.wall_started_at is None
            or self.wall_ended_at is None
            or self.wall_ended_at < self.wall_started_at
        ):
            raise ValueError("wall_started_at and wall_ended_at must form a valid interval")
        return self


class FinalizeSessionRequest(BaseModel):
    writer_id: str = Field(min_length=8, max_length=128)
    capture_epoch: int = Field(ge=1)
    final_sequence: int = Field(ge=0)
    final_monotonic_end_ms: int = Field(ge=0)
    gap_sequences: list[int] = Field(default_factory=list)


class FinalizeSessionResponse(BaseModel):
    session: RecordingSessionResponse
    complete: bool
    missing_sequences: list[int]
    gaps: list[RecordingGapResponse]
