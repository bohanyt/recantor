from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel

from recantor.meeting_essence.budget import (
    EngineLimits,
    fits_wire_budget,
    stabilize_audited_request,
    stub_wire_audit,
    wire_limit_bytes,
)
from recantor.meeting_essence.check_binding import (
    RelationContextPlan,
    RelationContextSlice,
    accessible_claims_from_result,
    bind_check_page,
    binding_matches_response,
    build_relation_context_slices,
    claims_touching_fragments,
    endpoint_records,
    fragment_key,
    fragment_span_record,
    is_admitted_semantic_method,
    manifest_fragments,
    receipt_acknowledges,
    records_touching_fragments,
    relation_page_covers_citations,
    support_fragments_for_claim,
)
from recantor.meeting_essence.checkpoint_binding import (
    CheckpointIncompatible,
    assert_checkpoint_compatible,
    bind_checkpoint,
    binding_from_run,
)
from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CheckStatus,
    CoverageLimitation,
    LayeredMeetingResult,
    MeetingIntelligenceJobMetadata,
    ProcessingJobStatus,
    RecordRelation,
    SourceCoverageFlags,
    SourceSnapshotManifest,
    StageOperation,
)
from recantor.meeting_essence.engine_invoke import InvokeConfig, invoke_stage
from recantor.meeting_essence.engine_support import (
    aggregate_check_status,
    combine_provider_and_annotation,
    merge_check_status,
    monotonic_deadline,
    note_check_status,
    page_records,
    relation_key,
    relations_touching,
    same_topic_dependencies,
    track_inspected_segments,
)
from recantor.meeting_essence.extract_validation import (
    merge_atomic_records,
    validate_extracted_records_for_request,
)
from recantor.meeting_essence.progress import EngineCheckpoint, StageCallTrace
from recantor.meeting_essence.prompt_loader import PromptBundle, load_prompt_bundle
from recantor.meeting_essence.provider import MeetingEssenceProvider
from recantor.meeting_essence.provider_errors import ProviderErrorCategory, StageInvocationError
from recantor.meeting_essence.render_builder import merge_rendered_pages
from recantor.meeting_essence.semantic_check import evaluate_fixture_annotations
from recantor.meeting_essence.source_reader import (
    CanonicalSourceReader,
    SourceWirePlan,
    materialize_manifest_from_reader,
    plan_source_wire,
    split_segment_fragments,
)
from recantor.meeting_essence.stage_contracts import (
    AccessibleOutputClaim,
    CheckPass,
    CheckStageRequest,
    CheckStageResponse,
    ExtractStageRequest,
    ExtractStageResponse,
    ModelInvocationMetadata,
    ReconcileStageRequest,
    ReconcileStageResponse,
    RenderStageRequest,
    RenderStageResponse,
    RequestBudget,
    StageProtocolMetadata,
    WireRecordPage,
)
from recantor.meeting_essence.validation import (
    collect_referenced_segment_ids,
    validate_result_against_manifest,
)
from recantor.meeting_essence.wire_payload import (
    SourceDescriptor,
    WirePayloadAudit,
    WireSegmentFragment,
)
from recantor.meeting_essence.wire_serialize import serialized_utf8_byte_length

RequestT = TypeVar("RequestT", bound=BaseModel)


@dataclass
class EngineRunConfig:
    limits: EngineLimits = field(default_factory=EngineLimits)
    max_wall_clock_seconds: float | None = 120.0
    max_attempts_per_stage: int = 3
    requested_model: str | None = None
    requested_reasoning_effort: str | None = None
    on_checkpoint_committed: Callable[[EngineCheckpoint], Awaitable[None]] | None = None
    on_validated_stage: Callable[[StageOperation, BaseModel, BaseModel], Awaitable[None]] | None = (
        None
    )


@dataclass
class EngineRunTrace:
    manifest: SourceSnapshotManifest
    prompt_cache_key: str
    schema_identity: str
    algorithm_identity: str
    stage_calls: list[StageCallTrace] = field(default_factory=list)
    wire_payload_samples: list[dict[str, object]] = field(default_factory=list)


@dataclass
class EngineRunResult:
    manifest: SourceSnapshotManifest
    layered_result: LayeredMeetingResult | None
    checkpoint: EngineCheckpoint
    trace: EngineRunTrace
    processing_status: ProcessingJobStatus
    limitations: list[CoverageLimitation] = field(default_factory=list)


def _records_aligned(left: list[object], right: list[object]) -> bool:
    def dumped(records: list[object]) -> dict[str, object]:
        return {
            str(record.record_id): record.model_dump(mode="json")  # type: ignore[attr-defined]
            for record in records
        }

    left_map = dumped(left)
    right_map = dumped(right)
    return left_map == right_map and len(left_map) == len(left)


def _partial_layered(checkpoint: EngineCheckpoint) -> LayeredMeetingResult | None:
    """Publish a yield as partial. Completed semantic checks stay completed.

    A render page that does not yet match the accepted ledger is withheld from
    this publication. The checkpoint keeps that render so the next quantum can
    finish it.
    """
    layered = checkpoint.layered_partial
    if layered is None:
        return None
    if not _records_aligned(list(layered.atomic_records), list(checkpoint.atomic_records)):
        return None
    if layered.coverage.processing_job_status == ProcessingJobStatus.PARTIAL:
        return layered
    updated = layered.model_copy(
        update={
            "coverage": layered.coverage.model_copy(
                update={"processing_job_status": ProcessingJobStatus.PARTIAL}
            )
        }
    )
    checkpoint.layered_partial = updated
    return updated


class MeetingEssenceEngine:
    def __init__(
        self,
        *,
        provider: MeetingEssenceProvider,
        prompt_bundle: PromptBundle | None = None,
        config: EngineRunConfig | None = None,
    ) -> None:
        self._provider = provider
        self._bundle = prompt_bundle or load_prompt_bundle()
        self._config = config or EngineRunConfig()
        self._check_manifest: SourceSnapshotManifest | None = None

    async def _commit_checkpoint(self, checkpoint: EngineCheckpoint) -> None:
        checkpoint.touch()
        callback = self._config.on_checkpoint_committed
        if callback is not None:
            await callback(checkpoint)

    async def _note_validated_stage(
        self, operation: StageOperation, request: BaseModel, response: BaseModel
    ) -> None:
        callback = self._config.on_validated_stage
        if callback is not None:
            await callback(operation, request, response)

    async def run_from_reader(
        self,
        reader: CanonicalSourceReader,
        *,
        session_id: UUID,
        publication_boundary_sequence: int,
        coverage: SourceCoverageFlags,
        meeting_metadata: dict[str, str] | None = None,
        checkpoint: EngineCheckpoint | None = None,
    ) -> EngineRunResult:
        started_monotonic = time.monotonic()
        reader.freeze_publication_boundary(publication_boundary_sequence)
        limits = self._config.limits
        manifest, coverage_report = materialize_manifest_from_reader(
            reader,
            session_id=session_id,
            coverage=coverage,
            meeting_metadata=meeting_metadata,
            max_pages=limits.max_manifest_materialization_pages,
        )
        checkpoint = checkpoint or EngineCheckpoint()
        trace = EngineRunTrace(
            manifest=manifest,
            prompt_cache_key=self._bundle.cache_key,
            schema_identity=self._bundle.schema_identity,
            algorithm_identity=self._bundle.algorithm_identity,
        )
        job = self._job_metadata(manifest)
        binding = binding_from_run(
            manifest,
            self._bundle,
            input_fingerprint=job.input_fingerprint,
            limits=limits,
            requested_model=self._config.requested_model,
            requested_reasoning_effort=self._config.requested_reasoning_effort,
        )
        assert_checkpoint_compatible(checkpoint, binding)
        limitations: list[CoverageLimitation] = []
        if not coverage_report.ok:
            if not checkpoint.binding_version:
                bind_checkpoint(checkpoint, binding)
            limitations.append(
                CoverageLimitation(code="source_coverage_gap", message=coverage_report.message)
            )
            checkpoint.processing_status = ProcessingJobStatus.PARTIAL
            layered_partial = _partial_layered(checkpoint)
            await self._commit_checkpoint(checkpoint)
            return EngineRunResult(
                manifest=manifest,
                layered_result=layered_partial,
                checkpoint=checkpoint,
                trace=trace,
                processing_status=ProcessingJobStatus.PARTIAL,
                limitations=limitations,
            )
        if self._already_complete(checkpoint):
            return EngineRunResult(
                manifest=manifest,
                layered_result=checkpoint.layered_partial,
                checkpoint=checkpoint,
                trace=trace,
                processing_status=ProcessingJobStatus.SUCCEEDED,
                limitations=[],
            )
        bind_checkpoint(checkpoint, binding)
        plan = plan_source_wire(
            manifest,
            max_explicit_gaps=limits.max_explicit_gaps_on_wire,
            max_gap_sequence_span=limits.max_gap_sequence_span,
            max_meeting_metadata_keys=limits.max_meeting_metadata_keys,
            max_metadata_value_utf8_bytes=limits.max_metadata_value_utf8_bytes,
        )
        if plan.omitted_metadata_keys:
            limitations.append(
                CoverageLimitation(
                    code="metadata_value_exceeds_budget",
                    message="one or more meeting metadata values exceed a single wire page",
                )
            )
            checkpoint.processing_status = ProcessingJobStatus.PARTIAL
        elif checkpoint.processing_status in {
            ProcessingJobStatus.PARTIAL,
            ProcessingJobStatus.FAILED,
        }:
            checkpoint.processing_status = ProcessingJobStatus.RUNNING
            checkpoint.last_error_category = None
            checkpoint.last_error_message = None
        try:
            if not checkpoint.extract_complete:
                await self._run_extract(
                    manifest, plan, job, checkpoint, trace, started_monotonic, limitations
                )
            if self._deadline_passed(started_monotonic):
                return await self._partial_result(manifest, checkpoint, trace, limitations)
            if not checkpoint.extract_complete:
                limitations.append(
                    CoverageLimitation(
                        code="extract_incomplete", message="extract stage did not finish"
                    )
                )
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                await self._commit_checkpoint(checkpoint)
                return self._result(
                    manifest, checkpoint, trace, limitations, checkpoint.layered_partial
                )

            if not checkpoint.reconcile_complete:
                await self._run_reconcile(
                    plan, job, checkpoint, trace, started_monotonic, limitations
                )
            if self._deadline_passed(started_monotonic):
                return await self._partial_result(manifest, checkpoint, trace, limitations)
            if not checkpoint.reconcile_complete:
                limitations.append(
                    CoverageLimitation(
                        code="reconcile_incomplete", message="reconcile stage did not finish"
                    )
                )
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                return self._result(
                    manifest, checkpoint, trace, limitations, checkpoint.layered_partial
                )

            layered: LayeredMeetingResult | None = checkpoint.layered_partial
            if checkpoint.atomic_records and not checkpoint.render_complete:
                layered = await self._run_render(
                    manifest, plan, job, checkpoint, trace, started_monotonic, limitations
                )
            elif not checkpoint.atomic_records:
                checkpoint.render_complete = True
                layered = None
            if self._deadline_passed(started_monotonic):
                return await self._partial_result(manifest, checkpoint, trace, limitations)
            if checkpoint.atomic_records and not checkpoint.render_complete:
                limitations.append(
                    CoverageLimitation(
                        code="render_incomplete", message="render stage did not finish"
                    )
                )
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                return self._result(
                    manifest, checkpoint, trace, limitations, checkpoint.layered_partial
                )
            if not checkpoint.check_complete:
                await self._run_semantic_checks(
                    manifest,
                    plan,
                    job,
                    checkpoint,
                    trace,
                    started_monotonic,
                    limitations,
                    layered=layered,
                )
            if self._deadline_passed(started_monotonic):
                return await self._partial_result(manifest, checkpoint, trace, limitations)
            if not checkpoint.check_complete:
                limitations.append(
                    CoverageLimitation(
                        code="check_incomplete", message="semantic check did not cover every page"
                    )
                )
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                return self._finalize(
                    manifest, checkpoint, trace, limitations, layered, plan, started_monotonic
                )
        except StageInvocationError as exc:
            if exc.quantum_expired:
                return await self._partial_result(manifest, checkpoint, trace, limitations)
            limitations.append(CoverageLimitation(code=exc.category.value, message=exc.message))
            if exc.category == ProviderErrorCategory.TIMEOUT:
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
            else:
                checkpoint.processing_status = (
                    ProcessingJobStatus.PARTIAL
                    if checkpoint.atomic_records or checkpoint.layered_partial is not None
                    else ProcessingJobStatus.FAILED
                )
            checkpoint.last_error_category = exc.category.value
            checkpoint.last_error_message = exc.message
            await self._commit_checkpoint(checkpoint)
            layered = (
                None
                if exc.category == ProviderErrorCategory.TIMEOUT
                else checkpoint.layered_partial
            )
            return self._result(manifest, checkpoint, trace, limitations, layered)

        result = self._finalize(
            manifest, checkpoint, trace, limitations, layered, plan, started_monotonic
        )
        await self._commit_checkpoint(checkpoint)
        return result

    def _already_complete(self, checkpoint: EngineCheckpoint) -> bool:
        return bool(
            checkpoint.binding_version
            and checkpoint.extract_complete
            and checkpoint.reconcile_complete
            and checkpoint.check_complete
            and checkpoint.render_complete
            and checkpoint.processing_status == ProcessingJobStatus.SUCCEEDED
            and checkpoint.layered_partial is not None
        )

    def _evaluation(self) -> tuple[list[AtomicRecord] | None, list[RecordRelation] | None]:
        records = getattr(self._provider, "evaluation_records", None)
        relations = getattr(self._provider, "relation_annotations", None)
        if records is None:
            return None, None
        return records, relations or []

    def _job_metadata(self, manifest: SourceSnapshotManifest) -> MeetingIntelligenceJobMetadata:
        from recantor.meeting_essence.identity import compute_input_fingerprint

        return MeetingIntelligenceJobMetadata(
            job_id=uuid4(),
            session_id=manifest.session_id,
            snapshot_id=manifest.snapshot_id,
            prompt_bundle_id=self._bundle.package_id,
            schema_identity=self._bundle.schema_identity,
            algorithm_identity=self._bundle.algorithm_identity,
            input_fingerprint=compute_input_fingerprint(manifest),
            requested_model=self._config.requested_model,
            requested_reasoning_effort=self._config.requested_reasoning_effort,
        )

    def _protocol_metadata(self) -> ModelInvocationMetadata:
        return ModelInvocationMetadata(
            requested_model=self._config.requested_model,
            requested_reasoning_effort=self._config.requested_reasoning_effort,
            protocol=StageProtocolMetadata(
                prompt_bundle_id=self._bundle.package_id,
                schema_identity=self._bundle.schema_identity,
                algorithm_identity=self._bundle.algorithm_identity,
            ),
        )

    def _request_budget(self, *, max_records: int) -> RequestBudget:
        limits = self._config.limits
        return RequestBudget(
            max_segments=limits.max_segments_per_extract_page,
            max_records=max(1, max_records),
            max_chars=wire_limit_bytes(limits),
        )

    def _deadline_passed(self, started_monotonic: float) -> bool:
        deadline = monotonic_deadline(self._config.max_wall_clock_seconds, started_monotonic)
        if deadline is None:
            return False
        return time.monotonic() >= deadline

    def _commit_source_page(self, plan: SourceWirePlan, checkpoint: EngineCheckpoint) -> None:
        if checkpoint.source_pages_sent < len(plan.pages):
            checkpoint.source_pages_sent += 1

    def _sample_source(self, source: SourceDescriptor) -> dict[str, object]:
        return {
            "metadata_keys": sorted(source.meeting_metadata),
            "gap_ranges": [
                [gap.sequence_start, gap.sequence_end] for gap in source.coverage.explicit_gaps
            ],
        }

    async def _partial_result(
        self,
        manifest: SourceSnapshotManifest,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        limitations: list[CoverageLimitation],
    ) -> EngineRunResult:
        checkpoint.processing_status = ProcessingJobStatus.PARTIAL
        limitations.append(
            CoverageLimitation(code="wall_clock_exhausted", message="engine wall clock budget hit")
        )
        layered = _partial_layered(checkpoint)
        await self._commit_checkpoint(checkpoint)
        return self._result(manifest, checkpoint, trace, limitations, layered)

    def _result(
        self,
        manifest: SourceSnapshotManifest,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        limitations: list[CoverageLimitation],
        layered: LayeredMeetingResult | None,
    ) -> EngineRunResult:
        return EngineRunResult(
            manifest=manifest,
            layered_result=layered,
            checkpoint=checkpoint,
            trace=trace,
            processing_status=checkpoint.processing_status,
            limitations=limitations,
        )

    def _fit(
        self,
        draft: RequestT,
        *,
        operation: StageOperation,
        response_model: type[BaseModel],
        cardinality: int,
        unicode_codepoints: int,
    ) -> tuple[RequestT, WirePayloadAudit] | None:
        try:
            fitted, audit = stabilize_audited_request(
                draft,
                bundle=self._bundle,
                operation=operation,
                response_model=response_model,
                limits=self._config.limits,
                cardinality=cardinality,
                unicode_payload_codepoints=unicode_codepoints,
            )
        except RuntimeError:
            return None
        if not fits_wire_budget(audit, self._config.limits):
            return None
        return fitted, audit  # type: ignore[return-value]

    async def _invoke(
        self,
        operation: StageOperation,
        request: BaseModel,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        audit: WirePayloadAudit,
        started_monotonic: float,
    ) -> BaseModel:
        deadline = monotonic_deadline(self._config.max_wall_clock_seconds, started_monotonic)
        return await invoke_stage(
            provider=self._provider,
            bundle=self._bundle,
            config=InvokeConfig(
                max_attempts_per_stage=self._config.max_attempts_per_stage,
                provider_retry_backoff_seconds=self._config.limits.provider_retry_backoff_seconds,
                deadline_monotonic=deadline,
                max_provider_call_seconds=self._config.limits.max_provider_call_seconds,
            ),
            operation=operation,
            request=request,  # type: ignore[arg-type]
            job=job,
            checkpoint=checkpoint,
            trace_stage_calls=trace.stage_calls,
            audit=audit,
            requested_model=self._config.requested_model,
        )

    async def _run_extract(
        self,
        manifest: SourceSnapshotManifest,
        plan: SourceWirePlan,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        started_monotonic: float,
        limitations: list[CoverageLimitation],
    ) -> None:
        limits = self._config.limits
        ordered = sorted(manifest.segments, key=lambda item: item.sequence)
        fragments: list[WireSegmentFragment] = []
        for segment in ordered:
            fragments.extend(
                split_segment_fragments(segment, max_chars=limits.max_fragment_chars_per_segment)
            )
        checkpoint.expected_fragment_count = len(fragments)
        page_start = checkpoint.extract_fragment_cursor
        if page_start > len(fragments):
            limitations.append(
                CoverageLimitation(
                    code="checkpoint_cursor", message="extract cursor is past the fragment list"
                )
            )
            checkpoint.processing_status = ProcessingJobStatus.FAILED
            return
        while page_start < len(fragments):
            if self._deadline_passed(started_monotonic):
                return
            fitted_page = self._fit_extract_page(
                plan, checkpoint, fragments, page_start, limits.max_records_per_extract_response
            )
            if fitted_page is None:
                fragment = fragments[page_start]
                if fragment.segment_id not in checkpoint.unprocessed_segment_ids:
                    checkpoint.unprocessed_segment_ids.append(fragment.segment_id)
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                limitations.append(
                    CoverageLimitation(
                        code="fragment_exceeds_budget",
                        message="a source fragment does not fit the wire budget",
                    )
                )
                return
            request, audit, page_fragments, context, slots = fitted_page
            known = {record.record_id: record for record in checkpoint.atomic_records}
            continuations = 0
            while continuations < limits.max_extract_continuations:
                if self._deadline_passed(started_monotonic):
                    return
                request = request.model_copy(
                    update={
                        "omitted_record_ids": tuple(known),
                        "source": self._peek_source(plan, checkpoint),
                        "wire_audit": stub_wire_audit(),
                    }
                )
                refit = self._fit(
                    request,
                    operation=StageOperation.EXTRACT,
                    response_model=ExtractStageResponse,
                    cardinality=slots,
                    unicode_codepoints=sum(len(fragment.text) for fragment in page_fragments),
                )
                if refit is None:
                    checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                    limitations.append(
                        CoverageLimitation(
                            code="extract_continuation_exceeds_budget",
                            message="unfinished extract page does not fit the wire budget",
                        )
                    )
                    return
                request, audit = refit
                self._commit_source_page(plan, checkpoint)
                self._record_sample(
                    trace,
                    stage="extract",
                    source=request.source,
                    extra={
                        "has_full_manifest": False,
                        "fragment_count": len(page_fragments),
                        "fragment_sequences": [fragment.sequence for fragment in page_fragments],
                        "fragment_start_cp": [
                            fragment.text_span_start_cp for fragment in page_fragments
                        ],
                        "fragment_spans": [
                            fragment_span_record(fragment) for fragment in page_fragments
                        ],
                        "serialized_utf8_bytes": serialized_utf8_byte_length(request),
                        "output_cardinality_limit": slots,
                    },
                )
                response = await self._invoke(
                    StageOperation.EXTRACT,
                    request,
                    job,
                    checkpoint,
                    trace,
                    audit,
                    started_monotonic,
                )
                assert isinstance(response, ExtractStageResponse)
                validation = validate_extracted_records_for_request(
                    manifest,
                    response.extracted_records,
                    session_id=manifest.session_id,
                    page_fragments=page_fragments,
                    context_fragments=context,
                    known_records=known,
                    max_records=slots,
                )
                if not validation.ok:
                    checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                    limitations.append(
                        CoverageLimitation(
                            code="extract_rejected",
                            message=validation.issues[0].code
                            if validation.issues
                            else "extract_rejected",
                        )
                    )
                    return
                fresh = [
                    record for record in response.extracted_records if record.record_id not in known
                ]
                if fresh:
                    checkpoint.atomic_records = merge_atomic_records(
                        checkpoint.atomic_records, fresh
                    )
                    for record in fresh:
                        known[record.record_id] = record
                exhausted = len(response.extracted_records) < slots
                if exhausted:
                    # Save acceptance and exhaustion together. A turn/restart after
                    # this checkpoint must not replay the exhausted page with a
                    # different omitted-record set (and hence a different cache key).
                    checkpoint.inspected_segment_ids = track_inspected_segments(
                        checkpoint.inspected_segment_ids, page_fragments + context
                    )
                    checkpoint.completed_fragment_indices = (
                        *checkpoint.completed_fragment_indices,
                        *range(page_start, page_start + len(page_fragments)),
                    )
                    checkpoint.extract_page_index += 1
                    page_start += len(page_fragments)
                    checkpoint.extract_fragment_cursor = page_start
                    checkpoint.processed_fragment_count = page_start
                await self._note_validated_stage(StageOperation.EXTRACT, request, response)
                await self._commit_checkpoint(checkpoint)
                if exhausted:
                    break
                if not fresh:
                    checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                    limitations.append(
                        CoverageLimitation(
                            code="extract_no_progress",
                            message="full extract response did not advance the continuation",
                        )
                    )
                    return
                continuations += 1
            else:
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                limitations.append(
                    CoverageLimitation(
                        code="extract_continuation_exhausted",
                        message="extract continuation stopped before the fragment was exhausted",
                    )
                )
                return
        if not checkpoint.unprocessed_segment_ids and page_start >= len(fragments):
            checkpoint.extract_complete = True
        elif page_start >= len(fragments):
            checkpoint.extract_complete = True
            checkpoint.processing_status = ProcessingJobStatus.PARTIAL
        checkpoint.stage = StageOperation.RECONCILE
        await self._commit_checkpoint(checkpoint)

    def _fit_extract_page(
        self,
        plan: SourceWirePlan,
        checkpoint: EngineCheckpoint,
        fragments: list[WireSegmentFragment],
        page_start: int,
        slots: int,
    ) -> (
        tuple[
            ExtractStageRequest,
            WirePayloadAudit,
            tuple[WireSegmentFragment, ...],
            tuple[WireSegmentFragment, ...],
            int,
        ]
        | None
    ):
        limits = self._config.limits
        width = min(limits.max_segments_per_extract_page, len(fragments) - page_start)
        output_slots = slots
        while width >= 1:
            trial_slots = output_slots
            while trial_slots >= 1:
                page_fragments = tuple(fragments[page_start : page_start + width])
                context_start = max(0, page_start - limits.context_overlap_segments)
                context = tuple(fragments[context_start:page_start])
                source = self._peek_source(plan, checkpoint)
                draft = ExtractStageRequest(
                    source=source,
                    segment_fragments=page_fragments,
                    context_fragments=context,
                    budget=self._request_budget(max_records=trial_slots),
                    wire_audit=stub_wire_audit(),
                    metadata=self._protocol_metadata(),
                )
                fitted = self._fit(
                    draft,
                    operation=StageOperation.EXTRACT,
                    response_model=ExtractStageResponse,
                    cardinality=trial_slots,
                    unicode_codepoints=sum(len(fragment.text) for fragment in page_fragments),
                )
                if fitted is not None:
                    request, audit = fitted
                    return request, audit, page_fragments, context, trial_slots
                if trial_slots == 1:
                    break
                trial_slots = max(1, trial_slots // 2)
            if width == 1:
                break
            width = max(1, width // 2)
        return None

    def _peek_source(self, plan: SourceWirePlan, checkpoint: EngineCheckpoint) -> SourceDescriptor:
        index = min(checkpoint.source_pages_sent, len(plan.pages) - 1)
        return plan.pages[index]

    def _record_sample(
        self,
        trace: EngineRunTrace,
        *,
        stage: str,
        source: SourceDescriptor,
        extra: dict[str, object],
    ) -> None:
        payload: dict[str, object] = {"stage": stage, **self._sample_source(source), **extra}
        trace.wire_payload_samples.append(payload)

    async def _run_reconcile(
        self,
        plan: SourceWirePlan,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        started_monotonic: float,
        limitations: list[CoverageLimitation],
    ) -> None:
        records = checkpoint.atomic_records
        if not records:
            checkpoint.reconcile_complete = True
            checkpoint.stage = StageOperation.CHECK
            await self._commit_checkpoint(checkpoint)
            return
        limits = self._config.limits
        page_size = limits.max_records_per_reconcile_page
        record_ids = [record.record_id for record in records]
        page_count = (len(record_ids) + page_size - 1) // page_size
        for page_index in range(checkpoint.reconcile_page_index, page_count):
            if self._deadline_passed(started_monotonic):
                return
            start = page_index * page_size
            page_ids = tuple(record_ids[start : start + page_size])
            page = page_records(records, page_ids)
            deps = same_topic_dependencies(page, records)
            windows = self._windows(page, deps, limits.max_dependency_records)
            for window in windows:
                if self._deadline_passed(started_monotonic):
                    return
                await self._reconcile_window(
                    plan,
                    job,
                    checkpoint,
                    trace,
                    started_monotonic,
                    limitations,
                    page_index=page_index,
                    page_count=page_count,
                    page_ids=page_ids,
                    window=window,
                )
            checkpoint.reconcile_page_index = page_index + 1
            await self._commit_checkpoint(checkpoint)
        checkpoint.reconcile_complete = True
        checkpoint.stage = StageOperation.CHECK
        await self._commit_checkpoint(checkpoint)

    def _windows(
        self,
        page: list[AtomicRecord],
        deps: list[AtomicRecord],
        window: int,
    ) -> list[list[AtomicRecord]]:
        if not deps:
            return [page]
        return [page + deps[offset : offset + window] for offset in range(0, len(deps), window)]

    async def _reconcile_window(
        self,
        plan: SourceWirePlan,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        started_monotonic: float,
        limitations: list[CoverageLimitation],
        *,
        page_index: int,
        page_count: int,
        page_ids: tuple[str, ...],
        window: list[AtomicRecord],
    ) -> None:
        limits = self._config.limits
        touching = relations_touching(checkpoint.relations, set(page_ids))
        offset = 0
        pending = touching or []
        while True:
            if self._deadline_passed(started_monotonic):
                return
            width = len(window)
            rel_width = max(
                1, min(limits.max_prior_relations_per_page, max(1, len(pending) - offset))
            )
            fitted: tuple[ReconcileStageRequest, WirePayloadAudit] | None = None
            chosen_records: list[AtomicRecord] = window
            chosen_rels: list[RecordRelation] = []
            while width >= 1 and fitted is None:
                rel_try = rel_width
                while True:
                    chosen_records = window[:width]
                    chosen_rels = pending[offset : offset + rel_try] if pending else []
                    source = self._peek_source(plan, checkpoint)
                    draft = ReconcileStageRequest(
                        source=source,
                        atomic_records=chosen_records,
                        record_page=WireRecordPage(
                            page_index=page_index,
                            page_count=page_count,
                            record_ids=tuple(
                                record.record_id
                                for record in chosen_records
                                if record.record_id in set(page_ids)
                            )
                            or page_ids[:1],
                        ),
                        prior_relations=chosen_rels,
                        budget=self._request_budget(max_records=max(1, len(chosen_records))),
                        wire_audit=stub_wire_audit(),
                        metadata=self._protocol_metadata(),
                    )
                    maybe = self._fit(
                        draft,
                        operation=StageOperation.RECONCILE,
                        response_model=ReconcileStageResponse,
                        cardinality=max(1, len(chosen_records)),
                        unicode_codepoints=sum(len(record.claim_text) for record in chosen_records),
                    )
                    if maybe is not None:
                        fitted = maybe
                        break
                    if rel_try <= 1:
                        break
                    rel_try = max(1, rel_try // 2)
                if fitted is None:
                    if width == 1:
                        break
                    width = max(1, width // 2)
            if fitted is None:
                limitations.append(
                    CoverageLimitation(
                        code="reconcile_page_exceeds_budget",
                        message="a reconcile window does not fit the wire budget",
                    )
                )
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                return
            request, audit = fitted
            self._commit_source_page(plan, checkpoint)
            self._record_sample(
                trace,
                stage="reconcile",
                source=request.source,
                extra={
                    "record_count": len(request.atomic_records),
                    "relation_count": len(request.prior_relations),
                    "serialized_utf8_bytes": serialized_utf8_byte_length(request),
                },
            )
            response = await self._invoke(
                StageOperation.RECONCILE, request, job, checkpoint, trace, audit, started_monotonic
            )
            assert isinstance(response, ReconcileStageResponse)
            if response.reconciled_records:
                allowed = {record.record_id for record in request.atomic_records}
                echoed = [
                    record for record in response.reconciled_records if record.record_id in allowed
                ]
                checkpoint.atomic_records = merge_atomic_records(checkpoint.atomic_records, echoed)
            seen = {relation_key(relation) for relation in checkpoint.relations}
            present = {record.record_id for record in request.atomic_records}
            for relation in response.relations:
                if relation.from_record_id not in present or relation.to_record_id not in present:
                    continue
                key = relation_key(relation)
                if key in seen:
                    continue
                seen.add(key)
                checkpoint.relations.append(relation)
            await self._note_validated_stage(StageOperation.RECONCILE, request, response)
            await self._commit_checkpoint(checkpoint)
            sent = len(request.prior_relations)
            if pending and sent == 0:
                limitations.append(
                    CoverageLimitation(
                        code="reconcile_relation_exceeds_budget",
                        message="a reconcile relation does not fit the wire budget",
                    )
                )
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                return
            offset += sent if pending else 0
            if not pending or offset >= len(pending):
                return

    async def _run_semantic_checks(
        self,
        manifest: SourceSnapshotManifest,
        plan: SourceWirePlan,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        started_monotonic: float,
        limitations: list[CoverageLimitation],
        *,
        layered: LayeredMeetingResult | None,
    ) -> None:
        self._check_manifest = manifest
        limits = self._config.limits
        fragments = manifest_fragments(manifest, max_chars=limits.max_fragment_chars_per_segment)
        claims = accessible_claims_from_result(layered) if layered is not None else []
        await self._run_source_coverage(
            plan,
            job,
            checkpoint,
            trace,
            started_monotonic,
            limitations,
            fragments=fragments,
            claims=claims,
        )
        if self._deadline_passed(started_monotonic):
            self._refresh_check_aggregate(checkpoint, fragments, claims, manifest, plan)
            return
        if checkpoint.source_check_cursor < len(fragments):
            self._refresh_check_aggregate(checkpoint, fragments, claims, manifest, plan)
            return
        await self._run_output_coverage(
            plan,
            job,
            checkpoint,
            trace,
            started_monotonic,
            limitations,
            fragments=fragments,
            claims=claims,
        )
        if self._deadline_passed(started_monotonic):
            self._refresh_check_aggregate(checkpoint, fragments, claims, manifest, plan)
            return
        if claims and checkpoint.output_check_cursor < len(claims):
            self._refresh_check_aggregate(checkpoint, fragments, claims, manifest, plan)
            return
        await self._run_relation_context(
            plan,
            job,
            checkpoint,
            trace,
            started_monotonic,
            limitations,
            manifest=manifest,
        )
        self._refresh_check_aggregate(checkpoint, fragments, claims, manifest, plan)
        relation_plan = self._planned_relation_slices(plan, checkpoint, manifest)
        relation_done = (
            not relation_plan.invalid
            and not relation_plan.exceeds
            and checkpoint.relation_context_cursor >= len(relation_plan.slices)
        )
        cursors_done = (
            checkpoint.source_check_cursor >= len(fragments)
            and checkpoint.output_check_cursor >= len(claims)
            and relation_done
        )
        receipts_done = (
            not checkpoint.pending_check_fingerprints and not checkpoint.failed_check_fingerprints
        )
        if cursors_done and receipts_done:
            checkpoint.check_complete = True
            await self._commit_checkpoint(checkpoint)

    def _refresh_check_aggregate(
        self,
        checkpoint: EngineCheckpoint,
        fragments: list[WireSegmentFragment],
        claims: list[AccessibleOutputClaim],
        manifest: SourceSnapshotManifest,
        plan: SourceWirePlan,
    ) -> None:
        fragment_keys = [fragment_key(fragment) for fragment in fragments]
        claim_ids = [claim.claim_id for claim in claims]
        record_ids = [record.record_id for record in checkpoint.atomic_records]
        relation_plan = self._planned_relation_slices(plan, checkpoint, manifest)
        if relation_plan.invalid:
            slice_keys = ["relation_context_invalid"]
        elif relation_plan.exceeds:
            slice_keys = ["relation_context_exceeds_budget"]
        else:
            slice_keys = [item.receipt_key for item in relation_plan.slices]
        checkpoint.forward_semantic_check_status = merge_check_status(
            merge_check_status(
                aggregate_check_status(fragment_keys, checkpoint.source_fragment_forward),
                aggregate_check_status(claim_ids, checkpoint.output_claim_forward),
            ),
            merge_check_status(
                aggregate_check_status(record_ids, checkpoint.source_record_forward),
                aggregate_check_status(slice_keys, checkpoint.relation_slice_forward),
            ),
        )
        checkpoint.backward_semantic_check_status = merge_check_status(
            merge_check_status(
                aggregate_check_status(fragment_keys, checkpoint.source_fragment_backward),
                aggregate_check_status(claim_ids, checkpoint.output_claim_backward),
            ),
            merge_check_status(
                aggregate_check_status(record_ids, checkpoint.source_record_backward),
                aggregate_check_status(slice_keys, checkpoint.relation_slice_backward),
            ),
        )

    async def _run_source_coverage(
        self,
        plan: SourceWirePlan,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        started_monotonic: float,
        limitations: list[CoverageLimitation],
        *,
        fragments: list[WireSegmentFragment],
        claims: list[AccessibleOutputClaim],
    ) -> None:
        limits = self._config.limits
        while checkpoint.source_check_cursor < len(fragments):
            if self._deadline_passed(started_monotonic):
                return
            index = checkpoint.source_check_cursor
            width = min(limits.max_source_fragments_per_check, len(fragments) - index)
            page: list[WireSegmentFragment] | None = None
            while width >= 1:
                candidate = fragments[index : index + width]
                progressed = await self._dispatch_check_pages(
                    plan,
                    job,
                    checkpoint,
                    trace,
                    started_monotonic,
                    limitations,
                    check_pass=CheckPass.SOURCE_COVERAGE,
                    fragments=candidate,
                    records=records_touching_fragments(checkpoint.atomic_records, candidate),
                    claims=claims_touching_fragments(claims, candidate),
                    page_index=index,
                    page_count=max(1, len(fragments)),
                    record_cursor=checkpoint.source_check_record_cursor,
                    claim_cursor=checkpoint.source_check_claim_cursor,
                    include_relations=False,
                )
                if progressed is None:
                    if width == 1:
                        checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                        limitations.append(
                            CoverageLimitation(
                                code="check_page_exceeds_budget",
                                message="a source check page does not fit the wire budget",
                            )
                        )
                        return
                    width = max(1, width // 2)
                    continue
                if not progressed:
                    return
                page = candidate
                checkpoint.source_check_record_cursor = 0
                checkpoint.source_check_claim_cursor = 0
                checkpoint.source_check_relation_cursor = 0
                break
            if page is None:
                return
            checkpoint.source_check_cursor = index + len(page)

    async def _run_output_coverage(
        self,
        plan: SourceWirePlan,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        started_monotonic: float,
        limitations: list[CoverageLimitation],
        *,
        fragments: list[WireSegmentFragment],
        claims: list[AccessibleOutputClaim],
    ) -> None:
        if not claims:
            checkpoint.output_check_cursor = 0
            return
        limits = self._config.limits
        while checkpoint.output_check_cursor < len(claims):
            if self._deadline_passed(started_monotonic):
                return
            index = checkpoint.output_check_cursor
            batch_width = min(limits.max_output_claims_per_check, len(claims) - index)
            sent_claims = 0
            while batch_width >= 1 and sent_claims == 0:
                batch = claims[index : index + batch_width]
                supports: list[WireSegmentFragment] = []
                seen: set[str] = set()
                for claim in batch:
                    for fragment in support_fragments_for_claim(claim, fragments):
                        key = fragment_key(fragment)
                        if key in seen:
                            continue
                        seen.add(key)
                        supports.append(fragment)
                if not supports:
                    for claim in batch:
                        note_check_status(
                            checkpoint.output_claim_forward, claim.claim_id, CheckStatus.FAILED
                        )
                        note_check_status(
                            checkpoint.output_claim_backward, claim.claim_id, CheckStatus.FAILED
                        )
                    limitations.append(
                        CoverageLimitation(
                            code="output_claim_without_source",
                            message="an accessible claim has no original support fragment",
                        )
                    )
                    checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                    checkpoint.output_check_cursor = index + len(batch)
                    sent_claims = len(batch)
                    break
                support_cursor = checkpoint.output_support_cursor
                support_width = min(
                    limits.max_source_fragments_per_check, len(supports) - support_cursor
                )
                fitted_support: list[WireSegmentFragment] | None = None
                while support_width >= 1 and fitted_support is None:
                    chunk = supports[support_cursor : support_cursor + support_width]
                    chunk_claims = claims_touching_fragments(batch, chunk)
                    support_ids = {fragment.segment_id for fragment in chunk}
                    records = [
                        record
                        for record in checkpoint.atomic_records
                        if any(
                            item.text.strip() == record.claim_text.strip() for item in chunk_claims
                        )
                        and any(evidence.segment_id in support_ids for evidence in record.evidence)
                    ]
                    progressed = await self._dispatch_check_pages(
                        plan,
                        job,
                        checkpoint,
                        trace,
                        started_monotonic,
                        limitations,
                        check_pass=CheckPass.OUTPUT_COVERAGE,
                        fragments=chunk,
                        records=records,
                        claims=chunk_claims,
                        page_index=index,
                        page_count=len(claims),
                        record_cursor=checkpoint.output_check_record_cursor,
                        claim_cursor=checkpoint.output_check_claim_cursor,
                        include_relations=False,
                    )
                    if progressed is None:
                        if support_width == 1 and batch_width == 1:
                            checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                            limitations.append(
                                CoverageLimitation(
                                    code="check_page_exceeds_budget",
                                    message="an output check page does not fit the wire budget",
                                )
                            )
                            checkpoint.output_support_cursor = support_cursor
                            return
                        if support_width > 1:
                            support_width = max(1, support_width // 2)
                            continue
                        break
                    if not progressed:
                        checkpoint.output_support_cursor = support_cursor
                        return
                    fitted_support = chunk
                if fitted_support is None:
                    batch_width = max(1, batch_width // 2) if batch_width > 1 else 0
                    if batch_width == 0:
                        checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                        return
                    continue
                support_cursor += len(fitted_support)
                checkpoint.output_support_cursor = support_cursor
                checkpoint.output_check_record_cursor = 0
                checkpoint.output_check_claim_cursor = 0
                checkpoint.output_check_relation_cursor = 0
                if support_cursor < len(supports):
                    continue
                sent_claims = len(batch)
                checkpoint.output_check_cursor = index + sent_claims
                checkpoint.output_support_cursor = 0

    def _write_check_cursors(
        self,
        checkpoint: EngineCheckpoint,
        check_pass: CheckPass,
        record_cursor: int,
        claim_cursor: int,
        relation_cursor: int,
    ) -> None:
        if check_pass == CheckPass.SOURCE_COVERAGE:
            checkpoint.source_check_record_cursor = record_cursor
            checkpoint.source_check_claim_cursor = claim_cursor
            checkpoint.source_check_relation_cursor = relation_cursor
            return
        checkpoint.output_check_record_cursor = record_cursor
        checkpoint.output_check_claim_cursor = claim_cursor
        checkpoint.output_check_relation_cursor = relation_cursor

    def _relation_cursor(self, checkpoint: EngineCheckpoint, check_pass: CheckPass) -> int:
        if check_pass == CheckPass.SOURCE_COVERAGE:
            return checkpoint.source_check_relation_cursor
        return checkpoint.output_check_relation_cursor

    async def _dispatch_check_pages(
        self,
        plan: SourceWirePlan,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        started_monotonic: float,
        limitations: list[CoverageLimitation],
        *,
        check_pass: CheckPass,
        fragments: list[WireSegmentFragment],
        records: list[AtomicRecord],
        claims: list[AccessibleOutputClaim],
        page_index: int,
        page_count: int,
        record_cursor: int,
        claim_cursor: int,
        include_relations: bool = True,
    ) -> bool | None:
        """Send every record, claim, and relation subpage. None means the page cannot fit."""
        relation_cursor = self._relation_cursor(checkpoint, check_pass)
        self._write_check_cursors(
            checkpoint, check_pass, record_cursor, claim_cursor, relation_cursor
        )
        if not fragments:
            return None
        if not records and not claims:
            request = self._fit_check_request(
                plan,
                checkpoint,
                check_pass=check_pass,
                fragments=fragments,
                records=[],
                claims=[],
                relations=[],
                page_index=page_index,
                page_count=page_count,
                record_cursor=record_cursor,
                claim_cursor=claim_cursor,
                relation_cursor=relation_cursor,
            )
            if request is None:
                return None
            fitted, audit = request
            return await self._send_check(
                plan, fitted, audit, job, checkpoint, trace, started_monotonic
            )
        while record_cursor < len(records) or claim_cursor < len(claims):
            if self._deadline_passed(started_monotonic):
                self._write_check_cursors(
                    checkpoint, check_pass, record_cursor, claim_cursor, relation_cursor
                )
                return False
            self._write_check_cursors(
                checkpoint, check_pass, record_cursor, claim_cursor, relation_cursor
            )
            fitted_page = self._shrink_check_page(
                plan,
                checkpoint,
                check_pass=check_pass,
                fragments=fragments,
                records=records,
                claims=claims,
                page_index=page_index,
                page_count=page_count,
                record_cursor=record_cursor,
                claim_cursor=claim_cursor,
                relation_cursor=relation_cursor,
                include_relations=include_relations,
            )
            if fitted_page is None:
                return None
            request, audit = fitted_page
            acknowledged = await self._send_check(
                plan, request, audit, job, checkpoint, trace, started_monotonic
            )
            if not acknowledged:
                self._write_check_cursors(
                    checkpoint, check_pass, record_cursor, claim_cursor, relation_cursor
                )
                return False
            present = {record.record_id for record in request.atomic_records}
            window_relations = (
                relations_touching(checkpoint.relations, present) if include_relations else []
            )
            relation_cursor += len(request.relations)
            remaining = window_relations[relation_cursor:]
            while remaining:
                if self._deadline_passed(started_monotonic):
                    self._write_check_cursors(
                        checkpoint, check_pass, record_cursor, claim_cursor, relation_cursor
                    )
                    return False
                self._write_check_cursors(
                    checkpoint, check_pass, record_cursor, claim_cursor, relation_cursor
                )
                rel_width = len(remaining)
                continuation = None
                while rel_width >= 1 and continuation is None:
                    continuation = self._fit_check_request(
                        plan,
                        checkpoint,
                        check_pass=check_pass,
                        fragments=fragments,
                        records=list(request.atomic_records),
                        claims=list(request.output_claims),
                        relations=remaining[:rel_width],
                        page_index=page_index,
                        page_count=page_count,
                        record_cursor=record_cursor,
                        claim_cursor=claim_cursor,
                        relation_cursor=relation_cursor,
                    )
                    if continuation is None:
                        if rel_width == 1:
                            limitations.append(
                                CoverageLimitation(
                                    code="check_relation_exceeds_budget",
                                    message="a check relation does not fit the wire budget",
                                )
                            )
                            checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                            self._write_check_cursors(
                                checkpoint,
                                check_pass,
                                record_cursor,
                                claim_cursor,
                                relation_cursor,
                            )
                            return False
                        rel_width = max(1, rel_width // 2)
                if continuation is None:
                    return None
                next_request, next_audit = continuation
                acknowledged = await self._send_check(
                    plan, next_request, next_audit, job, checkpoint, trace, started_monotonic
                )
                if not acknowledged:
                    self._write_check_cursors(
                        checkpoint, check_pass, record_cursor, claim_cursor, relation_cursor
                    )
                    return False
                sent_count = len(next_request.relations)
                if sent_count == 0:
                    limitations.append(
                        CoverageLimitation(
                            code="check_relation_exceeds_budget",
                            message="a check relation does not fit the wire budget",
                        )
                    )
                    checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                    return False
                relation_cursor += sent_count
                remaining = window_relations[relation_cursor:]
            advanced_records = len(request.atomic_records)
            advanced_claims = len(request.output_claims)
            if advanced_records == 0 and advanced_claims == 0:
                return None
            record_cursor += advanced_records
            claim_cursor += advanced_claims
            relation_cursor = 0
            self._write_check_cursors(
                checkpoint, check_pass, record_cursor, claim_cursor, relation_cursor
            )
        return True

    def _shrink_check_page(
        self,
        plan: SourceWirePlan,
        checkpoint: EngineCheckpoint,
        *,
        check_pass: CheckPass,
        fragments: list[WireSegmentFragment],
        records: list[AtomicRecord],
        claims: list[AccessibleOutputClaim],
        page_index: int,
        page_count: int,
        record_cursor: int,
        claim_cursor: int,
        relation_cursor: int,
        include_relations: bool = True,
    ) -> tuple[CheckStageRequest, WirePayloadAudit] | None:
        limits = self._config.limits
        record_width = min(limits.max_records_per_check_page, max(0, len(records) - record_cursor))
        claim_width = min(limits.max_output_claims_per_check, max(0, len(claims) - claim_cursor))
        if record_width == 0 and claim_width == 0:
            return None
        while True:
            chosen_records = records[record_cursor : record_cursor + record_width]
            chosen_claims = claims[claim_cursor : claim_cursor + claim_width]
            present = {record.record_id for record in chosen_records}
            relations = (
                relations_touching(checkpoint.relations, present) if include_relations else []
            )
            pending_relations = relations[min(relation_cursor, len(relations)) :]
            rel_width = min(limits.max_relations_on_check_page, len(pending_relations))
            while True:
                fitted = self._fit_check_request(
                    plan,
                    checkpoint,
                    check_pass=check_pass,
                    fragments=fragments,
                    records=chosen_records,
                    claims=chosen_claims,
                    relations=pending_relations[:rel_width],
                    page_index=page_index,
                    page_count=page_count,
                    record_cursor=record_cursor,
                    claim_cursor=claim_cursor,
                    relation_cursor=relation_cursor,
                )
                if fitted is not None:
                    return fitted
                if rel_width > 1:
                    rel_width = max(1, rel_width // 2)
                    continue
                break
            if claim_width > 1:
                claim_width = max(1, claim_width // 2)
                continue
            if record_width > 1:
                record_width = max(1, record_width // 2)
                continue
            if claim_width > 0 and record_width > 0:
                claim_width = 0
                continue
            return None

    def _fit_check_request(
        self,
        plan: SourceWirePlan,
        checkpoint: EngineCheckpoint,
        *,
        check_pass: CheckPass,
        fragments: list[WireSegmentFragment],
        records: list[AtomicRecord],
        claims: list[AccessibleOutputClaim],
        relations: list[RecordRelation],
        page_index: int,
        page_count: int,
        record_cursor: int,
        claim_cursor: int,
        relation_cursor: int,
        judged_evidence_keys: tuple[str, ...] = (),
        relation_slice_keys: tuple[str, ...] = (),
    ) -> tuple[CheckStageRequest, WirePayloadAudit] | None:
        if not fragments:
            return None
        source = self._peek_source(plan, checkpoint)
        page_cursor = (
            f"{check_pass.value}:{page_index}:{record_cursor}:{claim_cursor}:{relation_cursor}"
        )
        binding = bind_check_page(
            snapshot_content_hash=source.snapshot_content_hash,
            check_pass=check_pass,
            fragments=fragments,
            claims=claims,
            records=records,
            relations=relations,
            page_cursor=page_cursor,
            judged_evidence_keys=judged_evidence_keys,
            relation_slice_keys=relation_slice_keys,
        )
        record_page = None
        if records:
            record_page = WireRecordPage(
                page_index=page_index,
                page_count=max(1, page_count),
                record_ids=tuple(record.record_id for record in records),
            )
        draft = CheckStageRequest(
            source=source,
            atomic_records=records,
            relations=relations,
            record_page=record_page,
            check_pass=check_pass,
            direction=binding.direction,
            source_fragments=tuple(fragments),
            output_claims=claims,
            result_binding=binding,
            budget=self._request_budget(max_records=max(1, len(records) + len(claims))),
            wire_audit=stub_wire_audit(),
            metadata=self._protocol_metadata(),
        )
        return self._fit(
            draft,
            operation=StageOperation.CHECK,
            response_model=CheckStageResponse,
            cardinality=max(1, len(records) + len(claims) + len(fragments)),
            unicode_codepoints=sum(len(fragment.text) for fragment in fragments)
            + sum(len(claim.text) for claim in claims)
            + sum(len(record.claim_text) for record in records),
        )

    async def _send_check(
        self,
        plan: SourceWirePlan,
        request: CheckStageRequest,
        audit: WirePayloadAudit,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        started_monotonic: float,
    ) -> bool:
        fingerprint = request.result_binding.request_fingerprint
        if fingerprint in checkpoint.acked_check_fingerprints:
            return True
        if fingerprint in checkpoint.failed_check_fingerprints:
            return False
        checkpoint.pending_check_fingerprints.add(fingerprint)
        self._commit_source_page(plan, checkpoint)
        self._record_sample(
            trace,
            stage="check",
            source=request.source,
            extra={
                "check_pass": request.check_pass.value,
                "direction": request.direction.value,
                "page_cursor": request.result_binding.page_cursor,
                "record_count": len(request.atomic_records),
                "relation_count": len(request.relations),
                "record_ids": list(request.result_binding.record_ids),
                "relation_keys": list(request.result_binding.relation_keys),
                "judged_evidence_keys": list(request.result_binding.judged_evidence_keys),
                "relation_slice_keys": list(request.result_binding.relation_slice_keys),
                "source_fragment_keys": list(request.result_binding.source_fragment_keys),
                "output_claim_ids": list(request.result_binding.output_claim_ids),
                "output_claim_texts": [claim.text for claim in request.output_claims],
                "output_claim_surfaces": [claim.surface for claim in request.output_claims],
                "fragment_spans": [
                    fragment_span_record(fragment) for fragment in request.source_fragments
                ],
                "source_text_codepoints": sum(
                    len(fragment.text) for fragment in request.source_fragments
                ),
                "serialized_utf8_bytes": serialized_utf8_byte_length(request),
                "checks_render": request.check_pass == CheckPass.OUTPUT_COVERAGE,
            },
        )
        response = await self._invoke(
            StageOperation.CHECK, request, job, checkpoint, trace, audit, started_monotonic
        )
        assert isinstance(response, CheckStageResponse)
        accepted = self._absorb_check(checkpoint, request, response)
        if accepted:
            await self._note_validated_stage(StageOperation.CHECK, request, response)
            await self._commit_checkpoint(checkpoint)
        return accepted

    def _note_check_page(
        self,
        checkpoint: EngineCheckpoint,
        request: CheckStageRequest,
        forward: CheckStatus,
        backward: CheckStatus,
    ) -> None:
        """Record this pass only. A source receipt cannot complete an output claim."""
        binding = request.result_binding
        if request.check_pass == CheckPass.RELATION_CONTEXT:
            for key in binding.relation_slice_keys:
                note_check_status(checkpoint.relation_slice_forward, key, forward)
                note_check_status(checkpoint.relation_slice_backward, key, backward)
            return
        if request.check_pass == CheckPass.SOURCE_COVERAGE:
            for key in binding.source_fragment_keys:
                note_check_status(checkpoint.source_fragment_forward, key, forward)
                note_check_status(checkpoint.source_fragment_backward, key, backward)
            for record_id in binding.record_ids:
                note_check_status(checkpoint.source_record_forward, record_id, forward)
                note_check_status(checkpoint.source_record_backward, record_id, backward)
            return
        for claim_id in binding.output_claim_ids:
            note_check_status(checkpoint.output_claim_forward, claim_id, forward)
            note_check_status(checkpoint.output_claim_backward, claim_id, backward)

    def _absorb_check(
        self,
        checkpoint: EngineCheckpoint,
        request: CheckStageRequest,
        response: CheckStageResponse,
    ) -> bool:
        binding = request.result_binding
        fingerprint = binding.request_fingerprint
        matched = binding_matches_response(
            binding,
            request_fingerprint=response.request_fingerprint,
            source_fingerprint=response.source_fingerprint,
            result_fingerprint=response.result_fingerprint,
            check_pass=response.check_pass,
            direction=response.direction,
        )
        admitted = is_admitted_semantic_method(response.semantic_check_method)
        if not admitted or not matched:
            if admitted and not matched:
                checkpoint.failed_check_fingerprints.add(fingerprint)
                checkpoint.pending_check_fingerprints.discard(fingerprint)
                checkpoint.semantic_check_method = response.semantic_check_method
                self._note_check_page(checkpoint, request, CheckStatus.FAILED, CheckStatus.FAILED)
            return False
        checkpoint.semantic_check_method = response.semantic_check_method
        forward = response.forward_semantic_check_status
        backward = response.backward_semantic_check_status
        exact = receipt_acknowledges(
            binding,
            covered_fragment_keys=response.covered_fragment_keys,
            covered_claim_ids=response.covered_claim_ids,
            covered_record_ids=response.covered_record_ids,
            covered_relation_keys=response.covered_relation_keys,
        )
        if forward != CheckStatus.COMPLETED or backward != CheckStatus.COMPLETED:
            checkpoint.failed_check_fingerprints.add(fingerprint)
            checkpoint.pending_check_fingerprints.discard(fingerprint)
            self._note_check_page(checkpoint, request, forward, backward)
            return False
        if not exact:
            return False
        manifest = self._check_manifest
        context_ok = manifest is not None and relation_page_covers_citations(
            list(request.relations),
            list(request.atomic_records),
            request.source_fragments,
            binding.judged_evidence_keys,
            manifest,
        )
        if request.relations and not context_ok:
            checkpoint.failed_check_fingerprints.add(fingerprint)
            checkpoint.pending_check_fingerprints.discard(fingerprint)
            self._note_check_page(checkpoint, request, CheckStatus.FAILED, CheckStatus.FAILED)
            return False
        checkpoint.acked_check_fingerprints.add(fingerprint)
        checkpoint.pending_check_fingerprints.discard(fingerprint)
        checkpoint.failed_check_fingerprints.discard(fingerprint)
        self._note_check_page(checkpoint, request, forward, backward)
        return True

    def _planned_relation_slices(
        self,
        plan: SourceWirePlan,
        checkpoint: EngineCheckpoint,
        manifest: SourceSnapshotManifest,
    ) -> RelationContextPlan:
        if not checkpoint.relations:
            return RelationContextPlan()
        for relation in checkpoint.relations:
            if endpoint_records(checkpoint.atomic_records, relation) is None:
                return RelationContextPlan(invalid=True)
        limits = self._config.limits
        max_chars = limits.max_fragment_chars_per_segment
        while True:
            slices = build_relation_context_slices(
                checkpoint.relations, manifest, max_chars=max_chars
            )
            if slices is None:
                return RelationContextPlan(invalid=True)
            if all(
                self._relation_slice_fits(plan, checkpoint, item, index, len(slices))
                for index, item in enumerate(slices)
            ):
                return RelationContextPlan(slices=tuple(slices))
            if max_chars == 1:
                return RelationContextPlan(exceeds=True)
            max_chars = max(1, max_chars // 2)

    def _relation_slice_fits(
        self,
        plan: SourceWirePlan,
        checkpoint: EngineCheckpoint,
        item: RelationContextSlice,
        index: int,
        page_count: int,
    ) -> bool:
        relation = checkpoint.relations[item.relation_index]
        endpoints = endpoint_records(checkpoint.atomic_records, relation)
        if endpoints is None:
            return False
        return (
            self._fit_check_request(
                plan,
                checkpoint,
                check_pass=CheckPass.RELATION_CONTEXT,
                fragments=[item.fragment],
                records=endpoints,
                claims=[],
                relations=[relation],
                page_index=index,
                page_count=max(1, page_count),
                record_cursor=0,
                claim_cursor=0,
                relation_cursor=0,
                judged_evidence_keys=(item.evidence_key,),
                relation_slice_keys=(item.receipt_key,),
            )
            is not None
        )

    async def _run_relation_context(
        self,
        plan: SourceWirePlan,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        started_monotonic: float,
        limitations: list[CoverageLimitation],
        *,
        manifest: SourceSnapshotManifest,
    ) -> None:
        planned = self._planned_relation_slices(plan, checkpoint, manifest)
        if planned.invalid or planned.exceeds:
            code = (
                "relation_context_invalid" if planned.invalid else "relation_context_exceeds_budget"
            )
            message = (
                "a relationship citation is missing or does not match the frozen source"
                if planned.invalid
                else "a relationship citation does not fit the wire budget"
            )
            limitations.append(CoverageLimitation(code=code, message=message))
            checkpoint.processing_status = ProcessingJobStatus.PARTIAL
            note_check_status(checkpoint.relation_slice_forward, code, CheckStatus.FAILED)
            note_check_status(checkpoint.relation_slice_backward, code, CheckStatus.FAILED)
            return
        slices = planned.slices
        while checkpoint.relation_context_cursor < len(slices):
            if self._deadline_passed(started_monotonic):
                return
            index = checkpoint.relation_context_cursor
            item = slices[index]
            relation = checkpoint.relations[item.relation_index]
            endpoints = endpoint_records(checkpoint.atomic_records, relation)
            if endpoints is None:
                limitations.append(
                    CoverageLimitation(
                        code="relation_endpoint_missing",
                        message="a relationship is missing an endpoint record",
                    )
                )
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                note_check_status(
                    checkpoint.relation_slice_forward, item.receipt_key, CheckStatus.FAILED
                )
                note_check_status(
                    checkpoint.relation_slice_backward, item.receipt_key, CheckStatus.FAILED
                )
                return
            fitted = self._fit_check_request(
                plan,
                checkpoint,
                check_pass=CheckPass.RELATION_CONTEXT,
                fragments=[item.fragment],
                records=endpoints,
                claims=[],
                relations=[relation],
                page_index=index,
                page_count=max(1, len(slices)),
                record_cursor=0,
                claim_cursor=0,
                relation_cursor=0,
                judged_evidence_keys=(item.evidence_key,),
                relation_slice_keys=(item.receipt_key,),
            )
            if fitted is None:
                limitations.append(
                    CoverageLimitation(
                        code="relation_context_exceeds_budget",
                        message="a relationship citation does not fit the wire budget",
                    )
                )
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                note_check_status(
                    checkpoint.relation_slice_forward,
                    "relation_context_exceeds_budget",
                    CheckStatus.FAILED,
                )
                note_check_status(
                    checkpoint.relation_slice_backward,
                    "relation_context_exceeds_budget",
                    CheckStatus.FAILED,
                )
                return
            request, audit = fitted
            acknowledged = await self._send_check(
                plan, request, audit, job, checkpoint, trace, started_monotonic
            )
            if not acknowledged:
                return
            checkpoint.relation_context_cursor = index + 1

    async def _run_render(
        self,
        manifest: SourceSnapshotManifest,
        plan: SourceWirePlan,
        job: MeetingIntelligenceJobMetadata,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        started_monotonic: float,
        limitations: list[CoverageLimitation],
    ) -> LayeredMeetingResult | None:
        records = checkpoint.atomic_records
        if not records:
            checkpoint.render_complete = True
            return None
        limits = self._config.limits
        cursor = checkpoint.render_record_cursor
        pages: list[LayeredMeetingResult] = []
        if cursor > 0 and checkpoint.layered_partial is not None:
            pages.append(checkpoint.layered_partial)
        while cursor < len(records):
            if self._deadline_passed(started_monotonic):
                checkpoint.render_record_cursor = cursor
                return checkpoint.layered_partial
            width = min(limits.max_records_per_render_batch, len(records) - cursor)
            fitted_batch: (
                tuple[
                    RenderStageRequest, WirePayloadAudit, list[AtomicRecord], list[RecordRelation]
                ]
                | None
            ) = None
            while width >= 1 and fitted_batch is None:
                batch = records[cursor : cursor + width]
                batch_ids = {record.record_id for record in batch}
                rels = relations_touching(checkpoint.relations, batch_ids)
                rel_width = len(rels)
                while True:
                    rel_batch = rels[:rel_width] if rels else []
                    source = self._peek_source(plan, checkpoint)
                    draft = RenderStageRequest(
                        source=source,
                        atomic_records=batch,
                        relations=rel_batch,
                        budget=self._request_budget(max_records=max(1, len(batch))),
                        wire_audit=stub_wire_audit(),
                        metadata=self._protocol_metadata(),
                    )
                    maybe = self._fit(
                        draft,
                        operation=StageOperation.RENDER,
                        response_model=RenderStageResponse,
                        cardinality=max(1, len(batch)),
                        unicode_codepoints=sum(len(record.claim_text) for record in batch),
                    )
                    if maybe is not None:
                        request, audit = maybe
                        fitted_batch = (request, audit, batch, rel_batch)
                        break
                    if rel_width <= 1:
                        break
                    rel_width = max(1, rel_width // 2)
                if fitted_batch is None:
                    if width == 1:
                        break
                    width = max(1, width // 2)
            if fitted_batch is None:
                limitations.append(
                    CoverageLimitation(
                        code="render_page_exceeds_budget",
                        message="a render page does not fit the wire budget",
                    )
                )
                checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                checkpoint.render_record_cursor = cursor
                return checkpoint.layered_partial
            request, audit, batch, rel_batch = fitted_batch
            remaining_rels = relations_touching(
                checkpoint.relations, {record.record_id for record in batch}
            )
            rel_offset = 0
            while True:
                if rel_offset:
                    rel_batch = remaining_rels[
                        rel_offset : rel_offset + max(1, len(rel_batch) or 1)
                    ]
                    source = self._peek_source(plan, checkpoint)
                    draft = request.model_copy(
                        update={
                            "source": source,
                            "relations": rel_batch,
                            "wire_audit": stub_wire_audit(),
                        }
                    )
                    refit = self._fit(
                        draft,
                        operation=StageOperation.RENDER,
                        response_model=RenderStageResponse,
                        cardinality=len(batch),
                        unicode_codepoints=sum(len(record.claim_text) for record in batch),
                    )
                    if refit is None:
                        checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                        break
                    request, audit = refit
                self._commit_source_page(plan, checkpoint)
                self._record_sample(
                    trace,
                    stage="render",
                    source=request.source,
                    extra={
                        "record_count": len(request.atomic_records),
                        "relation_count": len(request.relations),
                        "serialized_utf8_bytes": serialized_utf8_byte_length(request),
                    },
                )
                response = await self._invoke(
                    StageOperation.RENDER, request, job, checkpoint, trace, audit, started_monotonic
                )
                assert isinstance(response, RenderStageResponse)
                sent_ids = {record.record_id for record in request.atomic_records}
                echoed_records = [
                    record
                    for record in response.layered_result.atomic_records
                    if record.record_id in sent_ids
                ]
                if {record.record_id for record in echoed_records} != sent_ids:
                    limitations.append(
                        CoverageLimitation(
                            code="render_omitted_record",
                            message="render page omitted a record that was on the request",
                        )
                    )
                    checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                sent_rel_keys = {relation_key(relation) for relation in request.relations}
                echoed_rels = [
                    relation
                    for relation in response.layered_result.relations
                    if relation_key(relation) in sent_rel_keys
                ]
                page_result = response.layered_result.model_copy(
                    update={"atomic_records": echoed_records, "relations": echoed_rels}
                )
                pages.append(page_result)
                await self._note_validated_stage(StageOperation.RENDER, request, response)
                sent_rel_count = len(request.relations)
                if remaining_rels and sent_rel_count == 0:
                    limitations.append(
                        CoverageLimitation(
                            code="render_relation_exceeds_budget",
                            message="a render relation does not fit the wire budget",
                        )
                    )
                    checkpoint.processing_status = ProcessingJobStatus.PARTIAL
                    break
                rel_offset += sent_rel_count
                if rel_offset >= len(remaining_rels):
                    break
            cursor += len(batch)
            checkpoint.render_record_cursor = cursor
            checkpoint.layered_partial = merge_rendered_pages(
                pages,
                snapshot_id=manifest.snapshot_id,
                enumerated_segment_count=len(manifest.segments),
                processing_status=ProcessingJobStatus.RUNNING,
                forward_check=CheckStatus.NOT_STARTED,
                backward_check=CheckStatus.NOT_STARTED,
                semantic_check_method=None,
            )
            await self._commit_checkpoint(checkpoint)
        if not pages:
            return None
        merged = merge_rendered_pages(
            pages,
            snapshot_id=manifest.snapshot_id,
            enumerated_segment_count=len(manifest.segments),
            processing_status=ProcessingJobStatus.RUNNING,
            forward_check=CheckStatus.NOT_STARTED,
            backward_check=CheckStatus.NOT_STARTED,
            semantic_check_method=None,
        )
        checkpoint.layered_partial = merged
        checkpoint.render_complete = cursor >= len(records)
        await self._commit_checkpoint(checkpoint)
        return merged

    def _finalize(
        self,
        manifest: SourceSnapshotManifest,
        checkpoint: EngineCheckpoint,
        trace: EngineRunTrace,
        limitations: list[CoverageLimitation],
        layered: LayeredMeetingResult | None,
        plan: SourceWirePlan,
        started_monotonic: float,
    ) -> EngineRunResult:
        del started_monotonic
        expected_records, expected_relations = self._evaluation()
        annotation = None
        annotation_forward: CheckStatus | None = None
        annotation_backward: CheckStatus | None = None
        if expected_records is not None:
            annotation = evaluate_fixture_annotations(
                manifest,
                checkpoint.atomic_records,
                checkpoint.relations,
                layered,
                expected_records=expected_records,
                expected_relations=expected_relations,
            )
            annotation_forward = annotation.forward
            annotation_backward = annotation.backward
        provider_forward = checkpoint.forward_semantic_check_status
        provider_backward = checkpoint.backward_semantic_check_status
        forward = combine_provider_and_annotation(provider_forward, annotation_forward)
        backward = combine_provider_and_annotation(provider_backward, annotation_backward)
        checkpoint.forward_semantic_check_status = forward
        checkpoint.backward_semantic_check_status = backward
        if not is_admitted_semantic_method(checkpoint.semantic_check_method):
            checkpoint.semantic_check_method = None
        if checkpoint.source_pages_sent < len(plan.pages):
            limitations.append(
                CoverageLimitation(
                    code="source_wire_pages_incomplete",
                    message="not every bounded source page was sent",
                )
            )
            checkpoint.processing_status = ProcessingJobStatus.PARTIAL
        structural_ok = True
        if layered is not None:
            structural = validate_result_against_manifest(manifest, layered)
            structural_ok = structural.ok
            referenced = len(collect_referenced_segment_ids(layered))
            layered = layered.model_copy(
                update={
                    "coverage": layered.coverage.model_copy(
                        update={
                            "processing_job_status": ProcessingJobStatus.RUNNING,
                            "forward_semantic_check_status": forward,
                            "backward_semantic_check_status": backward,
                            "semantic_check_method": checkpoint.semantic_check_method,
                            "snapshot_id": manifest.snapshot_id,
                            "source_segments_enumerated": len(manifest.segments),
                            "source_segments_referenced": referenced,
                            "structural_check_status": (
                                CheckStatus.COMPLETED if structural_ok else CheckStatus.FAILED
                            ),
                        }
                    )
                }
            )
            checkpoint.layered_partial = layered
        checks_done = forward == CheckStatus.COMPLETED and backward == CheckStatus.COMPLETED
        blocked = checkpoint.processing_status in {
            ProcessingJobStatus.PARTIAL,
            ProcessingJobStatus.FAILED,
            ProcessingJobStatus.CANCELLED,
        }
        if (
            checks_done
            and layered is not None
            and structural_ok
            and checkpoint.extract_complete
            and checkpoint.reconcile_complete
            and checkpoint.render_complete
            and checkpoint.check_complete
            and checkpoint.semantic_check_method is not None
            and not checkpoint.unprocessed_segment_ids
            and not blocked
            and checkpoint.source_pages_sent >= len(plan.pages)
        ):
            processing = ProcessingJobStatus.SUCCEEDED
        elif checkpoint.processing_status == ProcessingJobStatus.FAILED:
            processing = ProcessingJobStatus.FAILED
        else:
            processing = ProcessingJobStatus.PARTIAL
        if layered is not None:
            layered = layered.model_copy(
                update={
                    "coverage": layered.coverage.model_copy(
                        update={"processing_job_status": processing}
                    )
                }
            )
            checkpoint.layered_partial = layered
        annotation_failed = annotation is not None and (
            annotation.forward == CheckStatus.FAILED or annotation.backward == CheckStatus.FAILED
        )
        if not checks_done and checkpoint.semantic_check_method is None:
            limitations.append(
                CoverageLimitation(
                    code="semantic_check_unfinished",
                    message="source-aware semantic check did not complete both directions",
                )
            )
        elif annotation_failed:
            limitations.append(
                CoverageLimitation(
                    code="fixture_annotation_failed",
                    message="independent fixture annotation check failed",
                )
            )
        elif not checks_done:
            limitations.append(
                CoverageLimitation(
                    code="semantic_check_failed",
                    message="semantic check did not complete both directions",
                )
            )
        checkpoint.processing_status = processing
        checkpoint.touch()
        if processing == ProcessingJobStatus.PARTIAL:
            layered = _partial_layered(checkpoint)
        return self._result(manifest, checkpoint, trace, limitations, layered)


def engine_trace_json(trace: EngineRunTrace) -> str:
    payload = {
        "schema_identity": trace.schema_identity,
        "algorithm_identity": trace.algorithm_identity,
        "prompt_cache_key": trace.prompt_cache_key,
        "snapshot_content_hash": trace.manifest.snapshot_content_hash,
        "stage_calls": [
            {
                "operation": call.operation.value,
                "started_at": call.started_at.isoformat(),
                "finished_at": call.finished_at.isoformat() if call.finished_at else None,
                "error_category": call.error_category,
                "error": call.error,
                "attempt_count": call.attempt_count,
                "attempts": call.attempts,
                "observed_model": call.observed_model,
                "requested_model": call.requested_model,
                "serialized_utf8_bytes": call.serialized_utf8_bytes,
                "wire_audit": call.wire_audit.model_dump() if call.wire_audit else None,
            }
            for call in trace.stage_calls
        ],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


# Re-export for callers that caught the binding error from the engine module.
__all__ = [
    "CheckpointIncompatible",
    "EngineRunConfig",
    "EngineRunResult",
    "EngineRunTrace",
    "MeetingEssenceEngine",
    "engine_trace_json",
]
