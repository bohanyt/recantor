from __future__ import annotations

from uuid import UUID

from pydantic import Field, StrictInt

from recantor.meeting_essence.contracts import SourceCoverageFlags, TranscriptGapMarker
from recantor.meeting_essence.strict import StrictModel


class SourceDescriptor(StrictModel):
    """Bounded source identity for stage wire payloads (no full segment ledger)."""

    contract_version: str = Field(default="meeting-intelligence.snapshot.v1", min_length=1)
    session_id: UUID
    snapshot_id: str = Field(min_length=8)
    snapshot_content_hash: str = Field(min_length=64, max_length=64)
    publication_boundary_sequence: StrictInt = Field(ge=0)
    enumerated_segment_count: StrictInt = Field(ge=0)
    meeting_metadata: dict[str, str] = Field(default_factory=dict)
    coverage: SourceCoverageFlags = Field(default_factory=SourceCoverageFlags)


class WireSegmentFragment(StrictModel):
    segment_id: UUID
    sequence: StrictInt = Field(ge=1)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    text: str
    content_hash: str = Field(min_length=64, max_length=64)
    fragment_index: StrictInt = Field(default=0, ge=0)
    fragment_count: StrictInt = Field(default=1, ge=1)
    text_span_start_cp: StrictInt | None = Field(default=None, ge=0)
    text_span_end_cp: StrictInt | None = Field(default=None, gt=0)

    @property
    def char_count(self) -> int:
        return len(self.text)


class WireRecordPage(StrictModel):
    page_index: StrictInt = Field(ge=0)
    page_count: StrictInt = Field(ge=1)
    record_ids: tuple[str, ...] = Field(min_length=1)


class WirePayloadAudit(StrictModel):
    """Independent units: UTF-8 transport, schema bytes, and conservative token bounds.

    ``*_chars`` fields on the original contract store UTF-8 bytes, not Unicode code points.
    Code points are ``unicode_payload_codepoints``. Token fields are a documented upper bound
    (one token is at most one UTF-8 byte of input, plus a separate output reserve).
    """

    prompt_chars: StrictInt = Field(ge=0)
    schema_chars: StrictInt = Field(ge=0)
    metadata_chars: StrictInt = Field(ge=0)
    payload_body_chars: StrictInt = Field(ge=0)
    reserved_output_chars: StrictInt = Field(ge=0)
    total_estimated_chars: StrictInt = Field(ge=0)
    token_upper_bound: StrictInt = Field(ge=0)
    estimation_method: str = Field(min_length=1)
    transport_utf8_bytes: StrictInt = Field(default=0, ge=0)
    schema_utf8_bytes: StrictInt = Field(default=0, ge=0)
    prompt_utf8_bytes: StrictInt = Field(default=0, ge=0)
    context_input_token_upper_bound: StrictInt = Field(default=0, ge=0)
    reserved_output_tokens: StrictInt = Field(default=0, ge=0)
    output_cardinality_limit: StrictInt = Field(default=0, ge=0)
    unicode_payload_codepoints: StrictInt = Field(default=0, ge=0)


def bounded_coverage_for_wire(
    coverage: SourceCoverageFlags,
    *,
    max_explicit_gaps: int,
    max_gap_sequence_span: int,
) -> SourceCoverageFlags:
    """Split long gaps into exact covers. Raises instead of shortening a span."""
    from recantor.meeting_essence.source_reader import split_gap_exact

    gaps: list[TranscriptGapMarker] = []
    for gap in coverage.explicit_gaps:
        gaps.extend(split_gap_exact(gap, max_gap_sequence_span))
    if len(gaps) > max_explicit_gaps:
        msg = "explicit gaps do not fit one wire page"
        raise ValueError(msg)
    return coverage.model_copy(update={"explicit_gaps": gaps})
