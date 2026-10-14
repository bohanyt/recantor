from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from recantor.meeting_essence.check_binding import OFFLINE_FIXTURE_METHOD
from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CheckStatus,
    EvidenceReference,
    MeetingIntelligenceExampleBundle,
    ProcessingJobStatus,
    RecordRelation,
    StageOperation,
)
from recantor.meeting_essence.render_builder import build_layered_from_ledger
from recantor.meeting_essence.semantic_check import request_page_supported
from recantor.meeting_essence.stage_contracts import (
    CheckStageRequest,
    CheckStageResponse,
    ExtractStageRequest,
    ExtractStageResponse,
    ReconcileStageRequest,
    ReconcileStageResponse,
    RenderStageRequest,
    RenderStageResponse,
    StageRequest,
    StageResponse,
)
from recantor.meeting_essence.wire_payload import SourceDescriptor, WireSegmentFragment

STRUCTURAL_CHECK_METHOD = "deterministic-structural-ledger-v1"


def _fragment_covers(evidence: EvidenceReference, fragment: WireSegmentFragment) -> bool:
    if fragment.segment_id != evidence.segment_id:
        return False
    if fragment.start_ms != evidence.start_ms or fragment.end_ms != evidence.end_ms:
        return False
    start = 0 if fragment.text_span_start_cp is None else fragment.text_span_start_cp
    end = (
        start + len(fragment.text)
        if fragment.text_span_end_cp is None
        else fragment.text_span_end_cp
    )
    if evidence.text_span is None:
        return fragment.fragment_count == 1 and start == 0 and fragment.text_span_start_cp is None
    span = evidence.text_span
    if span.start_cp < start or span.end_cp > end:
        return False
    excerpt = fragment.text[span.start_cp - start : span.end_cp - start]
    return bool(excerpt.strip())


def _exact_coverage(request: CheckStageRequest) -> dict[str, tuple[str, ...]]:
    binding = request.result_binding
    return {
        "covered_fragment_keys": binding.source_fragment_keys,
        "covered_claim_ids": binding.output_claim_ids,
        "covered_record_ids": binding.record_ids,
        "covered_relation_keys": binding.relation_keys,
    }


def _record_visible(record: AtomicRecord, fragments: tuple[WireSegmentFragment, ...]) -> bool:
    if not record.evidence:
        return False
    return all(
        any(_fragment_covers(ref, fragment) for fragment in fragments) for ref in record.evidence
    )


@dataclass
class DeterministicMeetingEssenceProvider:
    """Fixture provider. Extraction annotations are separate from evaluation records.

    Render uses only the records on the request. It does not read ``gold.result``.
    """

    gold: MeetingIntelligenceExampleBundle | None = None
    extract_by_segment: dict[UUID, list[AtomicRecord]] = field(default_factory=dict)
    extraction_records: list[AtomicRecord] = field(default_factory=list)
    evaluation_records: list[AtomicRecord] | None = None
    relation_annotations: list[RecordRelation] = field(default_factory=list)
    semantic_check_method: str = OFFLINE_FIXTURE_METHOD
    fail_check: bool = False
    call_count: int = 0
    max_wire_utf8_bytes: int | None = None
    drop_record_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.gold is None:
            return
        if not self.extraction_records:
            self.extraction_records = [record.model_copy(deep=True) for record in self.gold.records]
        if self.evaluation_records is None and self.gold.records:
            self.evaluation_records = [record.model_copy(deep=True) for record in self.gold.records]
        if not self.relation_annotations:
            self.relation_annotations = [
                relation.model_copy(deep=True) for relation in self.gold.relations
            ]
        if not self.extract_by_segment:
            mapping: dict[UUID, list[AtomicRecord]] = {}
            for record in self.extraction_records:
                for evidence in record.evidence:
                    mapping.setdefault(evidence.segment_id, []).append(record)
            self.extract_by_segment = mapping

    def _records_for_fragments(self, request: ExtractStageRequest) -> list[AtomicRecord]:
        visible = request.segment_fragments + request.context_fragments
        omitted = set(request.omitted_record_ids) | set(self.drop_record_ids)
        emitted: list[AtomicRecord] = []
        seen: set[str] = set()
        candidates: list[AtomicRecord] = []
        seen_candidates: set[str] = set()
        for fragment in request.segment_fragments:
            for record in self.extract_by_segment.get(fragment.segment_id, []):
                if record.record_id in seen_candidates:
                    continue
                seen_candidates.add(record.record_id)
                candidates.append(record)
        for record in candidates:
            if record.record_id in omitted or record.record_id in seen:
                continue
            if not _record_visible(record, visible):
                continue
            seen.add(record.record_id)
            emitted.append(record.model_copy(deep=True))
        limit = request.budget.max_records
        return emitted[:limit]

    def _enforce_wire_budget(self, request: StageRequest) -> None:
        if self.max_wire_utf8_bytes is None:
            return
        from recantor.meeting_essence.wire_serialize import serialized_utf8_byte_length

        size = serialized_utf8_byte_length(request)
        if size > self.max_wire_utf8_bytes:
            msg = f"wire budget exceeded: {size} utf8 bytes > {self.max_wire_utf8_bytes}"
            raise ValueError(msg)

    async def run_stage(
        self,
        *,
        operation: StageOperation,
        request: StageRequest,
        prompt_bundle: object,
        job: object,
    ) -> StageResponse:
        del prompt_bundle, job
        self.call_count += 1
        self._enforce_wire_budget(request)
        if operation == StageOperation.EXTRACT:
            assert isinstance(request, ExtractStageRequest)
            return ExtractStageResponse(extracted_records=self._records_for_fragments(request))
        if operation == StageOperation.RECONCILE:
            assert isinstance(request, ReconcileStageRequest)
            present = {record.record_id for record in request.atomic_records}
            relations = [
                relation.model_copy(deep=True)
                for relation in self.relation_annotations
                if relation.from_record_id in present and relation.to_record_id in present
            ]
            return ReconcileStageResponse(
                reconciled_records=[
                    record.model_copy(deep=True) for record in request.atomic_records
                ],
                relations=relations,
            )
        if operation == StageOperation.CHECK:
            assert isinstance(request, CheckStageRequest)
            binding = request.result_binding
            coverage = _exact_coverage(request)
            fingerprints = {
                "request_fingerprint": binding.request_fingerprint,
                "source_fingerprint": binding.source_fingerprint,
                "result_fingerprint": binding.result_fingerprint,
            }
            if self.evaluation_records is None:
                return CheckStageResponse(
                    forward_semantic_check_status=CheckStatus.NOT_STARTED,
                    backward_semantic_check_status=CheckStatus.NOT_STARTED,
                    semantic_check_method=STRUCTURAL_CHECK_METHOD,
                    check_pass=request.check_pass,
                    direction=request.direction,
                    **fingerprints,
                )
            if self.fail_check:
                return CheckStageResponse(
                    forward_semantic_check_status=CheckStatus.FAILED,
                    backward_semantic_check_status=CheckStatus.NOT_STARTED,
                    semantic_check_method=OFFLINE_FIXTURE_METHOD,
                    check_pass=request.check_pass,
                    direction=request.direction,
                    **coverage,
                    **fingerprints,
                )
            supported = request_page_supported(
                records=list(request.atomic_records),
                relations=list(request.relations),
                claims=list(request.output_claims),
                fragments=request.source_fragments,
                expected_records=self.evaluation_records,
                expected_relations=self.relation_annotations,
                manifest=self.gold.manifest if self.gold is not None else None,
                judged_evidence_keys=binding.judged_evidence_keys,
            )
            status = CheckStatus.COMPLETED if supported else CheckStatus.FAILED
            return CheckStageResponse(
                forward_semantic_check_status=status,
                backward_semantic_check_status=status,
                semantic_check_method=OFFLINE_FIXTURE_METHOD,
                check_pass=request.check_pass,
                direction=request.direction,
                **coverage,
                **fingerprints,
            )
        if operation == StageOperation.RENDER:
            assert isinstance(request, RenderStageRequest)
            if not isinstance(request.source, SourceDescriptor):
                msg = "render source must be a descriptor"
                raise ValueError(msg)
            if not request.atomic_records:
                msg = "render request has no atomic records"
                raise ValueError(msg)
            layered = build_layered_from_ledger(
                snapshot_id=request.source.snapshot_id,
                enumerated_segment_count=request.source.enumerated_segment_count,
                records=list(request.atomic_records),
                relations=list(request.relations),
                processing_status=ProcessingJobStatus.RUNNING,
                forward_check=CheckStatus.NOT_STARTED,
                backward_check=CheckStatus.NOT_STARTED,
                semantic_check_method=None,
            )
            return RenderStageResponse(layered_result=layered)
        msg = f"unsupported operation {operation}"
        raise ValueError(msg)
