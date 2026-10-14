from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from meeting_intelligence_fixtures import (
    FIXTURE_SESSION_ID,
    SEGMENT_SPECS,
    build_s01_s06_bundle,
)

from recantor.meeting_essence.budget import build_wire_audit_compat
from recantor.meeting_essence.contracts import (
    CheckStatus,
    ManifestSegmentEntry,
    ProcessingJobStatus,
    SourceCoverageFlags,
    StageOperation,
)
from recantor.meeting_essence.deterministic_provider import DeterministicMeetingEssenceProvider
from recantor.meeting_essence.engine import MeetingEssenceEngine
from recantor.meeting_essence.identity import segment_content_hash
from recantor.meeting_essence.prompt_loader import load_prompt_bundle
from recantor.meeting_essence.source_reader import (
    CanonicalSegmentInput,
    InMemoryCanonicalSourceReader,
    materialize_manifest_from_reader,
    split_segment_fragments,
)
from recantor.meeting_essence.stage_contracts import ExtractStageRequest, RequestBudget
from recantor.meeting_essence.wire_payload import SourceDescriptor


def _reader_from_specs(
    specs: list[tuple[str, int, int, int, str]],
) -> InMemoryCanonicalSourceReader:
    segments = [
        CanonicalSegmentInput(
            id=UUID(segment_id),
            sequence=sequence,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
        )
        for segment_id, sequence, start_ms, end_ms, text in specs
    ]
    return InMemoryCanonicalSourceReader(segments=segments)


@pytest.mark.asyncio
async def test_engine_s01_s06_deterministic_pipeline() -> None:
    bundle = build_s01_s06_bundle()
    reader = _reader_from_specs(SEGMENT_SPECS)
    engine = MeetingEssenceEngine(
        provider=DeterministicMeetingEssenceProvider(gold=bundle),
    )
    result = await engine.run_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        publication_boundary_sequence=6,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
    )
    assert result.processing_status == ProcessingJobStatus.SUCCEEDED
    assert result.layered_result is not None
    assert result.layered_result.coverage.forward_semantic_check_status == CheckStatus.COMPLETED
    assert result.checkpoint.atomic_records
    dini = next(record for record in result.checkpoint.atomic_records if record.assignee == "Dini")
    assert dini.deadline_raw == "besok pukul 12.00"
    assert dini.deadline_normalized is None
    invoice = next(
        record for record in result.checkpoint.atomic_records if record.topic == "Invoice vendor"
    )
    assert invoice.assignee_explicitly_undetermined
    assert invoice.deadline_unmentioned
    extract_samples = [
        sample for sample in result.trace.wire_payload_samples if sample.get("stage") == "extract"
    ]
    assert extract_samples
    assert all(sample["has_full_manifest"] is False for sample in extract_samples)


@pytest.mark.asyncio
async def test_post_freeze_publication_does_not_change_manifest() -> None:
    reader = _reader_from_specs(SEGMENT_SPECS)
    reader.freeze_publication_boundary(6)
    before, _ = materialize_manifest_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
        page_size=2,
    )
    reader.inject_post_freeze_segment(
        CanonicalSegmentInput(
            id=uuid4(),
            sequence=99,
            start_ms=200_000,
            end_ms=201_000,
            text="Late Friday utterance that must not override Tuesday decisions.",
        )
    )
    after, _ = materialize_manifest_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
        page_size=2,
    )
    assert before.snapshot_content_hash == after.snapshot_content_hash
    assert len(after.segments) == 6


def test_oversized_segment_split_preserves_offsets_and_hash() -> None:
    text = "α" * 10_000
    segment = ManifestSegmentEntry(
        id=uuid4(),
        sequence=1,
        start_ms=0,
        end_ms=1000,
        text=text,
        content_hash=segment_content_hash(text),
    )
    parts = split_segment_fragments(segment, max_chars=4000)
    assert len(parts) == 3
    rebuilt = "".join(part.text for part in parts)
    assert rebuilt == text
    assert parts[0].text_span_start_cp == 0
    assert parts[-1].text_span_end_cp == len(text)
    assert all(part.content_hash == segment.content_hash for part in parts)


def test_wire_extract_request_has_no_manifest_segments_ledger() -> None:
    bundle = build_s01_s06_bundle()
    source = SourceDescriptor(
        session_id=bundle.manifest.session_id,
        snapshot_id=bundle.manifest.snapshot_id,
        snapshot_content_hash=bundle.manifest.snapshot_content_hash,
        publication_boundary_sequence=bundle.manifest.publication_boundary_sequence,
        enumerated_segment_count=len(bundle.manifest.segments),
    )
    segment = bundle.manifest.segments[0]
    request = ExtractStageRequest(
        source=source,
        segment_fragments=(split_segment_fragments(segment, max_chars=4000)[0],),
        budget=RequestBudget(max_segments=8, max_records=32, max_chars=20_000),
        wire_audit=build_wire_audit_compat(
            bundle=load_prompt_bundle(),
            operation=StageOperation.EXTRACT,
            metadata_chars=128,
            payload_body_chars=len(segment.text.encode("utf-8")),
        ),
    )
    payload = request.model_dump(mode="json")
    assert "manifest" not in payload
    assert "segments" not in payload.get("source", {})


@pytest.mark.asyncio
async def test_checker_partial_leaves_failed_status() -> None:
    bundle = build_s01_s06_bundle()
    reader = _reader_from_specs(SEGMENT_SPECS)
    provider = DeterministicMeetingEssenceProvider(gold=bundle, fail_check=True)
    engine = MeetingEssenceEngine(provider=provider)
    result = await engine.run_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        publication_boundary_sequence=6,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
    )
    assert result.checkpoint.forward_semantic_check_status == CheckStatus.FAILED
    assert result.checkpoint.backward_semantic_check_status == CheckStatus.NOT_STARTED


@pytest.mark.asyncio
async def test_missing_sequence_surfaces_partial_coverage() -> None:
    specs = list(SEGMENT_SPECS)
    specs.pop(2)
    reader = _reader_from_specs(specs)
    reader.freeze_publication_boundary(6)
    _, report = materialize_manifest_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
        page_size=2,
    )
    assert not report.ok
    assert report.missing_sequences


@pytest.mark.asyncio
async def test_fail_check_does_not_report_succeeded() -> None:
    bundle = build_s01_s06_bundle()
    reader = _reader_from_specs(SEGMENT_SPECS)
    provider = DeterministicMeetingEssenceProvider(gold=bundle, fail_check=True)
    engine = MeetingEssenceEngine(provider=provider)
    result = await engine.run_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        publication_boundary_sequence=6,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
    )
    assert result.processing_status == ProcessingJobStatus.PARTIAL
    assert result.checkpoint.forward_semantic_check_status == CheckStatus.FAILED


@pytest.mark.asyncio
async def test_provider_enforces_max_wire_bytes() -> None:
    bundle = build_s01_s06_bundle()
    reader = _reader_from_specs(SEGMENT_SPECS)
    provider = DeterministicMeetingEssenceProvider(gold=bundle, max_wire_utf8_bytes=1)
    engine = MeetingEssenceEngine(provider=provider)
    result = await engine.run_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        publication_boundary_sequence=6,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
    )
    assert result.processing_status == ProcessingJobStatus.FAILED
    assert result.checkpoint.last_error_category == "wire_budget"
    blob = " ".join(
        [
            result.checkpoint.last_error_message or "",
            *(call.error or "" for call in result.trace.stage_calls),
        ]
    )
    assert "wire budget exceeded:" not in blob


@pytest.mark.asyncio
async def test_checkpoint_resume_skips_completed_extract() -> None:
    bundle = build_s01_s06_bundle()
    reader = _reader_from_specs(SEGMENT_SPECS)
    provider = DeterministicMeetingEssenceProvider(gold=bundle)
    engine = MeetingEssenceEngine(provider=provider)
    first = await engine.run_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        publication_boundary_sequence=6,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
    )
    second = await engine.run_from_reader(
        _reader_from_specs(SEGMENT_SPECS),
        session_id=FIXTURE_SESSION_ID,
        publication_boundary_sequence=6,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
        checkpoint=first.checkpoint,
    )
    assert second.processing_status == ProcessingJobStatus.SUCCEEDED
    assert all(call.operation.value != "extract" for call in second.trace.stage_calls)
