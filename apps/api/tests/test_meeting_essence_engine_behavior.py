from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from meeting_intelligence_fixtures import (
    FIXTURE_SESSION_ID,
    SEGMENT_SPECS,
    build_s01_s06_bundle,
)

from recantor.meeting_essence.budget import (
    EngineLimits,
    estimate_schema_utf8_bytes,
    fits_wire_budget,
    stabilize_audited_request,
    stub_wire_audit,
)
from recantor.meeting_essence.check_binding import (
    OFFLINE_FIXTURE_METHOD,
    SOURCE_AWARE_METHOD,
    bind_check_page,
    receipt_acknowledges,
    relation_page_covers_citations,
)
from recantor.meeting_essence.checkpoint_binding import CheckpointIncompatible
from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CheckStatus,
    DeadlineNormalizationBasis,
    EvidenceReference,
    ProcessingJobStatus,
    RecordContentStatus,
    RecordKind,
    RecordRelation,
    RecordRelationType,
    SourceCoverageFlags,
    StageOperation,
    UnicodeTextSpan,
)
from recantor.meeting_essence.deterministic_provider import (
    STRUCTURAL_CHECK_METHOD,
    DeterministicMeetingEssenceProvider,
)
from recantor.meeting_essence.engine import EngineRunConfig, MeetingEssenceEngine, engine_trace_json
from recantor.meeting_essence.engine_support import merge_check_status
from recantor.meeting_essence.identity import compute_record_identity, segment_content_hash
from recantor.meeting_essence.persistence.checkpoint_store import (
    deserialize_checkpoint,
    serialize_checkpoint,
)
from recantor.meeting_essence.progress import EngineCheckpoint
from recantor.meeting_essence.prompt_loader import load_prompt_bundle
from recantor.meeting_essence.semantic_check import (
    FIXTURE_ANNOTATION_METHOD,
    evaluate_fixture_annotations,
    relation_signature,
    request_page_supported,
)
from recantor.meeting_essence.source_reader import (
    CanonicalSegmentInput,
    FrozenSourceExpectation,
    InMemoryCanonicalSourceReader,
    SourceReadPage,
    materialize_manifest_from_reader,
    plan_source_wire,
    split_segment_fragments,
)
from recantor.meeting_essence.stage_contracts import (
    CheckPass,
    CheckStageRequest,
    CheckStageResponse,
    ExtractStageRequest,
    ExtractStageResponse,
    ReconcileStageResponse,
    RenderStageResponse,
    RequestBudget,
    evidence_provenance_key,
)
from recantor.meeting_essence.wire_payload import SourceDescriptor, WireSegmentFragment
from recantor.meeting_essence.wire_serialize import (
    schema_json_utf8_bytes,
    serialized_utf8_byte_length,
)


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


def _record(
    *,
    topic: str,
    kind: RecordKind,
    status: RecordContentStatus,
    claim: str,
    segment_id: str,
    start_ms: int,
    end_ms: int,
    text_span: tuple[int, int] | None = None,
    assignee: str | None = None,
    undetermined: bool = False,
    deadline_raw: str | None = None,
    unmentioned: bool = False,
) -> AtomicRecord:
    evidence = EvidenceReference(
        segment_id=UUID(segment_id),
        start_ms=start_ms,
        end_ms=end_ms,
        text_span=None
        if text_span is None
        else UnicodeTextSpan(start_cp=text_span[0], end_cp=text_span[1]),
    )
    return AtomicRecord(
        record_id=compute_record_identity(topic, kind.value, segment_id, claim),
        topic=topic,
        kind=kind,
        claim_text=claim,
        content_status=status,
        evidence=[evidence],
        assignee=assignee,
        assignee_explicitly_undetermined=undetermined,
        deadline_raw=deadline_raw,
        deadline_unmentioned=unmentioned,
    )


async def _run(provider: DeterministicMeetingEssenceProvider, reader, **kwargs):
    coverage = kwargs.pop("coverage", SourceCoverageFlags(transcript_publication_complete=True))
    config = kwargs.pop("config", None)
    checkpoint = kwargs.pop("checkpoint", None)
    metadata = kwargs.pop("metadata", None)
    boundary = kwargs.pop("boundary")
    session_id = kwargs.pop("session_id", FIXTURE_SESSION_ID)
    engine = MeetingEssenceEngine(provider=provider, config=config)
    return await engine.run_from_reader(
        reader,
        session_id=session_id,
        publication_boundary_sequence=boundary,
        coverage=coverage,
        meeting_metadata=metadata,
        checkpoint=checkpoint,
    )


def test_merge_check_status_does_not_erase_not_started() -> None:
    assert (
        merge_check_status(CheckStatus.COMPLETED, CheckStatus.NOT_STARTED)
        == CheckStatus.IN_PROGRESS
    )
    assert merge_check_status(CheckStatus.COMPLETED, CheckStatus.FAILED) == CheckStatus.FAILED
    assert merge_check_status(CheckStatus.FAILED, CheckStatus.COMPLETED) == CheckStatus.FAILED
    assert (
        merge_check_status(CheckStatus.NOT_STARTED, CheckStatus.COMPLETED) == CheckStatus.COMPLETED
    )


def test_schema_budget_uses_real_response_schema_and_fixed_point() -> None:
    measured = schema_json_utf8_bytes(ExtractStageResponse)
    assert estimate_schema_utf8_bytes(StageOperation.EXTRACT, ExtractStageResponse) == measured
    assert measured > 5_000
    bundle = build_s01_s06_bundle()
    segment = bundle.manifest.segments[0]
    source = SourceDescriptor(
        session_id=bundle.manifest.session_id,
        snapshot_id=bundle.manifest.snapshot_id,
        snapshot_content_hash=bundle.manifest.snapshot_content_hash,
        publication_boundary_sequence=bundle.manifest.publication_boundary_sequence,
        enumerated_segment_count=1,
    )
    draft = ExtractStageRequest(
        source=source,
        segment_fragments=(split_segment_fragments(segment, max_chars=4000)[0],),
        budget=RequestBudget(max_segments=4, max_records=8, max_chars=128_000),
        wire_audit=stub_wire_audit(),
    )
    limits = EngineLimits()
    fitted, audit = stabilize_audited_request(
        draft,
        bundle=load_prompt_bundle(),
        operation=StageOperation.EXTRACT,
        response_model=ExtractStageResponse,
        limits=limits,
        cardinality=1,
        unicode_payload_codepoints=len(segment.text),
    )
    assert audit.transport_utf8_bytes == serialized_utf8_byte_length(fitted)
    assert audit.schema_utf8_bytes == measured
    assert audit.context_input_token_upper_bound >= audit.transport_utf8_bytes
    assert audit.unicode_payload_codepoints == len(segment.text)
    assert audit.token_upper_bound >= audit.context_input_token_upper_bound
    assert fits_wire_budget(audit, limits)
    emoji = "👋"
    assert len(emoji.encode("utf-8")) > len(emoji)
    assert audit.estimation_method.startswith("utf8-byte-token-upper-bound")


def test_engine_limits_reject_non_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        EngineLimits(max_wire_utf8_bytes_per_call=0)


@pytest.mark.asyncio
async def test_s01_annotation_check_and_no_gold_result_injection() -> None:
    bundle = build_s01_s06_bundle()
    poison = bundle.result.overview_bullets[0].model_copy(update={"text": "POISON_GOLD_OVERVIEW"})
    poisoned = bundle.model_copy(
        update={
            "result": bundle.result.model_copy(
                update={"overview_bullets": [poison, *bundle.result.overview_bullets[1:]]}
            )
        }
    )
    result = await _run(
        DeterministicMeetingEssenceProvider(gold=poisoned),
        _reader_from_specs(SEGMENT_SPECS),
        boundary=6,
    )
    assert result.processing_status == ProcessingJobStatus.SUCCEEDED
    assert result.layered_result is not None
    rendered = " ".join(bullet.text for bullet in result.layered_result.overview_bullets)
    assert "POISON_GOLD_OVERVIEW" not in rendered
    assert "Selasa" in rendered
    assert 1 <= len(result.layered_result.overview_bullets) <= 7
    assert result.layered_result.coverage.semantic_check_method == OFFLINE_FIXTURE_METHOD
    assert result.layered_result.coverage.forward_semantic_check_status == CheckStatus.COMPLETED
    assert result.layered_result.coverage.backward_semantic_check_status == CheckStatus.COMPLETED
    friday = next(
        record for record in result.checkpoint.atomic_records if "Jumat" in record.claim_text
    )
    decision = next(
        record for record in result.checkpoint.atomic_records if "Selasa" in record.claim_text
    )
    assert friday.content_status == RecordContentStatus.PROPOSAL
    assert decision.content_status == RecordContentStatus.DECISION
    assert decision.condition_text
    dini = next(action for action in result.layered_result.actions if action.assignee == "Dini")
    assert dini.deadline_raw == "besok pukul 12.00"
    assert dini.deadline_normalized is None
    assert dini.content_status == RecordContentStatus.ACCEPTANCE
    assert result.layered_result.coverage.source_segments_enumerated == 6
    assert result.layered_result.coverage.source_segments_referenced == len(
        {
            ref.segment_id
            for record in result.layered_result.atomic_records
            for ref in record.evidence
        }
    )
    assert len(result.checkpoint.inspected_segment_ids) == 6
    for call in result.trace.stage_calls:
        assert call.observed_model is None
        assert call.wire_audit is not None
        assert call.serialized_utf8_bytes is not None
        assert call.serialized_utf8_bytes <= EngineLimits().max_wire_utf8_bytes_per_call
        assert call.wire_audit.token_upper_bound <= EngineLimits().max_context_tokens
        assert "segments" not in call.wire_audit.model_dump()


@pytest.mark.asyncio
async def test_long_source_derived_start_middle_end() -> None:
    start_claim = "START_FACT_ALPHA dibuka."
    middle_claim = "MIDDLE_FACT_BETA dicatat."
    end_claim = "END_FACT_OMEGA"
    total = 130_000
    middle_at = 64_010
    end_tail = "\r\n" + end_claim + " 👋"
    end_at = total - len(end_tail)
    assert len(start_claim) < 4_000
    assert total // 3 < middle_at < (2 * total) // 3
    assert middle_at + len(middle_claim) < end_at
    chars = ["x"] * total
    chars[0 : len(start_claim)] = list(start_claim)
    chars[middle_at : middle_at + len(middle_claim)] = list(middle_claim)
    chars[end_at : end_at + len(end_tail)] = list(end_tail)
    huge = "".join(chars)
    assert len(huge) > 120_000
    assert "\r\n" in huge and "👋" in huge
    omega_at = end_at + len("\r\n")
    assert huge[omega_at : omega_at + len(end_claim)] == end_claim
    segment_id = str(uuid4())
    start_record = _record(
        topic="Awal",
        kind=RecordKind.FINDING,
        status=RecordContentStatus.FINDING,
        claim=start_claim,
        segment_id=segment_id,
        start_ms=0,
        end_ms=total,
        text_span=(0, len(start_claim)),
    )
    middle_record = _record(
        topic="Tengah",
        kind=RecordKind.FINDING,
        status=RecordContentStatus.FINDING,
        claim=middle_claim,
        segment_id=segment_id,
        start_ms=0,
        end_ms=total,
        text_span=(middle_at, middle_at + len(middle_claim)),
    )
    end_record = _record(
        topic="Akhir",
        kind=RecordKind.DECISION,
        status=RecordContentStatus.DECISION,
        claim=end_claim,
        segment_id=segment_id,
        start_ms=0,
        end_ms=total,
        text_span=(omega_at, omega_at + len(end_claim)),
    )
    records = [start_record, middle_record, end_record]
    provider = DeterministicMeetingEssenceProvider(
        extraction_records=[record.model_copy(deep=True) for record in records],
        evaluation_records=[record.model_copy(deep=True) for record in records],
    )
    provider.extract_by_segment = {UUID(segment_id): list(provider.extraction_records)}
    result = await _run(provider, _reader_from_specs([(segment_id, 1, 0, total, huge)]), boundary=1)
    assert result.processing_status == ProcessingJobStatus.SUCCEEDED
    assert result.layered_result is not None
    detail = " ".join(
        point.text for topic in result.layered_result.topics for point in topic.points
    )
    assert start_claim in detail
    assert middle_claim in detail
    assert end_claim in detail
    end_facts = [
        record for record in result.checkpoint.atomic_records if record.claim_text == end_claim
    ]
    assert len(end_facts) == 1
    assert end_facts[0].evidence[0].text_span is not None
    assert end_facts[0].evidence[0].text_span.start_cp == omega_at
    spans: list[tuple[int, int, str, str]] = []
    for sample in result.trace.wire_payload_samples:
        if sample["stage"] != "extract":
            continue
        for span in sample["fragment_spans"]:  # type: ignore[union-attr]
            spans.append(
                (
                    int(span["start_cp"]),
                    int(span["end_cp"]),
                    str(span["content_hash"]),
                    str(span["segment_id"]),
                )
            )
    assert spans
    spans.sort()
    cursor = 0
    segment = result.manifest.segments[0]
    for start, end, digest, seg in spans:
        assert start == cursor
        assert end > start
        assert digest == segment.content_hash
        assert seg == str(segment.id)
        cursor = end
    assert cursor == len(huge)
    assert any(start <= middle_at < end for start, end, _digest, _seg in spans)
    assert segment_content_hash(huge) == segment.content_hash
    assert result.layered_result.coverage.source_segments_enumerated == 1
    assert result.layered_result.coverage.source_segments_referenced == 1
    assert len(result.checkpoint.inspected_segment_ids) == 1
    limits = EngineLimits()
    for call in result.trace.stage_calls:
        assert call.serialized_utf8_bytes is not None
        assert call.serialized_utf8_bytes <= limits.max_wire_utf8_bytes_per_call
        assert call.wire_audit is not None
        assert call.wire_audit.token_upper_bound <= limits.max_context_tokens
        assert call.wire_audit.schema_utf8_bytes > 0


@pytest.mark.asyncio
async def test_dense_items_remain_accessible_across_render_pages() -> None:
    claims = [f"Tindak {index:02d} dicatat lengkap." for index in range(70)]
    text = " ".join(claims)
    assert len(text) < 4_000
    segment_id = str(uuid4())
    records = [
        _record(
            topic="Tindak",
            kind=RecordKind.ACTION,
            status=RecordContentStatus.REQUEST,
            claim=claim,
            segment_id=segment_id,
            start_ms=0,
            end_ms=1_000,
            undetermined=True,
            unmentioned=True,
        )
        for claim in claims
    ]
    provider = DeterministicMeetingEssenceProvider(
        extraction_records=[record.model_copy(deep=True) for record in records],
        evaluation_records=[record.model_copy(deep=True) for record in records],
    )
    provider.extract_by_segment = {UUID(segment_id): list(provider.extraction_records)}
    result = await _run(
        provider,
        _reader_from_specs([(segment_id, 1, 0, 1_000, text)]),
        boundary=1,
    )
    assert result.processing_status == ProcessingJobStatus.SUCCEEDED
    assert result.layered_result is not None
    found = {action.task for action in result.layered_result.actions}
    assert found == set(claims)
    assert len(result.layered_result.overview_bullets) <= 7
    render_samples = [
        sample for sample in result.trace.wire_payload_samples if sample["stage"] == "render"
    ]
    assert render_samples
    assert all(int(sample["record_count"]) <= 64 for sample in render_samples)
    assert sum(int(sample["record_count"]) for sample in render_samples) >= 70
    extract_calls = [
        call for call in result.trace.stage_calls if call.operation == StageOperation.EXTRACT
    ]
    assert len(extract_calls) >= 2


@pytest.mark.asyncio
async def test_hanging_provider_stops_at_job_deadline() -> None:
    class Hanging:
        def __init__(self) -> None:
            self.calls = 0
            self.active = 0
            self.cancel_acknowledged = 0

        async def run_stage(self, **kwargs: object) -> object:
            del kwargs
            self.calls += 1
            self.active += 1
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancel_acknowledged += 1
                raise
            finally:
                self.active -= 1
            raise AssertionError("hang should not return")

    provider = Hanging()
    started = time.monotonic()
    result = await _run(
        provider,  # type: ignore[arg-type]
        _reader_from_specs(SEGMENT_SPECS),
        boundary=6,
        config=EngineRunConfig(
            max_wall_clock_seconds=0.05,
            max_attempts_per_stage=3,
            limits=EngineLimits(provider_retry_backoff_seconds=0.05, max_provider_call_seconds=5),
        ),
    )
    elapsed = time.monotonic() - started
    assert provider.calls == 1
    assert provider.active == 0
    assert provider.cancel_acknowledged >= 1
    assert elapsed < 1.0
    assert result.checkpoint.last_error_category is None
    assert any(item.code == "wall_clock_exhausted" for item in result.limitations)
    assert result.processing_status == ProcessingJobStatus.PARTIAL


@pytest.mark.asyncio
async def test_provider_errors_are_sanitized_and_not_retried_blindly() -> None:
    class Leak:
        def __init__(self) -> None:
            self.calls = 0

        async def run_stage(self, **kwargs: object) -> object:
            del kwargs
            self.calls += 1
            raise RuntimeError("SYNTHETIC_PRIVATE_DIAGNOSTIC token sk-LIVESECRET")

    leak = Leak()
    leaked = await _run(leak, _reader_from_specs(SEGMENT_SPECS), boundary=6)  # type: ignore[arg-type]
    blob = engine_trace_json(leaked.trace) + (leaked.checkpoint.last_error_message or "")
    blob += " ".join(item.message for item in leaked.limitations)
    assert leak.calls == 1
    assert leaked.checkpoint.last_error_category == "unknown"
    assert leaked.checkpoint.last_error_message == "provider call failed"
    assert "SYNTHETIC_PRIVATE_DIAGNOSTIC" not in blob
    assert "sk-LIVESECRET" not in blob

    class Auth:
        def __init__(self) -> None:
            self.calls = 0

        async def run_stage(self, **kwargs: object) -> object:
            del kwargs
            self.calls += 1
            raise RuntimeError("auth rejected token sk-LIVESECRET")

    auth = Auth()
    authed = await _run(auth, _reader_from_specs(SEGMENT_SPECS), boundary=6)  # type: ignore[arg-type]
    auth_blob = engine_trace_json(authed.trace)
    assert auth.calls == 1
    assert authed.checkpoint.last_error_category == "auth"
    assert "sk-LIVESECRET" not in auth_blob
    assert authed.trace.stage_calls[0].attempt_count == 1


def _continuation_source():
    spec = SEGMENT_SPECS[0]
    segment_id, _, start_ms, end_ms, _ = spec
    records = [
        _record(
            topic="Boundary",
            kind=RecordKind.FINDING,
            status=RecordContentStatus.FINDING,
            claim=f"Butir {index}.",
            segment_id=segment_id,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        for index in range(5)
    ]
    return [spec], records


@pytest.mark.asyncio
async def test_extract_deadline_before_invocation_does_not_complete_page(monkeypatch) -> None:
    specs, records = _continuation_source()
    provider = DeterministicMeetingEssenceProvider(
        extraction_records=records,
        extract_by_segment={UUID(specs[0][0]): records},
    )
    engine = MeetingEssenceEngine(provider=provider)
    expired = False
    fit = engine._fit_extract_page

    def _fit_then_expire(*args, **kwargs):
        nonlocal expired
        fitted = fit(*args, **kwargs)
        expired = True
        return fitted

    monkeypatch.setattr(engine, "_fit_extract_page", _fit_then_expire)
    monkeypatch.setattr(engine, "_deadline_passed", lambda _: expired)
    first = await engine.run_from_reader(
        _reader_from_specs(specs),
        session_id=FIXTURE_SESSION_ID,
        publication_boundary_sequence=1,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
    )
    assert provider.call_count == 0
    assert not first.checkpoint.extract_complete
    assert first.checkpoint.extract_fragment_cursor == 0
    assert first.checkpoint.completed_fragment_indices == ()
    resumed = await _run(
        provider, _reader_from_specs(specs), boundary=1, checkpoint=first.checkpoint
    )
    assert resumed.checkpoint.extract_complete
    assert {record.record_id for record in resumed.checkpoint.atomic_records} == {
        record.record_id for record in records
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_at_deadline", [True, False])
async def test_extract_full_cardinality_continuation_survives_turn(
    monkeypatch, stop_at_deadline: bool
) -> None:
    specs, records = _continuation_source()
    provider = DeterministicMeetingEssenceProvider(
        extraction_records=records,
        extract_by_segment={UUID(specs[0][0]): records},
    )
    limits = EngineLimits(
        max_records_per_extract_response=2,
        max_extract_continuations=8 if stop_at_deadline else 1,
    )
    checkpoint = None
    for accepted_count in (2, 4, 5):
        expired = [False]

        async def _after_accept(saved, deadline=expired):
            if saved.atomic_records and (
                stop_at_deadline or len(saved.atomic_records) == len(records)
            ):
                deadline[0] = True

        engine = MeetingEssenceEngine(
            provider=provider,
            config=EngineRunConfig(limits=limits, on_checkpoint_committed=_after_accept),
        )
        monkeypatch.setattr(engine, "_deadline_passed", lambda _, deadline=expired: deadline[0])
        result = await engine.run_from_reader(
            _reader_from_specs(specs),
            session_id=FIXTURE_SESSION_ID,
            publication_boundary_sequence=1,
            coverage=SourceCoverageFlags(transcript_publication_complete=True),
            checkpoint=checkpoint,
        )
        checkpoint = result.checkpoint
        assert len(checkpoint.atomic_records) == accepted_count
        assert checkpoint.extract_fragment_cursor == (1 if accepted_count == 5 else 0)
        assert checkpoint.completed_fragment_indices == ((0,) if accepted_count == 5 else ())
        checkpoint = deserialize_checkpoint(serialize_checkpoint(checkpoint))
        assert checkpoint is not None
    assert checkpoint.extract_complete
    assert provider.call_count == 3
    assert {record.record_id for record in checkpoint.atomic_records} == {
        record.record_id for record in records
    }


@pytest.mark.asyncio
async def test_exhausted_extract_checkpoint_resumes_without_replaying_page() -> None:
    specs, records = _continuation_source()
    provider = DeterministicMeetingEssenceProvider(
        extraction_records=records,
        extract_by_segment={UUID(specs[0][0]): records},
    )
    saved_body = None

    class StoppedAfterCommit(Exception):
        pass

    async def _stop_after_commit(saved):
        nonlocal saved_body
        if saved.atomic_records:
            saved_body = serialize_checkpoint(saved)
            raise StoppedAfterCommit

    with pytest.raises(StoppedAfterCommit):
        await _run(
            provider,
            _reader_from_specs(specs),
            boundary=1,
            config=EngineRunConfig(on_checkpoint_committed=_stop_after_commit),
        )
    assert saved_body is not None
    checkpoint = deserialize_checkpoint(saved_body)
    assert checkpoint is not None
    assert checkpoint.extract_fragment_cursor == 1
    assert checkpoint.completed_fragment_indices == (0,)
    resumed = await _run(provider, _reader_from_specs(specs), boundary=1, checkpoint=checkpoint)
    assert resumed.checkpoint.extract_complete
    assert not any(call.operation == StageOperation.EXTRACT for call in resumed.trace.stage_calls)
    assert {record.record_id for record in resumed.checkpoint.atomic_records} == {
        record.record_id for record in records
    }


@pytest.mark.asyncio
async def test_checkpoint_fail_closed_and_mid_extract_resume() -> None:
    bundle = build_s01_s06_bundle()
    limits = EngineLimits(max_segments_per_extract_page=1, context_overlap_segments=1)

    class AuthAfter(DeterministicMeetingEssenceProvider):
        async def run_stage(self, **kwargs: object) -> object:
            if self.call_count >= 1:
                self.call_count += 1
                raise RuntimeError("auth rejected")
            return await super().run_stage(**kwargs)  # type: ignore[arg-type]

    first_provider = AuthAfter(gold=bundle)
    first = await _run(
        first_provider,
        _reader_from_specs(SEGMENT_SPECS),
        boundary=6,
        config=EngineRunConfig(limits=limits),
    )
    assert first.checkpoint.extract_fragment_cursor == 1
    assert first.checkpoint.atomic_records
    assert not first.checkpoint.extract_complete
    saved_ids = [record.record_id for record in first.checkpoint.atomic_records]
    resumed = await _run(
        DeterministicMeetingEssenceProvider(gold=bundle),
        _reader_from_specs(SEGMENT_SPECS),
        boundary=6,
        config=EngineRunConfig(limits=limits),
        checkpoint=first.checkpoint,
    )
    assert resumed.processing_status == ProcessingJobStatus.SUCCEEDED
    resume_sequences = [
        sequence
        for sample in resumed.trace.wire_payload_samples
        if sample.get("stage") == "extract"
        for sequence in sample.get("fragment_sequences", [])
    ]
    assert 1 not in resume_sequences
    assert len(resumed.checkpoint.atomic_records) == len(
        {record.record_id for record in resumed.checkpoint.atomic_records}
    )
    assert set(saved_ids) <= {record.record_id for record in resumed.checkpoint.atomic_records}

    done = await _run(
        DeterministicMeetingEssenceProvider(gold=bundle),
        _reader_from_specs(SEGMENT_SPECS),
        boundary=6,
        config=EngineRunConfig(limits=limits),
        checkpoint=resumed.checkpoint,
    )
    assert done.trace.stage_calls == []
    assert done.processing_status == ProcessingJobStatus.SUCCEEDED

    foreign = resumed.checkpoint
    stamp = foreign.updated_at
    ids_before = [record.record_id for record in foreign.atomic_records]
    foreign.snapshot_content_hash = "f" * 64
    with pytest.raises(CheckpointIncompatible):
        await _run(
            DeterministicMeetingEssenceProvider(gold=bundle),
            _reader_from_specs(SEGMENT_SPECS),
            boundary=6,
            config=EngineRunConfig(limits=limits),
            checkpoint=foreign,
        )
    assert [record.record_id for record in foreign.atomic_records] == ids_before
    assert foreign.updated_at == stamp

    unbound = EngineCheckpoint(atomic_records=[bundle.records[0].model_copy(deep=True)])
    with pytest.raises(CheckpointIncompatible, match="unbound"):
        await _run(
            DeterministicMeetingEssenceProvider(gold=bundle),
            _reader_from_specs(SEGMENT_SPECS),
            boundary=6,
            checkpoint=unbound,
        )
    assert unbound.atomic_records[0].record_id == bundle.records[0].record_id


@pytest.mark.asyncio
async def test_malicious_extract_rejected_before_merge() -> None:
    bundle = build_s01_s06_bundle()
    good = bundle.records[0]
    foreign = good.model_copy(
        update={
            "record_id": "rec_foreign_segment",
            "claim_text": "fakta asing",
            "evidence": [
                EvidenceReference(segment_id=uuid4(), start_ms=1, end_ms=2),
            ],
        }
    )
    mistimed = good.model_copy(
        update={
            "record_id": "rec_bad_times",
            "evidence": [
                EvidenceReference(segment_id=good.evidence[0].segment_id, start_ms=1, end_ms=2),
            ],
        }
    )
    owned = good.model_copy(update={"record_id": "rec_bad_owner", "assignee": "Zorro"})
    dated = good.model_copy(
        update={
            "record_id": "rec_bad_date",
            "deadline_raw": "Jumat",
            "deadline_normalized": datetime(2026, 10, 17, tzinfo=UTC),
            "deadline_normalization_basis": DeadlineNormalizationBasis.ABSOLUTE_SOURCE,
        }
    )
    conflict_a = good.model_copy(deep=True)
    conflict_b = good.model_copy(update={"claim_text": "klaim bertentangan"})

    class Scripted(DeterministicMeetingEssenceProvider):
        def __init__(self, scripted: list[AtomicRecord]) -> None:
            super().__init__(gold=bundle)
            self.scripted = scripted

        async def run_stage(self, **kwargs: object) -> object:
            if kwargs["operation"] == StageOperation.EXTRACT:
                self.call_count += 1
                self._enforce_wire_budget(kwargs["request"])  # type: ignore[arg-type]
                return ExtractStageResponse(extracted_records=list(self.scripted))
            return await super().run_stage(**kwargs)  # type: ignore[arg-type]

    for scripted in ([foreign], [mistimed], [owned], [dated], [conflict_a, conflict_b]):
        result = await _run(Scripted(scripted), _reader_from_specs(SEGMENT_SPECS), boundary=6)
        assert result.processing_status != ProcessingJobStatus.SUCCEEDED
        assert all(
            record.record_id != scripted[0].record_id for record in result.checkpoint.atomic_records
        )
        if scripted[0].assignee == "Zorro":
            assert all(record.assignee != "Zorro" for record in result.checkpoint.atomic_records)


@pytest.mark.asyncio
async def test_dropped_fact_and_changed_status_fail_annotation_check() -> None:
    bundle = build_s01_s06_bundle()
    invoice_id = next(
        record.record_id for record in bundle.records if "Invoice" in record.claim_text
    )
    dropper = DeterministicMeetingEssenceProvider(gold=bundle, drop_record_ids={invoice_id})
    dropped = await _run(dropper, _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert dropped.processing_status == ProcessingJobStatus.PARTIAL
    assert dropped.checkpoint.backward_semantic_check_status == CheckStatus.FAILED
    rendered = ""
    if dropped.layered_result is not None:
        rendered = " ".join(
            point.text for topic in dropped.layered_result.topics for point in topic.points
        )
    assert "Invoice" not in rendered

    class Mutate(DeterministicMeetingEssenceProvider):
        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] == StageOperation.RENDER and response.layered_result.actions:
                action = response.layered_result.actions[0].model_copy(
                    update={
                        "content_status": RecordContentStatus.REPORTED_COMPLETION,
                        "condition_text": "syarat yang tidak ada di sumber",
                    }
                )
                return RenderStageResponse(
                    layered_result=response.layered_result.model_copy(
                        update={"actions": [action, *response.layered_result.actions[1:]]}
                    )
                )
            return response

    class _NoCache:
        async def get_validated_extraction(self, **kwargs: object) -> None:
            del kwargs
            return None

    from recantor.meeting_essence.persistence.caching_provider import (
        CachedMeetingEssenceProvider,
    )

    wrapped = CachedMeetingEssenceProvider(
        inner=DeterministicMeetingEssenceProvider(
            gold=bundle, drop_record_ids={bundle.records[-1].record_id}
        ),
        repository=_NoCache(),  # type: ignore[arg-type]
        session_id=str(FIXTURE_SESSION_ID),
        run_fingerprint="0" * 64,
        source_content_hash="0" * 64,
        provider_adapter_id="deterministic-v1",
        prompt_cache_key="prompt",
        limits_fingerprint="limits",
    )
    wrapped_result = await _run(wrapped, _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert wrapped_result.processing_status == ProcessingJobStatus.PARTIAL
    assert wrapped_result.checkpoint.backward_semantic_check_status == CheckStatus.FAILED
    assert wrapped.evaluation_records is not None

    mutated = await _run(Mutate(gold=bundle), _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert mutated.processing_status == ProcessingJobStatus.PARTIAL
    assert mutated.checkpoint.forward_semantic_check_status == CheckStatus.FAILED
    assert mutated.layered_result is not None
    assert any(
        action.content_status == RecordContentStatus.REPORTED_COMPLETION
        for action in mutated.layered_result.actions
    )


@pytest.mark.asyncio
async def test_disagreement_stays_open_and_distant_acceptance_does_not_rewrite_proposal() -> None:
    proposal_id = str(uuid4())
    objection_id = str(uuid4())
    proposal = _record(
        topic="Rilis",
        kind=RecordKind.PROPOSAL,
        status=RecordContentStatus.PROPOSAL,
        claim="Usulan rilis Jumat tetap dibahas.",
        segment_id=proposal_id,
        start_ms=0,
        end_ms=1_000,
    )
    objection = _record(
        topic="Rilis",
        kind=RecordKind.OBJECTION,
        status=RecordContentStatus.UNRESOLVED,
        claim="Keberatan rilis Jumat belum disepakati.",
        segment_id=objection_id,
        start_ms=1_000,
        end_ms=2_000,
    )
    relation = RecordRelation(
        relation_type=RecordRelationType.CONTRADICTS,
        from_record_id=objection.record_id,
        to_record_id=proposal.record_id,
        evidence=list(objection.evidence),
    )
    provider = DeterministicMeetingEssenceProvider(
        extraction_records=[proposal.model_copy(deep=True), objection.model_copy(deep=True)],
        evaluation_records=[proposal.model_copy(deep=True), objection.model_copy(deep=True)],
        relation_annotations=[relation],
    )
    provider.extract_by_segment = {
        UUID(proposal_id): [provider.extraction_records[0]],
        UUID(objection_id): [provider.extraction_records[1]],
    }
    disagreed = await _run(
        provider,
        _reader_from_specs(
            [
                (proposal_id, 1, 0, 1_000, proposal.claim_text),
                (objection_id, 2, 1_000, 2_000, objection.claim_text),
            ]
        ),
        boundary=2,
    )
    assert disagreed.processing_status == ProcessingJobStatus.SUCCEEDED
    assert disagreed.layered_result is not None
    assert disagreed.layered_result.open_items
    assert disagreed.layered_result.actions == []
    assert all(
        record.content_status != RecordContentStatus.DECISION
        for record in disagreed.checkpoint.atomic_records
    )

    early_id = str(uuid4())
    late_id = str(uuid4())
    fillers = [
        (str(uuid4()), index, index * 1000, index * 1000 + 500, f"pengisi {index}")
        for index in range(2, 12)
    ]
    early = _record(
        topic="Jadwal",
        kind=RecordKind.PROPOSAL,
        status=RecordContentStatus.PROPOSAL,
        claim="Usulan rapat Selasa pagi.",
        segment_id=early_id,
        start_ms=0,
        end_ms=500,
    )
    late = _record(
        topic="Jadwal",
        kind=RecordKind.ACTION,
        status=RecordContentStatus.ACCEPTANCE,
        claim="Penerimaan rapat Selasa pagi.",
        segment_id=late_id,
        start_ms=20_000,
        end_ms=21_000,
        assignee="Dini",
    )
    accepts = RecordRelation(
        relation_type=RecordRelationType.ACCEPTS,
        from_record_id=late.record_id,
        to_record_id=early.record_id,
        evidence=list(late.evidence),
    )
    distant = DeterministicMeetingEssenceProvider(
        extraction_records=[early.model_copy(deep=True), late.model_copy(deep=True)],
        evaluation_records=[early.model_copy(deep=True), late.model_copy(deep=True)],
        relation_annotations=[accepts],
    )
    distant.extract_by_segment = {
        UUID(early_id): [distant.extraction_records[0]],
        UUID(late_id): [distant.extraction_records[1]],
    }
    specs = [(early_id, 1, 0, 500, early.claim_text + " Dini hadir.")] + fillers
    specs.append((late_id, 12, 20_000, 21_000, late.claim_text + " Dini menerima."))
    reader = _reader_from_specs(specs)
    result = await _run(
        distant,
        reader,
        boundary=12,
        config=EngineRunConfig(
            limits=EngineLimits(max_segments_per_extract_page=1, context_overlap_segments=0)
        ),
    )
    assert result.processing_status == ProcessingJobStatus.SUCCEEDED
    proposal_row = next(
        record for record in result.checkpoint.atomic_records if record.record_id == early.record_id
    )
    assert proposal_row.content_status == RecordContentStatus.PROPOSAL
    assert any(
        record.content_status == RecordContentStatus.ACCEPTANCE
        for record in result.checkpoint.atomic_records
    )
    assert any(
        rel.relation_type == RecordRelationType.ACCEPTS for rel in result.checkpoint.relations
    )
    reader.freeze_publication_boundary(12)
    reader.inject_post_freeze_segment(
        CanonicalSegmentInput(
            id=uuid4(),
            sequence=99,
            start_ms=50_000,
            end_ms=51_000,
            text="Late Friday utterance must not override Tuesday.",
        )
    )
    _, report = materialize_manifest_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
    )
    assert report.ok
    assert all("Late Friday" not in segment.text for segment in result.manifest.segments)


@pytest.mark.asyncio
async def test_informational_findings_do_not_invent_actions() -> None:
    first = str(uuid4())
    second = str(uuid4())
    finding = _record(
        topic="Riset",
        kind=RecordKind.FINDING,
        status=RecordContentStatus.FINDING,
        claim="Adopsi naik 12 persen.",
        segment_id=first,
        start_ms=0,
        end_ms=1_000,
    )
    unresolved = _record(
        topic="Opsi",
        kind=RecordKind.UNRESOLVED_QUESTION,
        status=RecordContentStatus.UNRESOLVED,
        claim="Belum ada keputusan akhir.",
        segment_id=second,
        start_ms=1_000,
        end_ms=2_000,
    )
    provider = DeterministicMeetingEssenceProvider(
        extraction_records=[finding.model_copy(deep=True), unresolved.model_copy(deep=True)],
        evaluation_records=[finding.model_copy(deep=True), unresolved.model_copy(deep=True)],
    )
    provider.extract_by_segment = {
        UUID(first): [provider.extraction_records[0]],
        UUID(second): [provider.extraction_records[1]],
    }
    result = await _run(
        provider,
        _reader_from_specs(
            [
                (first, 1, 0, 1_000, finding.claim_text),
                (second, 2, 1_000, 2_000, unresolved.claim_text),
            ]
        ),
        boundary=2,
    )
    assert result.processing_status == ProcessingJobStatus.SUCCEEDED
    assert result.layered_result is not None
    assert result.layered_result.actions == []
    assert result.layered_result.open_items
    assert result.checkpoint.semantic_check_method == OFFLINE_FIXTURE_METHOD


def test_gap_pages_cover_full_span_and_huge_boundary_is_bounded() -> None:
    bundle = build_s01_s06_bundle()
    manifest = bundle.manifest.model_copy(
        update={
            "coverage": SourceCoverageFlags(
                transcript_publication_complete=False,
                explicit_gaps=[],
            )
        }
    )
    from recantor.meeting_essence.contracts import TranscriptGapMarker

    manifest = manifest.model_copy(
        update={
            "coverage": SourceCoverageFlags(
                explicit_gaps=[
                    TranscriptGapMarker(sequence_start=1, sequence_end=25_000, reason="gap")
                ]
            )
        }
    )
    plan = plan_source_wire(
        manifest,
        max_explicit_gaps=3,
        max_gap_sequence_span=10_000,
        max_meeting_metadata_keys=8,
        max_metadata_value_utf8_bytes=100,
    )
    ranges = [
        (gap.sequence_start, gap.sequence_end)
        for page in plan.pages
        for gap in page.coverage.explicit_gaps
    ]
    assert ranges[0][0] == 1
    assert ranges[-1][1] == 25_000
    covered = []
    for start, end in ranges:
        covered.append((start, end))
        assert end - start <= 10_000
    assert covered[0][0] == 1
    cursor = 1
    for start, end in covered:
        assert start == cursor
        cursor = end + 1
    assert cursor == 25_001

    reader = _reader_from_specs([(str(uuid4()), 1, 0, 10, "satu")])
    started = time.monotonic()
    _manifest, report = materialize_manifest_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
    )
    reader.freeze_publication_boundary(5_000_000)
    _manifest, report = materialize_manifest_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
    )
    assert time.monotonic() - started < 1.0
    assert not report.ok
    assert report.uncovered_spans
    assert report.uncovered_spans[-1][1] == 5_000_000
    assert len(report.missing_sequences) < 100


def test_malformed_reader_is_explicit_partial() -> None:
    class BadReader:
        def freeze_publication_boundary(self, boundary_sequence: int) -> FrozenSourceExpectation:
            del boundary_sequence
            return FrozenSourceExpectation(1, (), (), ())

        def frozen_expectation(self) -> FrozenSourceExpectation:
            return FrozenSourceExpectation(1, (), (), ())

        def read_segments_page(self, *, page_index: int, page_size: int) -> SourceReadPage:
            del page_index, page_size
            raise RuntimeError("reader exploded PRIVATE_ROW")

    reader = BadReader()
    reader.freeze_publication_boundary(1)
    _manifest, report = materialize_manifest_from_reader(
        reader,
        session_id=FIXTURE_SESSION_ID,
        coverage=SourceCoverageFlags(transcript_publication_complete=True),
    )
    assert not report.ok
    assert "PRIVATE_ROW" not in report.message


@pytest.mark.asyncio
async def test_metadata_pages_are_not_silently_clipped() -> None:
    bundle = build_s01_s06_bundle()
    metadata = {f"k{index}": f"v{index}" for index in range(3)}
    result = await _run(
        DeterministicMeetingEssenceProvider(gold=bundle),
        _reader_from_specs(SEGMENT_SPECS),
        boundary=6,
        metadata=metadata,
        config=EngineRunConfig(limits=EngineLimits(max_meeting_metadata_keys=1)),
    )
    assert result.processing_status == ProcessingJobStatus.SUCCEEDED
    seen: set[str] = set()
    for sample in result.trace.wire_payload_samples:
        seen.update(sample["metadata_keys"])  # type: ignore[arg-type]
    assert seen == set(metadata)
    huge = "x" * 50 + "TAILMARK"
    omitted = await _run(
        DeterministicMeetingEssenceProvider(gold=bundle),
        _reader_from_specs(SEGMENT_SPECS),
        boundary=6,
        metadata={"note": huge},
        config=EngineRunConfig(limits=EngineLimits(max_metadata_value_utf8_bytes=16)),
    )
    assert omitted.processing_status == ProcessingJobStatus.PARTIAL
    assert any(item.code == "metadata_value_exceeds_budget" for item in omitted.limitations)
    assert "TAILMARK" not in str(omitted.trace.wire_payload_samples)


def test_closed_world_annotations_reject_extra_facts_and_hidden_ledger() -> None:
    bundle = build_s01_s06_bundle()
    display = _display_from_annotations(bundle.records, bundle.relations, bundle.result.snapshot_id)
    hidden = display.model_construct(
        contract_version=display.contract_version,
        snapshot_id=display.snapshot_id,
        overview_bullets=display.overview_bullets,
        topics=[],
        actions=[],
        open_items=[],
        atomic_records=display.atomic_records,
        relations=display.relations,
        coverage=display.coverage,
    )
    hidden_check = evaluate_fixture_annotations(
        bundle.manifest,
        display.atomic_records,
        display.relations,
        hidden,
        expected_records=bundle.records,
        expected_relations=bundle.relations,
    )
    assert hidden_check.backward == CheckStatus.FAILED
    assert "missing-display-detail" in hidden_check.notes
    extra = bundle.records[-1].model_copy(
        update={
            "record_id": "unsupported_record_5",
            "claim_text": "Dewan menyetujui anggaran Rp 9 miliar untuk proyek rahasia.",
        }
    )
    extra_display = _display_from_annotations(
        [*bundle.records, extra], bundle.relations, bundle.result.snapshot_id
    )
    extra_check = evaluate_fixture_annotations(
        bundle.manifest,
        extra_display.atomic_records,
        extra_display.relations,
        extra_display,
        expected_records=bundle.records,
        expected_relations=bundle.relations,
    )
    assert extra_check.forward == CheckStatus.FAILED
    assert "unsupported-extra-ledger" in extra_check.notes
    honest = evaluate_fixture_annotations(
        bundle.manifest,
        display.atomic_records,
        display.relations,
        display,
        expected_records=bundle.records,
        expected_relations=bundle.relations,
    )
    assert honest.forward == CheckStatus.COMPLETED
    assert honest.backward == CheckStatus.COMPLETED
    assert honest.method == FIXTURE_ANNOTATION_METHOD


def _display_from_annotations(records, relations, snapshot_id):
    from recantor.meeting_essence.render_builder import build_layered_from_ledger

    return build_layered_from_ledger(
        snapshot_id=snapshot_id,
        enumerated_segment_count=6,
        records=records,
        relations=relations,
        processing_status=ProcessingJobStatus.RUNNING,
        forward_check=CheckStatus.NOT_STARTED,
        backward_check=CheckStatus.NOT_STARTED,
        semantic_check_method=None,
    )


@pytest.mark.asyncio
async def test_injected_extra_claim_does_not_succeed() -> None:
    bundle = build_s01_s06_bundle()
    secret = "Dewan menyetujui anggaran Rp 9 miliar untuk proyek rahasia."

    class Inject(DeterministicMeetingEssenceProvider):
        def _records_for_fragments(self, request):  # type: ignore[no-untyped-def]
            emitted = super()._records_for_fragments(request)
            visible = {fragment.segment_id for fragment in request.segment_fragments}
            template = next(
                record for record in self.extraction_records if "Invoice" in record.claim_text
            )
            if template.evidence[0].segment_id in visible and all(
                record.record_id != "unsupported_record_5" for record in emitted
            ):
                emitted.append(
                    template.model_copy(
                        update={"record_id": "unsupported_record_5", "claim_text": secret}
                    )
                )
            return emitted

    result = await _run(Inject(gold=bundle), _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert result.processing_status == ProcessingJobStatus.PARTIAL
    assert result.checkpoint.forward_semantic_check_status == CheckStatus.FAILED
    assert result.layered_result is not None
    rendered = " ".join(action.task for action in result.layered_result.actions)
    assert secret in rendered
    check_texts = [
        text
        for sample in result.trace.wire_payload_samples
        if sample["stage"] == "check"
        for text in sample["output_claim_texts"]  # type: ignore[union-attr]
    ]
    assert secret in check_texts
    assert any(
        int(sample["source_text_codepoints"]) > 0
        for sample in result.trace.wire_payload_samples
        if sample["stage"] == "check"
    )


@pytest.mark.asyncio
async def test_display_mutation_fails_annotation_support() -> None:
    bundle = build_s01_s06_bundle()
    result = await _run(
        DeterministicMeetingEssenceProvider(gold=bundle),
        _reader_from_specs(SEGMENT_SPECS),
        boundary=6,
    )
    assert result.processing_status == ProcessingJobStatus.SUCCEEDED
    assert result.layered_result is not None
    altered = result.layered_result.model_copy(
        update={
            "actions": [
                result.layered_result.actions[0].model_copy(
                    update={
                        "content_status": RecordContentStatus.REPORTED_COMPLETION,
                        "assignee": "BukanDini",
                        "condition_text": "syarat baru",
                        "deadline_normalized": datetime(2026, 10, 20, tzinfo=UTC),
                        "deadline_normalization_basis": DeadlineNormalizationBasis.ABSOLUTE_SOURCE,
                        "evidence": [
                            EvidenceReference(
                                segment_id=uuid4(),
                                start_ms=1,
                                end_ms=2,
                            )
                        ],
                    }
                ),
                *result.layered_result.actions[1:],
            ]
        }
    )
    changed = evaluate_fixture_annotations(
        result.manifest,
        result.checkpoint.atomic_records,
        result.checkpoint.relations,
        altered,
        expected_records=bundle.records,
        expected_relations=bundle.relations,
    )
    assert changed.forward == CheckStatus.FAILED
    topics = []
    for topic in result.layered_result.topics:
        points = [point for point in topic.points if "Invoice" not in point.text]
        if points:
            topics.append(topic.model_copy(update={"points": points}))
    removed = result.layered_result.model_copy(update={"topics": topics})
    recall = evaluate_fixture_annotations(
        result.manifest,
        result.checkpoint.atomic_records,
        result.checkpoint.relations,
        removed,
        expected_records=bundle.records,
        expected_relations=bundle.relations,
    )
    assert recall.backward == CheckStatus.FAILED
    assert "missing-display-detail" in recall.notes


@pytest.mark.asyncio
async def test_source_aware_provider_succeeds_without_fixture_gold() -> None:
    segment_id = str(uuid4())
    claim = "Rapat mencatat kenaikan adopsi 12 persen."
    record = _record(
        topic="Riset",
        kind=RecordKind.FINDING,
        status=RecordContentStatus.FINDING,
        claim=claim,
        segment_id=segment_id,
        start_ms=0,
        end_ms=1_000,
    )

    class ReadingProvider(DeterministicMeetingEssenceProvider):
        def __init__(self) -> None:
            super().__init__(
                extraction_records=[record.model_copy(deep=True)],
                evaluation_records=None,
            )
            self.extract_by_segment = {UUID(segment_id): list(self.extraction_records)}
            self.seen: list[CheckStageRequest] = []

        async def run_stage(self, **kwargs: object) -> object:
            if kwargs["operation"] != StageOperation.CHECK:
                return await super().run_stage(**kwargs)  # type: ignore[arg-type]
            request = kwargs["request"]
            assert isinstance(request, CheckStageRequest)
            self.seen.append(request)
            binding = request.result_binding
            grounded = all(
                any(claim_text in fragment.text for fragment in request.source_fragments)
                for claim_text in [
                    *(item.claim_text for item in request.atomic_records),
                    *(item.text for item in request.output_claims),
                ]
            )
            status = CheckStatus.COMPLETED if grounded else CheckStatus.FAILED
            return CheckStageResponse(
                forward_semantic_check_status=status,
                backward_semantic_check_status=status,
                semantic_check_method=SOURCE_AWARE_METHOD,
                check_pass=request.check_pass,
                direction=request.direction,
                covered_fragment_keys=binding.source_fragment_keys,
                covered_claim_ids=binding.output_claim_ids,
                covered_record_ids=binding.record_ids,
                covered_relation_keys=binding.relation_keys,
                request_fingerprint=binding.request_fingerprint,
                source_fingerprint=binding.source_fingerprint,
                result_fingerprint=binding.result_fingerprint,
            )

    provider = ReadingProvider()
    assert provider.evaluation_records is None
    result = await _run(
        provider,
        _reader_from_specs([(segment_id, 1, 0, 1_000, claim)]),
        boundary=1,
    )
    assert result.processing_status == ProcessingJobStatus.SUCCEEDED
    assert result.checkpoint.semantic_check_method == SOURCE_AWARE_METHOD
    assert provider.seen
    assert any(
        fragment.text == claim for request in provider.seen for fragment in request.source_fragments
    )
    assert any(item.text == claim for request in provider.seen for item in request.output_claims)
    assert any(
        item.surface == "overview" for request in provider.seen for item in request.output_claims
    )

    class Ungrounded(ReadingProvider):
        def _records_for_fragments(self, request):  # type: ignore[no-untyped-def]
            emitted = super()._records_for_fragments(request)
            template = self.extraction_records[0]
            if template.evidence[0].segment_id in {
                fragment.segment_id for fragment in request.segment_fragments
            }:
                emitted.append(
                    template.model_copy(
                        update={
                            "record_id": "unsupported_record_5",
                            "claim_text": (
                                "Dewan menyetujui anggaran Rp 9 miliar untuk proyek rahasia."
                            ),
                        }
                    )
                )
            return emitted

    poisoned = await _run(
        Ungrounded(),
        _reader_from_specs([(segment_id, 1, 0, 1_000, claim)]),
        boundary=1,
    )
    assert poisoned.processing_status == ProcessingJobStatus.PARTIAL
    assert poisoned.checkpoint.forward_semantic_check_status == CheckStatus.FAILED


@pytest.mark.asyncio
async def test_skipped_check_page_is_not_overridden_by_annotations() -> None:
    bundle = build_s01_s06_bundle()

    class Drop(DeterministicMeetingEssenceProvider):
        def __init__(self, drop_at: int) -> None:
            super().__init__(gold=bundle)
            self.drop_at = drop_at
            self.checks = 0

        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] != StageOperation.CHECK:
                return response
            index = self.checks
            self.checks += 1
            if index != self.drop_at:
                return response
            assert isinstance(response, CheckStageResponse)
            return response.model_copy(
                update={
                    "forward_semantic_check_status": CheckStatus.NOT_STARTED,
                    "backward_semantic_check_status": CheckStatus.NOT_STARTED,
                    "covered_fragment_keys": (),
                    "covered_claim_ids": (),
                }
            )

    probe = await _run(Drop(10_000), _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert probe.processing_status == ProcessingJobStatus.SUCCEEDED
    total_checks = sum(
        1 for call in probe.trace.stage_calls if call.operation == StageOperation.CHECK
    )
    assert total_checks >= 3
    for drop_at in (0, total_checks // 2, total_checks - 1):
        dropped = await _run(Drop(drop_at), _reader_from_specs(SEGMENT_SPECS), boundary=6)
        assert dropped.processing_status == ProcessingJobStatus.PARTIAL
        assert dropped.checkpoint.forward_semantic_check_status != CheckStatus.COMPLETED
        assert dropped.layered_result is not None


@pytest.mark.asyncio
async def test_structural_method_and_bad_fingerprint_do_not_succeed() -> None:
    bundle = build_s01_s06_bundle()

    class Structural(DeterministicMeetingEssenceProvider):
        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] == StageOperation.CHECK:
                assert isinstance(response, CheckStageResponse)
                return response.model_copy(
                    update={
                        "semantic_check_method": "deterministic-structural-ledger-v1",
                        "forward_semantic_check_status": CheckStatus.COMPLETED,
                        "backward_semantic_check_status": CheckStatus.COMPLETED,
                    }
                )
            return response

    structural = await _run(Structural(gold=bundle), _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert structural.processing_status == ProcessingJobStatus.PARTIAL
    assert structural.checkpoint.semantic_check_method is None

    class BadFingerprint(DeterministicMeetingEssenceProvider):
        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] == StageOperation.CHECK:
                assert isinstance(response, CheckStageResponse)
                return response.model_copy(update={"request_fingerprint": "0" * 64})
            return response

    mismatched = await _run(
        BadFingerprint(gold=bundle), _reader_from_specs(SEGMENT_SPECS), boundary=6
    )
    assert mismatched.processing_status == ProcessingJobStatus.PARTIAL
    assert mismatched.checkpoint.forward_semantic_check_status == CheckStatus.FAILED


_SECRET = "Dewan menyetujui anggaran Rp 9 miliar untuk proyek rahasia."


def _append_secret(provider: DeterministicMeetingEssenceProvider, request, emitted):  # type: ignore[no-untyped-def]
    template = next(
        record for record in provider.extraction_records if "Invoice" in record.claim_text
    )
    if template.evidence[0].segment_id not in {
        fragment.segment_id for fragment in request.segment_fragments
    }:
        return emitted
    if any(record.record_id == "unsupported_record_5" for record in emitted):
        return emitted
    emitted.append(
        template.model_copy(update={"record_id": "unsupported_record_5", "claim_text": _SECRET})
    )
    return emitted


def test_fingerprint_binds_fields_cursor_and_exact_receipt() -> None:
    segment_id = uuid4()
    text = "Fakta awal tetap di sumber."
    fragment = WireSegmentFragment(
        segment_id=segment_id,
        sequence=1,
        start_ms=0,
        end_ms=1_000,
        text=text,
        content_hash=segment_content_hash(text),
    )
    record = _record(
        topic="Rilis",
        kind=RecordKind.FINDING,
        status=RecordContentStatus.FINDING,
        claim=text,
        segment_id=str(segment_id),
        start_ms=0,
        end_ms=1_000,
    )
    binding = bind_check_page(
        snapshot_content_hash="a" * 64,
        check_pass=CheckPass.SOURCE_COVERAGE,
        fragments=[fragment],
        claims=[],
        records=[record],
        relations=[],
        page_cursor="source_coverage:0:0:0:0",
    )
    changed = record.model_copy(update={"claim_text": "Fakta yang tidak ada di sumber."})
    changed_binding = bind_check_page(
        snapshot_content_hash="a" * 64,
        check_pass=CheckPass.SOURCE_COVERAGE,
        fragments=[fragment],
        claims=[],
        records=[changed],
        relations=[],
        page_cursor=binding.page_cursor,
    )
    later_record = record.model_copy(update={"record_id": "later-record"})
    later = bind_check_page(
        snapshot_content_hash="a" * 64,
        check_pass=CheckPass.OUTPUT_COVERAGE,
        fragments=[fragment],
        claims=[],
        records=[later_record],
        relations=[],
        page_cursor="output_coverage:1:0:0:0",
    )
    assert binding.request_fingerprint != changed_binding.request_fingerprint
    assert binding.result_fingerprint != changed_binding.result_fingerprint
    assert binding.request_fingerprint != later.request_fingerprint
    assert receipt_acknowledges(
        binding,
        covered_fragment_keys=binding.source_fragment_keys,
        covered_claim_ids=binding.output_claim_ids,
        covered_record_ids=binding.record_ids,
        covered_relation_keys=binding.relation_keys,
    )
    assert not receipt_acknowledges(
        later,
        covered_fragment_keys=binding.source_fragment_keys,
        covered_claim_ids=binding.output_claim_ids,
        covered_record_ids=binding.record_ids,
        covered_relation_keys=binding.relation_keys,
    )
    assert not receipt_acknowledges(
        binding,
        covered_fragment_keys=(),
        covered_claim_ids=(),
        covered_record_ids=(),
        covered_relation_keys=(),
    )


@pytest.mark.asyncio
async def test_no_gold_structural_provider_reports_structural_method() -> None:
    bundle = build_s01_s06_bundle()

    class Poison(DeterministicMeetingEssenceProvider):
        def __init__(self) -> None:
            super().__init__(gold=bundle)
            self.evaluation_records = None
            self.methods: list[str] = []

        def _records_for_fragments(self, request):  # type: ignore[no-untyped-def]
            return _append_secret(self, request, super()._records_for_fragments(request))

        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] == StageOperation.CHECK:
                assert isinstance(response, CheckStageResponse)
                self.methods.append(response.semantic_check_method)
            return response

    provider = Poison()
    result = await _run(provider, _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert provider.methods
    assert set(provider.methods) == {STRUCTURAL_CHECK_METHOD}
    assert result.processing_status == ProcessingJobStatus.PARTIAL
    assert result.checkpoint.semantic_check_method is None
    assert result.checkpoint.forward_semantic_check_status != CheckStatus.COMPLETED
    assert result.layered_result is not None
    assert _SECRET in " ".join(action.task for action in result.layered_result.actions)


@pytest.mark.asyncio
async def test_unsupported_overview_is_retained_and_partial() -> None:
    bundle = build_s01_s06_bundle()

    class OverviewPoison(DeterministicMeetingEssenceProvider):
        def __init__(self) -> None:
            super().__init__(gold=bundle)
            self.check_requests: list[CheckStageRequest] = []

        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            operation = kwargs["operation"]
            if operation == StageOperation.CHECK:
                request = kwargs["request"]
                assert isinstance(request, CheckStageRequest)
                self.check_requests.append(request)
                return response
            if operation != StageOperation.RENDER:
                return response
            assert isinstance(response, RenderStageResponse)
            layered = response.layered_result
            bullet = layered.overview_bullets[0].model_copy(update={"text": _SECRET})
            poisoned = layered.model_copy(
                update={"overview_bullets": [bullet, *layered.overview_bullets[1:]]}
            )
            return response.model_copy(update={"layered_result": poisoned})

    provider = OverviewPoison()
    result = await _run(provider, _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert result.processing_status == ProcessingJobStatus.PARTIAL
    assert result.checkpoint.forward_semantic_check_status == CheckStatus.FAILED
    assert result.layered_result is not None
    overview = " ".join(bullet.text for bullet in result.layered_result.overview_bullets)
    assert _SECRET in overview
    assert any(
        claim.surface == "overview" and claim.text == _SECRET
        for request in provider.check_requests
        for claim in request.output_claims
    )
    assert any(
        fragment.text
        for request in provider.check_requests
        for fragment in request.source_fragments
    )
    hidden = result.layered_result.model_copy(
        update={"topics": [], "actions": [], "open_items": []}
    )
    recall = evaluate_fixture_annotations(
        result.manifest,
        result.checkpoint.atomic_records,
        result.checkpoint.relations,
        hidden,
        expected_records=bundle.records,
        expected_relations=bundle.relations,
    )
    assert recall.backward == CheckStatus.FAILED
    assert "missing-display-detail" in recall.notes


@pytest.mark.asyncio
async def test_empty_or_stale_output_ack_and_unregistered_method_stay_partial() -> None:
    bundle = build_s01_s06_bundle()

    class EmptyOutput(DeterministicMeetingEssenceProvider):
        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] != StageOperation.CHECK:
                return response
            request = kwargs["request"]
            assert isinstance(request, CheckStageRequest)
            assert isinstance(response, CheckStageResponse)
            if request.check_pass != CheckPass.OUTPUT_COVERAGE:
                return response
            return response.model_copy(
                update={
                    "covered_fragment_keys": (),
                    "covered_claim_ids": (),
                    "covered_record_ids": (),
                    "covered_relation_keys": (),
                }
            )

    empty = await _run(EmptyOutput(gold=bundle), _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert empty.processing_status == ProcessingJobStatus.PARTIAL
    assert empty.checkpoint.forward_semantic_check_status != CheckStatus.COMPLETED
    assert empty.checkpoint.output_claim_forward == {}
    assert empty.checkpoint.source_fragment_forward
    assert empty.layered_result is not None

    class StaleOutput(DeterministicMeetingEssenceProvider):
        def __init__(self) -> None:
            super().__init__(gold=bundle)
            self.previous: CheckStageResponse | None = None

        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] != StageOperation.CHECK:
                return response
            request = kwargs["request"]
            assert isinstance(request, CheckStageRequest)
            assert isinstance(response, CheckStageResponse)
            if request.check_pass != CheckPass.OUTPUT_COVERAGE:
                return response
            previous = self.previous
            self.previous = response
            if previous is None:
                return response
            return response.model_copy(
                update={
                    "covered_fragment_keys": previous.covered_fragment_keys,
                    "covered_claim_ids": previous.covered_claim_ids,
                    "covered_record_ids": previous.covered_record_ids,
                    "covered_relation_keys": previous.covered_relation_keys,
                }
            )

    stale = await _run(
        StaleOutput(),
        _reader_from_specs(SEGMENT_SPECS),
        boundary=6,
        config=EngineRunConfig(limits=EngineLimits(max_output_claims_per_check=1)),
    )
    assert stale.processing_status == ProcessingJobStatus.PARTIAL
    assert stale.checkpoint.forward_semantic_check_status != CheckStatus.COMPLETED

    class Banana(DeterministicMeetingEssenceProvider):
        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] == StageOperation.CHECK:
                assert isinstance(response, CheckStageResponse)
                return response.model_copy(update={"semantic_check_method": "banana"})
            return response

    banana = await _run(Banana(gold=bundle), _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert banana.processing_status == ProcessingJobStatus.PARTIAL
    assert banana.checkpoint.semantic_check_method is None


@pytest.mark.asyncio
async def test_mid_check_resume_does_not_repeat_acked_subpages() -> None:
    bundle = build_s01_s06_bundle()
    limits = EngineLimits(
        max_records_per_check_page=1,
        max_output_claims_per_check=1,
        max_relations_on_check_page=1,
    )
    config = EngineRunConfig(limits=limits)

    class Stop(DeterministicMeetingEssenceProvider):
        def __init__(self) -> None:
            super().__init__(gold=bundle)
            self.seen: list[str] = []
            self.stop_after: int | None = 2

        async def run_stage(self, **kwargs: object) -> object:
            if kwargs["operation"] == StageOperation.CHECK:
                request = kwargs["request"]
                assert isinstance(request, CheckStageRequest)
                if self.stop_after is not None and len(self.seen) >= self.stop_after:
                    raise RuntimeError("config boundary")
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] == StageOperation.CHECK:
                request = kwargs["request"]
                assert isinstance(request, CheckStageRequest)
                self.seen.append(request.result_binding.request_fingerprint)
            return response

    provider = Stop()
    reader = _reader_from_specs(SEGMENT_SPECS)
    stopped = await _run(provider, reader, boundary=6, config=config)
    assert stopped.processing_status == ProcessingJobStatus.PARTIAL
    acked = set(stopped.checkpoint.acked_check_fingerprints)
    assert acked == set(provider.seen)
    assert len(acked) == 2
    provider.stop_after = None
    resumed = await _run(
        provider,
        reader,
        boundary=6,
        config=config,
        checkpoint=stopped.checkpoint,
    )
    repeated = [fingerprint for fingerprint in provider.seen[2:] if fingerprint in acked]
    assert repeated == []
    assert resumed.processing_status == ProcessingJobStatus.SUCCEEDED
    assert resumed.checkpoint.check_complete
    assert len(provider.seen) > 2


_S03 = UUID("11111111-1111-4111-8111-111111111103")
_S06 = UUID("11111111-1111-4111-8111-111111111106")


def _swap_relation_evidence(response: object, evidence: list[EvidenceReference]) -> object:
    assert isinstance(response, ReconcileStageResponse)
    return response.model_copy(
        update={
            "relations": [
                relation.model_copy(
                    update={"evidence": [item.model_copy(deep=True) for item in evidence]}
                )
                for relation in response.relations
            ]
        }
    )


@pytest.mark.asyncio
async def test_s01_relation_check_carries_citation_source_and_both_endpoints() -> None:
    bundle = build_s01_s06_bundle()
    relation = bundle.relations[0]
    segment = next(item for item in bundle.manifest.segments if item.id == _S03)

    class Capture(DeterministicMeetingEssenceProvider):
        def __init__(self) -> None:
            super().__init__(gold=bundle)
            self.checks: list[CheckStageRequest] = []

        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] == StageOperation.CHECK:
                request = kwargs["request"]
                assert isinstance(request, CheckStageRequest)
                self.checks.append(request)
            return response

    provider = Capture()
    result = await _run(provider, _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert result.processing_status == ProcessingJobStatus.SUCCEEDED
    relation_pages = [request for request in provider.checks if request.relations]
    assert relation_pages
    assert all(request.check_pass == CheckPass.RELATION_CONTEXT for request in relation_pages)
    for request in relation_pages:
        cited = [fragment for fragment in request.source_fragments if fragment.segment_id == _S03]
        assert cited
        assert cited[0].content_hash == segment.content_hash
        assert (cited[0].start_ms, cited[0].end_ms) == (segment.start_ms, segment.end_ms)
        assert "Selasa" in cited[0].text
        ids = {record.record_id for record in request.atomic_records}
        assert relation.from_record_id in ids
        assert relation.to_record_id in ids
        assert request.result_binding.judged_evidence_keys
    other_pages = [request for request in provider.checks if not request.relations]
    assert other_pages
    assert all(not request.relations for request in other_pages)


def test_relation_support_rejects_wrong_hash_time_span_and_absent_source() -> None:
    bundle = build_s01_s06_bundle()
    relation = bundle.relations[0]
    segment = next(item for item in bundle.manifest.segments if item.id == _S03)
    records = [
        record
        for record in bundle.records
        if record.record_id in {relation.from_record_id, relation.to_record_id}
    ]
    good = WireSegmentFragment(
        segment_id=segment.id,
        sequence=segment.sequence,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        text=segment.text,
        content_hash=segment.content_hash,
        text_span_start_cp=0,
        text_span_end_cp=len(segment.text),
    )
    judged = tuple(evidence_provenance_key(item) for item in relation.evidence)
    assert relation_page_covers_citations([relation], records, [good], judged, bundle.manifest)
    bad_hash = good.model_copy(update={"content_hash": "ab" * 32})
    assert not relation_page_covers_citations(
        [relation], records, [bad_hash], judged, bundle.manifest
    )
    bad_time = good.model_copy(update={"start_ms": 1, "end_ms": 2})
    assert not relation_page_covers_citations(
        [relation], records, [bad_time], judged, bundle.manifest
    )
    assert not relation_page_covers_citations([relation], records, [], judged, bundle.manifest)
    wrong_span = relation.model_copy(
        update={
            "evidence": [
                relation.evidence[0].model_copy(
                    update={"text_span": UnicodeTextSpan(start_cp=0, end_cp=4)}
                )
            ]
        }
    )
    assert relation_signature(wrong_span, bundle.manifest) != relation_signature(
        relation, bundle.manifest
    )
    assert not request_page_supported(
        records=records,
        relations=[wrong_span],
        claims=[],
        fragments=[good],
        expected_records=bundle.records,
        expected_relations=bundle.relations,
        manifest=bundle.manifest,
        judged_evidence_keys=judged,
    )


@pytest.mark.asyncio
async def test_wrong_relation_citation_stays_partial_even_if_checker_completes() -> None:
    bundle = build_s01_s06_bundle()
    s06_evidence = [item.model_copy(deep=True) for item in bundle.records[-1].evidence]

    class CiteS06(DeterministicMeetingEssenceProvider):
        def __init__(self, *, lie: bool) -> None:
            super().__init__(gold=bundle)
            self.lie = lie
            self.relation_status: list[CheckStatus] = []
            self.cited_segments: list[UUID] = []

        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] == StageOperation.RECONCILE:
                return _swap_relation_evidence(response, s06_evidence)
            if kwargs["operation"] != StageOperation.CHECK:
                return response
            request = kwargs["request"]
            assert isinstance(request, CheckStageRequest)
            assert isinstance(response, CheckStageResponse)
            if not request.relations:
                return response
            self.relation_status.append(response.forward_semantic_check_status)
            self.cited_segments.extend(fragment.segment_id for fragment in request.source_fragments)
            if not self.lie:
                return response
            binding = request.result_binding
            return response.model_copy(
                update={
                    "forward_semantic_check_status": CheckStatus.COMPLETED,
                    "backward_semantic_check_status": CheckStatus.COMPLETED,
                    "covered_fragment_keys": binding.source_fragment_keys,
                    "covered_claim_ids": binding.output_claim_ids,
                    "covered_record_ids": binding.record_ids,
                    "covered_relation_keys": binding.relation_keys,
                }
            )

    honest = CiteS06(lie=False)
    honest_result = await _run(honest, _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert honest.relation_status == [CheckStatus.FAILED]
    assert _S06 in honest.cited_segments
    assert _S03 not in honest.cited_segments
    assert honest_result.processing_status == ProcessingJobStatus.PARTIAL
    assert honest_result.checkpoint.forward_semantic_check_status == CheckStatus.FAILED
    assert honest_result.checkpoint.relations
    assert honest_result.checkpoint.relations[0].evidence[0].segment_id == _S06
    assert honest_result.checkpoint.relations[0].relation_type == bundle.relations[0].relation_type
    assert (
        honest_result.checkpoint.relations[0].from_record_id == bundle.relations[0].from_record_id
    )
    assert honest_result.layered_result is not None
    rendered = " ".join(
        point.text for topic in honest_result.layered_result.topics for point in topic.points
    )
    assert "Selasa" in rendered
    assert "Jumat" in rendered

    lied = CiteS06(lie=True)
    lied_result = await _run(lied, _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert lied_result.processing_status == ProcessingJobStatus.PARTIAL
    assert lied_result.checkpoint.forward_semantic_check_status == CheckStatus.FAILED
    annotation = evaluate_fixture_annotations(
        lied_result.manifest,
        lied_result.checkpoint.atomic_records,
        lied_result.checkpoint.relations,
        lied_result.layered_result,
        expected_records=bundle.records,
        expected_relations=bundle.relations,
    )
    assert annotation.forward == CheckStatus.FAILED
    assert "unsupported-extra-relation" in annotation.notes


@pytest.mark.asyncio
async def test_relation_evidence_span_or_time_change_stays_partial() -> None:
    bundle = build_s01_s06_bundle()

    class Mutate(DeterministicMeetingEssenceProvider):
        def __init__(self, evidence: list[EvidenceReference]) -> None:
            super().__init__(gold=bundle)
            self.evidence = evidence

        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] == StageOperation.RECONCILE:
                return _swap_relation_evidence(response, self.evidence)
            return response

    span = (
        bundle.relations[0]
        .evidence[0]
        .model_copy(update={"text_span": UnicodeTextSpan(start_cp=0, end_cp=4)})
    )
    spanned = await _run(Mutate([span]), _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert spanned.processing_status == ProcessingJobStatus.PARTIAL
    assert spanned.checkpoint.forward_semantic_check_status == CheckStatus.FAILED
    assert spanned.checkpoint.relations[0].from_record_id == bundle.relations[0].from_record_id
    assert spanned.checkpoint.relations[0].evidence[0].text_span is not None

    timed = bundle.relations[0].evidence[0].model_copy(update={"start_ms": 1, "end_ms": 2})
    mistimed = await _run(Mutate([timed]), _reader_from_specs(SEGMENT_SPECS), boundary=6)
    assert mistimed.processing_status == ProcessingJobStatus.PARTIAL
    assert mistimed.checkpoint.forward_semantic_check_status == CheckStatus.FAILED
    assert any(item.code == "relation_context_invalid" for item in mistimed.limitations)


@pytest.mark.asyncio
async def test_omitted_relation_companion_page_stays_partial() -> None:
    decision_claim = "Keputusan baru berlaku."
    proposal_claim = "Usulan lama tercatat."
    body = decision_claim + (" perubahan jadwal" * 6)
    width = len(body) // 3
    text = body[: width * 3]
    assert width >= len(decision_claim)
    long_id = str(uuid4())
    other_id = str(uuid4())
    decision = _record(
        topic="Jadwal",
        kind=RecordKind.DECISION,
        status=RecordContentStatus.DECISION,
        claim=decision_claim,
        segment_id=long_id,
        start_ms=0,
        end_ms=3_000,
        text_span=(0, len(decision_claim)),
    )
    proposal = _record(
        topic="Jadwal",
        kind=RecordKind.PROPOSAL,
        status=RecordContentStatus.PROPOSAL,
        claim=proposal_claim,
        segment_id=other_id,
        start_ms=3_000,
        end_ms=4_000,
    )
    relation = RecordRelation(
        relation_type=RecordRelationType.SUPERSEDES,
        from_record_id=decision.record_id,
        to_record_id=proposal.record_id,
        evidence=[
            EvidenceReference(
                segment_id=UUID(long_id),
                start_ms=0,
                end_ms=3_000,
                text_span=UnicodeTextSpan(start_cp=0, end_cp=len(text)),
            )
        ],
    )
    limits = EngineLimits(
        max_fragment_chars_per_segment=width,
        max_segments_per_extract_page=1,
        context_overlap_segments=0,
    )
    config = EngineRunConfig(limits=limits)
    specs = [
        (long_id, 1, 0, 3_000, text),
        (other_id, 2, 3_000, 4_000, proposal_claim),
    ]

    class DropSlice(DeterministicMeetingEssenceProvider):
        def __init__(self, drop_at: int) -> None:
            super().__init__(
                extraction_records=[
                    decision.model_copy(deep=True),
                    proposal.model_copy(deep=True),
                ],
                evaluation_records=[
                    decision.model_copy(deep=True),
                    proposal.model_copy(deep=True),
                ],
                relation_annotations=[relation.model_copy(deep=True)],
            )
            self.extract_by_segment = {
                UUID(long_id): [self.extraction_records[0]],
                UUID(other_id): [self.extraction_records[1]],
            }
            self.drop_at = drop_at
            self.relation_pages: list[CheckStageRequest] = []

        async def run_stage(self, **kwargs: object) -> object:
            response = await super().run_stage(**kwargs)  # type: ignore[arg-type]
            if kwargs["operation"] != StageOperation.CHECK:
                return response
            request = kwargs["request"]
            assert isinstance(request, CheckStageRequest)
            assert isinstance(response, CheckStageResponse)
            if request.check_pass != CheckPass.RELATION_CONTEXT:
                return response
            index = len(self.relation_pages)
            self.relation_pages.append(request)
            if index != self.drop_at:
                return response
            return response.model_copy(
                update={
                    "forward_semantic_check_status": CheckStatus.NOT_STARTED,
                    "backward_semantic_check_status": CheckStatus.NOT_STARTED,
                    "covered_fragment_keys": (),
                    "covered_claim_ids": (),
                    "covered_record_ids": (),
                    "covered_relation_keys": (),
                }
            )

    probe = DropSlice(10_000)
    proved = await _run(probe, _reader_from_specs(specs), boundary=2, config=config)
    assert proved.processing_status == ProcessingJobStatus.SUCCEEDED
    assert len(probe.relation_pages) == 3
    excerpts = [request.source_fragments[0].text for request in probe.relation_pages]
    assert "".join(excerpts) == text
    assert all(len(excerpt) == width for excerpt in excerpts)
    assert all(len(request.atomic_records) == 2 for request in probe.relation_pages)
    for drop_at in (0, 1, 2):
        dropped = await _run(
            DropSlice(drop_at), _reader_from_specs(specs), boundary=2, config=config
        )
        assert dropped.processing_status == ProcessingJobStatus.PARTIAL
        assert dropped.checkpoint.forward_semantic_check_status != CheckStatus.COMPLETED
        assert dropped.layered_result is not None
