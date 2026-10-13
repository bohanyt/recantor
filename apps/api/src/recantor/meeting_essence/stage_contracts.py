from __future__ import annotations

from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CheckStatus,
    CoverageLimitation,
    LayeredMeetingResult,
    RecordRelation,
    SourceFragmentRef,
    SourceSnapshotManifest,
)
from recantor.meeting_essence.identities import (
    assert_known_algorithm_identity,
    assert_known_prompt_bundle_id,
    assert_known_schema_identity,
)
from recantor.meeting_essence.strict import StrictModel


class RequestBudget(StrictModel):
    max_segments: StrictInt = Field(ge=1)
    max_records: StrictInt = Field(ge=1, le=10_000)
    max_chars: StrictInt = Field(ge=1, le=500_000)


class StageProtocolMetadata(StrictModel):
    """Runtime prompt/schema identities carried on stage requests (#71 engine)."""

    prompt_bundle_id: str
    schema_identity: str
    algorithm_identity: str

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


class ModelInvocationMetadata(StrictModel):
    requested_model: str | None = None
    requested_reasoning_effort: str | None = None
    observed_model: str | None = None
    observed_reasoning_effort: str | None = None
    protocol: StageProtocolMetadata | None = None


class SegmentSlice(StrictModel):
    segment_ids: tuple[UUID, ...] = Field(min_length=1)
    provenance: str = Field(min_length=1)
    source_fragments: tuple[SourceFragmentRef, ...] = ()


class ExtractStageRequest(StrictModel):
    manifest: SourceSnapshotManifest
    segment_slice: SegmentSlice
    budget: RequestBudget
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)
    omitted_record_ids: tuple[str, ...] = ()


class ExtractStageResponse(StrictModel):
    extracted_records: list[AtomicRecord] = Field(default_factory=list)
    omitted_record_ids: list[str] = Field(default_factory=list)
    coverage_limitations: list[CoverageLimitation] = Field(default_factory=list)
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


class ReconcileStageRequest(StrictModel):
    manifest: SourceSnapshotManifest
    atomic_records: list[AtomicRecord] = Field(min_length=1)
    prior_relations: list[RecordRelation] = Field(default_factory=list)
    budget: RequestBudget
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


class ReconcileStageResponse(StrictModel):
    reconciled_records: list[AtomicRecord] = Field(default_factory=list)
    relations: list[RecordRelation] = Field(default_factory=list)
    omitted_record_ids: list[str] = Field(default_factory=list)
    coverage_limitations: list[CoverageLimitation] = Field(default_factory=list)
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


class CheckStageRequest(StrictModel):
    manifest: SourceSnapshotManifest
    layered_result: LayeredMeetingResult | None = None
    atomic_records: list[AtomicRecord] = Field(default_factory=list)
    relations: list[RecordRelation] = Field(default_factory=list)
    budget: RequestBudget
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)

    @model_validator(mode="after")
    def _requires_check_payload(self) -> CheckStageRequest:
        if self.layered_result is None and not self.atomic_records:
            msg = "check stage requires layered_result or atomic_records"
            raise ValueError(msg)
        return self


class CheckStageResponse(StrictModel):
    forward_semantic_check_status: CheckStatus
    backward_semantic_check_status: CheckStatus
    semantic_check_method: str = Field(min_length=1)
    coverage_limitations: list[CoverageLimitation] = Field(default_factory=list)
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


class RenderStageRequest(StrictModel):
    manifest: SourceSnapshotManifest
    atomic_records: list[AtomicRecord] = Field(default_factory=list)
    relations: list[RecordRelation] = Field(default_factory=list)
    prior_layered: LayeredMeetingResult | None = None
    budget: RequestBudget
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


class RenderStageResponse(StrictModel):
    layered_result: LayeredMeetingResult
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


StageRequest = ExtractStageRequest | ReconcileStageRequest | CheckStageRequest | RenderStageRequest
StageResponse = (
    ExtractStageResponse | ReconcileStageResponse | CheckStageResponse | RenderStageResponse
)
