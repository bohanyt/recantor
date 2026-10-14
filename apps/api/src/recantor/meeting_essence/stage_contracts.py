from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CheckStatus,
    CoverageLimitation,
    EvidenceReference,
    LayeredMeetingResult,
    RecordRelation,
    SourceFragmentRef,
)
from recantor.meeting_essence.identities import (
    assert_known_algorithm_identity,
    assert_known_prompt_bundle_id,
    assert_known_schema_identity,
)
from recantor.meeting_essence.strict import StrictModel
from recantor.meeting_essence.wire_payload import (
    SourceDescriptor,
    WirePayloadAudit,
    WireRecordPage,
    WireSegmentFragment,
)


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
    segment_ids: tuple[str, ...] = Field(min_length=1)
    provenance: str = Field(min_length=1)
    source_fragments: tuple[SourceFragmentRef, ...] = ()


class ExtractStageRequest(StrictModel):
    source: SourceDescriptor
    segment_fragments: tuple[WireSegmentFragment, ...] = Field(min_length=1)
    context_fragments: tuple[WireSegmentFragment, ...] = ()
    budget: RequestBudget
    wire_audit: WirePayloadAudit
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)
    omitted_record_ids: tuple[str, ...] = ()


class ExtractStageResponse(StrictModel):
    extracted_records: list[AtomicRecord] = Field(default_factory=list)
    omitted_record_ids: list[str] = Field(default_factory=list)
    coverage_limitations: list[CoverageLimitation] = Field(default_factory=list)
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


class ReconcileStageRequest(StrictModel):
    source: SourceDescriptor
    atomic_records: list[AtomicRecord] = Field(min_length=1)
    record_page: WireRecordPage
    prior_relations: list[RecordRelation] = Field(default_factory=list)
    budget: RequestBudget
    wire_audit: WirePayloadAudit
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


class ReconcileStageResponse(StrictModel):
    reconciled_records: list[AtomicRecord] = Field(default_factory=list)
    relations: list[RecordRelation] = Field(default_factory=list)
    omitted_record_ids: list[str] = Field(default_factory=list)
    coverage_limitations: list[CoverageLimitation] = Field(default_factory=list)
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


class CheckPass(StrEnum):
    SOURCE_COVERAGE = "source_coverage"
    OUTPUT_COVERAGE = "output_coverage"
    RELATION_CONTEXT = "relation_context"


class CheckDirection(StrEnum):
    SOURCE_TO_OUTPUT = "source_to_output"
    OUTPUT_TO_SOURCE = "output_to_source"


def evidence_provenance_key(evidence: EvidenceReference) -> str:
    """Segment, original time, and Unicode span. This is not the type/from/to triple."""
    if evidence.text_span is None:
        span = "-"
    else:
        span = f"{evidence.text_span.start_cp}-{evidence.text_span.end_cp}"
    return f"{evidence.segment_id}@{evidence.start_ms}-{evidence.end_ms}:{span}"


def relation_wire_key(relation: RecordRelation) -> str:
    evidence = ",".join(evidence_provenance_key(item) for item in relation.evidence)
    return (
        f"{relation.relation_type.value}|{relation.from_record_id}|"
        f"{relation.to_record_id}|{evidence}"
    )


class AccessibleOutputClaim(StrictModel):
    """One consequential output surface. Overview is forward-checked and is not detail recall."""

    claim_id: str = Field(min_length=4)
    surface: Literal["topic", "action", "open_item", "overview"]
    text: str = Field(min_length=1)
    topic: str | None = None
    kind: str | None = None
    content_status: str | None = None
    condition_text: str | None = None
    rationale: str | None = None
    uncertainty: str | None = None
    assignee: str | None = None
    assignee_explicitly_undetermined: bool = False
    deadline_raw: str | None = None
    deadline_normalized: str | None = None
    deadline_unmentioned: bool = False
    deadline_normalization_basis: str | None = None
    superseded: bool = False
    evidence: list[EvidenceReference] = Field(min_length=1)


class CheckResultBinding(StrictModel):
    """Binds one check pass and cursor to the source, claims, ledger rows, and relations sent."""

    snapshot_content_hash: str = Field(min_length=64, max_length=64)
    check_pass: CheckPass
    direction: CheckDirection
    page_cursor: str = Field(min_length=1)
    source_fragment_keys: tuple[str, ...] = ()
    output_claim_ids: tuple[str, ...] = ()
    record_ids: tuple[str, ...] = ()
    relation_keys: tuple[str, ...] = ()
    judged_evidence_keys: tuple[str, ...] = ()
    relation_slice_keys: tuple[str, ...] = ()
    request_fingerprint: str = Field(min_length=64, max_length=64)
    source_fingerprint: str = Field(min_length=64, max_length=64)
    result_fingerprint: str = Field(min_length=64, max_length=64)


class CheckStageRequest(StrictModel):
    source: SourceDescriptor
    layered_result: LayeredMeetingResult | None = None
    atomic_records: list[AtomicRecord] = Field(default_factory=list)
    relations: list[RecordRelation] = Field(default_factory=list)
    record_page: WireRecordPage | None = None
    check_pass: CheckPass
    direction: CheckDirection
    source_fragments: tuple[WireSegmentFragment, ...] = ()
    output_claims: list[AccessibleOutputClaim] = Field(default_factory=list)
    result_binding: CheckResultBinding
    budget: RequestBudget
    wire_audit: WirePayloadAudit
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)

    @model_validator(mode="after")
    def _requires_check_payload(self) -> CheckStageRequest:
        if self.layered_result is None and not self.atomic_records and not self.source_fragments:
            msg = "check stage requires source fragments, atomic records, or layered output"
            raise ValueError(msg)
        if self.result_binding.check_pass != self.check_pass:
            msg = "check binding pass does not match the request"
            raise ValueError(msg)
        if self.result_binding.direction != self.direction:
            msg = "check binding direction does not match the request"
            raise ValueError(msg)
        if self.result_binding.output_claim_ids != tuple(
            claim.claim_id for claim in self.output_claims
        ):
            msg = "check binding claims do not match the request"
            raise ValueError(msg)
        if self.result_binding.record_ids != tuple(
            record.record_id for record in self.atomic_records
        ):
            msg = "check binding records do not match the request"
            raise ValueError(msg)
        if self.result_binding.relation_keys != tuple(
            relation_wire_key(relation) for relation in self.relations
        ):
            msg = "check binding relations do not match the request"
            raise ValueError(msg)
        if self.relations:
            evidence_keys = {
                evidence_provenance_key(item)
                for relation in self.relations
                for item in relation.evidence
            }
            judged = set(self.result_binding.judged_evidence_keys)
            if not judged or not judged <= evidence_keys:
                msg = "relationship check must name evidence on this relation"
                raise ValueError(msg)
            if not self.result_binding.relation_slice_keys:
                msg = "relationship check requires a slice receipt"
                raise ValueError(msg)
        return self


class CheckStageResponse(StrictModel):
    forward_semantic_check_status: CheckStatus
    backward_semantic_check_status: CheckStatus
    semantic_check_method: str = Field(min_length=1)
    check_pass: CheckPass
    direction: CheckDirection
    covered_fragment_keys: tuple[str, ...] = ()
    covered_claim_ids: tuple[str, ...] = ()
    covered_record_ids: tuple[str, ...] = ()
    covered_relation_keys: tuple[str, ...] = ()
    request_fingerprint: str = Field(min_length=64, max_length=64)
    source_fingerprint: str = Field(min_length=64, max_length=64)
    result_fingerprint: str = Field(min_length=64, max_length=64)
    coverage_limitations: list[CoverageLimitation] = Field(default_factory=list)
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


class RenderStageRequest(StrictModel):
    source: SourceDescriptor
    atomic_records: list[AtomicRecord] = Field(default_factory=list)
    relations: list[RecordRelation] = Field(default_factory=list)
    prior_layered: LayeredMeetingResult | None = None
    budget: RequestBudget
    wire_audit: WirePayloadAudit
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


class RenderStageResponse(StrictModel):
    layered_result: LayeredMeetingResult
    metadata: ModelInvocationMetadata = Field(default_factory=ModelInvocationMetadata)


StageRequest = ExtractStageRequest | ReconcileStageRequest | CheckStageRequest | RenderStageRequest
StageResponse = (
    ExtractStageResponse | ReconcileStageResponse | CheckStageResponse | RenderStageResponse
)
