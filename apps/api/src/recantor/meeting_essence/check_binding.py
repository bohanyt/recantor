"""Fingerprints and accessible-claim binding for source-aware check pages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from recantor.meeting_essence.contracts import (
    AtomicRecord,
    EvidenceReference,
    LayeredMeetingResult,
    ManifestSegmentEntry,
    RecordRelation,
    RecordRelationType,
    SourceSnapshotManifest,
)
from recantor.meeting_essence.identity import segment_content_hash
from recantor.meeting_essence.source_reader import split_segment_fragments
from recantor.meeting_essence.stage_contracts import (
    AccessibleOutputClaim,
    CheckDirection,
    CheckPass,
    CheckResultBinding,
    evidence_provenance_key,
    relation_wire_key,
)
from recantor.meeting_essence.wire_payload import WireSegmentFragment

SOURCE_AWARE_METHOD = "source-aware-binding-v1"
OFFLINE_FIXTURE_METHOD = "offline-fixture-annotation-v1"

ADMITTED_SEMANTIC_METHODS = frozenset({SOURCE_AWARE_METHOD, OFFLINE_FIXTURE_METHOD})


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def fragment_bounds(fragment: WireSegmentFragment) -> tuple[int, int]:
    start = 0 if fragment.text_span_start_cp is None else fragment.text_span_start_cp
    if fragment.text_span_end_cp is None:
        return start, start + len(fragment.text)
    return start, fragment.text_span_end_cp


def fragment_key(fragment: WireSegmentFragment) -> str:
    start, end = fragment_bounds(fragment)
    return f"{fragment.segment_id}:{fragment.sequence}:{start}:{end}:{fragment.content_hash}"


def fragment_span_record(fragment: WireSegmentFragment) -> dict[str, object]:
    start, end = fragment_bounds(fragment)
    return {
        "segment_id": str(fragment.segment_id),
        "sequence": fragment.sequence,
        "start_cp": start,
        "end_cp": end,
        "content_hash": fragment.content_hash,
        "start_ms": fragment.start_ms,
        "end_ms": fragment.end_ms,
    }


def manifest_fragments(
    manifest: SourceSnapshotManifest, *, max_chars: int
) -> list[WireSegmentFragment]:
    fragments: list[WireSegmentFragment] = []
    for segment in sorted(manifest.segments, key=lambda item: item.sequence):
        fragments.extend(split_segment_fragments(segment, max_chars=max_chars))
    return fragments


def is_admitted_semantic_method(method: str | None) -> bool:
    """Allow-list. Unregistered names, including structural or mock labels, are rejected."""
    if method is None:
        return False
    return method.strip() in ADMITTED_SEMANTIC_METHODS


def evidence_key(evidence: EvidenceReference) -> tuple[object, ...]:
    span = None
    if evidence.text_span is not None:
        span = (evidence.text_span.start_cp, evidence.text_span.end_cp)
    return (str(evidence.segment_id), evidence.start_ms, evidence.end_ms, span)


def _fragment_material(fragment: WireSegmentFragment) -> dict[str, object]:
    start, end = fragment_bounds(fragment)
    return {
        "segment_id": str(fragment.segment_id),
        "sequence": fragment.sequence,
        "start_ms": fragment.start_ms,
        "end_ms": fragment.end_ms,
        "content_hash": fragment.content_hash,
        "start_cp": start,
        "end_cp": end,
        "text": fragment.text,
    }


def claim_identity(
    *,
    surface: str,
    text: str,
    content_status: str | None,
    superseded: bool,
    evidence: list[EvidenceReference],
) -> str:
    material = {
        "surface": surface,
        "text": text,
        "content_status": content_status,
        "superseded": superseded,
        "evidence": [list(evidence_key(item)) for item in evidence],
    }
    return "clm_" + _sha256(material)[:20]


def bind_check_page(
    *,
    snapshot_content_hash: str,
    check_pass: CheckPass,
    fragments: tuple[WireSegmentFragment, ...] | list[WireSegmentFragment],
    claims: list[AccessibleOutputClaim],
    records: list[AtomicRecord],
    relations: list[RecordRelation],
    page_cursor: str,
    judged_evidence_keys: tuple[str, ...] = (),
    relation_slice_keys: tuple[str, ...] = (),
) -> CheckResultBinding:
    direction = (
        CheckDirection.SOURCE_TO_OUTPUT
        if check_pass == CheckPass.SOURCE_COVERAGE
        else CheckDirection.OUTPUT_TO_SOURCE
    )
    fragment_list = list(fragments)
    source_keys = tuple(fragment_key(fragment) for fragment in fragment_list)
    claim_ids = tuple(claim.claim_id for claim in claims)
    record_ids = tuple(record.record_id for record in records)
    relation_keys = tuple(relation_wire_key(relation) for relation in relations)
    judged = tuple(judged_evidence_keys)
    slice_keys = tuple(relation_slice_keys)
    source_fingerprint = _sha256([_fragment_material(fragment) for fragment in fragment_list])
    result_fingerprint = _sha256(
        {
            "claims": [claim.model_dump(mode="json") for claim in claims],
            "records": [record.model_dump(mode="json") for record in records],
            "relations": [relation.model_dump(mode="json") for relation in relations],
            "judged_evidence_keys": list(judged),
        }
    )
    request_fingerprint = _sha256(
        {
            "snapshot_content_hash": snapshot_content_hash,
            "check_pass": check_pass.value,
            "direction": direction.value,
            "page_cursor": page_cursor,
            "source_fragment_keys": list(source_keys),
            "output_claim_ids": list(claim_ids),
            "record_ids": list(record_ids),
            "relation_keys": list(relation_keys),
            "judged_evidence_keys": list(judged),
            "relation_slice_keys": list(slice_keys),
            "source_fingerprint": source_fingerprint,
            "result_fingerprint": result_fingerprint,
        }
    )
    return CheckResultBinding(
        snapshot_content_hash=snapshot_content_hash,
        check_pass=check_pass,
        direction=direction,
        page_cursor=page_cursor,
        source_fragment_keys=source_keys,
        output_claim_ids=claim_ids,
        record_ids=record_ids,
        relation_keys=relation_keys,
        judged_evidence_keys=judged,
        relation_slice_keys=slice_keys,
        request_fingerprint=request_fingerprint,
        source_fingerprint=source_fingerprint,
        result_fingerprint=result_fingerprint,
    )


def receipt_acknowledges(
    binding: CheckResultBinding,
    *,
    covered_fragment_keys: tuple[str, ...] | list[str],
    covered_claim_ids: tuple[str, ...] | list[str],
    covered_record_ids: tuple[str, ...] | list[str],
    covered_relation_keys: tuple[str, ...] | list[str],
) -> bool:
    """Exact acknowledgement of this request. Another pass or an older page does not count."""
    return (
        set(covered_fragment_keys) == set(binding.source_fragment_keys)
        and set(covered_claim_ids) == set(binding.output_claim_ids)
        and set(covered_record_ids) == set(binding.record_ids)
        and set(covered_relation_keys) == set(binding.relation_keys)
    )


def binding_matches_response(
    binding: CheckResultBinding,
    *,
    request_fingerprint: str,
    source_fingerprint: str,
    result_fingerprint: str,
    check_pass: CheckPass,
    direction: CheckDirection,
) -> bool:
    return (
        binding.request_fingerprint == request_fingerprint
        and binding.source_fingerprint == source_fingerprint
        and binding.result_fingerprint == result_fingerprint
        and binding.check_pass == check_pass
        and binding.direction == direction
    )


def evidence_on_fragments(
    evidence: EvidenceReference,
    fragments: tuple[WireSegmentFragment, ...] | list[WireSegmentFragment],
) -> bool | None:
    """True or False when this page can decide the citation. None when the segment is absent."""
    matched = [
        fragment
        for fragment in fragments
        if fragment.segment_id == evidence.segment_id
        and fragment.start_ms == evidence.start_ms
        and fragment.end_ms == evidence.end_ms
    ]
    if not matched:
        return None
    if evidence.text_span is None:
        for fragment in matched:
            start, _end = fragment_bounds(fragment)
            if start == 0 and fragment.fragment_count == 1 and fragment.text_span_start_cp is None:
                return True
        return None
    span = evidence.text_span
    saw_overlap = False
    for fragment in matched:
        start, end = fragment_bounds(fragment)
        if span.end_cp <= start or span.start_cp >= end:
            continue
        saw_overlap = True
        if span.start_cp < start or span.end_cp > end:
            continue
        excerpt = fragment.text[span.start_cp - start : span.end_cp - start]
        return bool(excerpt.strip())
    if saw_overlap:
        return False
    return None


def evidence_overlaps_fragment(
    evidence: EvidenceReference,
    fragment: WireSegmentFragment,
) -> bool:
    if evidence.segment_id != fragment.segment_id:
        return False
    if evidence.start_ms != fragment.start_ms or evidence.end_ms != fragment.end_ms:
        return False
    if evidence.text_span is None:
        return True
    start, end = fragment_bounds(fragment)
    return evidence.text_span.start_cp < end and evidence.text_span.end_cp > start


def records_touching_fragments(
    records: list[AtomicRecord],
    fragments: list[WireSegmentFragment],
) -> list[AtomicRecord]:
    touched: list[AtomicRecord] = []
    for record in records:
        if any(
            evidence_overlaps_fragment(evidence, fragment)
            for evidence in record.evidence
            for fragment in fragments
        ):
            touched.append(record)
    return touched


def claims_touching_fragments(
    claims: list[AccessibleOutputClaim],
    fragments: list[WireSegmentFragment],
) -> list[AccessibleOutputClaim]:
    return [
        claim
        for claim in claims
        if any(
            evidence_overlaps_fragment(evidence, fragment)
            for evidence in claim.evidence
            for fragment in fragments
        )
    ]


def support_fragments_for_claim(
    claim: AccessibleOutputClaim,
    fragments: list[WireSegmentFragment],
) -> list[WireSegmentFragment]:
    needed: list[WireSegmentFragment] = []
    seen: set[str] = set()
    for evidence in claim.evidence:
        for fragment in fragments:
            if fragment.segment_id != evidence.segment_id:
                continue
            if fragment.start_ms != evidence.start_ms or fragment.end_ms != evidence.end_ms:
                continue
            start, end = fragment_bounds(fragment)
            if evidence.text_span is None:
                include = True
            else:
                include = evidence.text_span.start_cp < end and evidence.text_span.end_cp > start
            if not include:
                continue
            key = fragment_key(fragment)
            if key in seen:
                continue
            seen.add(key)
            needed.append(fragment)
    return needed


def superseded_ids(relations: list[RecordRelation]) -> set[str]:
    blocked: set[str] = set()
    for relation in relations:
        if relation.relation_type in {RecordRelationType.SUPERSEDES, RecordRelationType.CANCELS}:
            blocked.add(relation.to_record_id)
    return blocked


def _iso_deadline(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def accessible_claims_from_result(result: LayeredMeetingResult) -> list[AccessibleOutputClaim]:
    """Overview, topic points, actions, and open items. The raw ledger is not a claim."""
    claims: list[AccessibleOutputClaim] = []
    for bullet in result.overview_bullets:
        claims.append(
            AccessibleOutputClaim(
                claim_id=claim_identity(
                    surface="overview",
                    text=bullet.text,
                    content_status=None,
                    superseded=False,
                    evidence=list(bullet.evidence),
                ),
                surface="overview",
                text=bullet.text,
                evidence=list(bullet.evidence),
            )
        )
    for topic in result.topics:
        for point in topic.points:
            status = point.content_status.value if point.content_status is not None else None
            kind = point.kind.value if point.kind is not None else None
            claims.append(
                AccessibleOutputClaim(
                    claim_id=claim_identity(
                        surface="topic",
                        text=point.text,
                        content_status=status,
                        superseded=point.superseded,
                        evidence=list(point.evidence),
                    ),
                    surface="topic",
                    text=point.text,
                    topic=topic.topic,
                    kind=kind,
                    content_status=status,
                    condition_text=point.condition_text,
                    rationale=point.rationale,
                    uncertainty=point.uncertainty,
                    assignee=point.assignee,
                    assignee_explicitly_undetermined=point.assignee_explicitly_undetermined,
                    deadline_raw=point.deadline_raw,
                    deadline_normalized=_iso_deadline(point.deadline_normalized),
                    deadline_unmentioned=point.deadline_unmentioned,
                    deadline_normalization_basis=point.deadline_normalization_basis.value,
                    superseded=point.superseded,
                    evidence=list(point.evidence),
                )
            )
    for action in result.actions:
        claims.append(
            AccessibleOutputClaim(
                claim_id=claim_identity(
                    surface="action",
                    text=action.task,
                    content_status=action.content_status.value,
                    superseded=False,
                    evidence=list(action.evidence),
                ),
                surface="action",
                text=action.task,
                kind="action",
                content_status=action.content_status.value,
                condition_text=action.condition_text,
                assignee=action.assignee,
                assignee_explicitly_undetermined=action.assignee_explicitly_undetermined,
                deadline_raw=action.deadline_raw,
                deadline_normalized=_iso_deadline(action.deadline_normalized),
                deadline_unmentioned=action.deadline_unmentioned,
                deadline_normalization_basis=action.deadline_normalization_basis.value,
                evidence=list(action.evidence),
            )
        )
    for item in result.open_items:
        claims.append(
            AccessibleOutputClaim(
                claim_id=claim_identity(
                    surface="open_item",
                    text=item.text,
                    content_status=None,
                    superseded=False,
                    evidence=list(item.evidence),
                ),
                surface="open_item",
                text=item.text,
                evidence=list(item.evidence),
            )
        )
    return claims


@dataclass(frozen=True)
class RelationContextSlice:
    """One bounded citation excerpt for one relationship."""

    relation_index: int
    evidence_key: str
    fragment: WireSegmentFragment
    receipt_key: str


@dataclass(frozen=True)
class RelationContextPlan:
    slices: tuple[RelationContextSlice, ...] = ()
    invalid: bool = False
    exceeds: bool = False


def endpoint_records(
    records: list[AtomicRecord], relation: RecordRelation
) -> list[AtomicRecord] | None:
    by_id = {record.record_id: record for record in records}
    source = by_id.get(relation.from_record_id)
    target = by_id.get(relation.to_record_id)
    if source is None or target is None:
        return None
    return [source, target]


def _citation_bounds(
    evidence: EvidenceReference, segment: ManifestSegmentEntry | None
) -> tuple[int, int] | None:
    length = len(segment.text) if segment is not None else None
    if segment is not None and (evidence.start_ms, evidence.end_ms) != (
        segment.start_ms,
        segment.end_ms,
    ):
        return None
    if evidence.text_span is None:
        if length is None:
            return (0, 10**12)
        return (0, length)
    start = evidence.text_span.start_cp
    end = evidence.text_span.end_cp
    if length is not None and end > length:
        return None
    return (start, end)


def fragment_slice_supports(
    evidence: EvidenceReference,
    fragments: tuple[WireSegmentFragment, ...] | list[WireSegmentFragment],
    segment: ManifestSegmentEntry | None,
) -> bool:
    """True when a fragment is an original excerpt of this citation, including a bounded slice."""
    bounds = _citation_bounds(evidence, segment)
    if bounds is None:
        return False
    cite_start, cite_end = bounds
    for fragment in fragments:
        if fragment.segment_id != evidence.segment_id:
            continue
        if fragment.start_ms != evidence.start_ms or fragment.end_ms != evidence.end_ms:
            continue
        if segment is not None and fragment.content_hash != segment.content_hash:
            continue
        start, end = fragment_bounds(fragment)
        overlap_start = max(start, cite_start)
        overlap_end = min(end, cite_end)
        if overlap_end <= overlap_start:
            continue
        if segment is None:
            if fragment.text.strip():
                return True
            continue
        if fragment.text != segment.text[start:end]:
            continue
        excerpt = segment.text[overlap_start:overlap_end]
        if excerpt.strip():
            return True
    return False


def relation_page_covers_citations(
    relations: list[RecordRelation],
    records: list[AtomicRecord],
    fragments: tuple[WireSegmentFragment, ...] | list[WireSegmentFragment],
    judged_evidence_keys: tuple[str, ...] | list[str],
    manifest: SourceSnapshotManifest | None,
) -> bool:
    """A page that carries a relationship must include both endpoints and the cited source slice."""
    if not relations:
        return True
    judged = set(judged_evidence_keys)
    if not judged:
        return False
    record_ids = {record.record_id for record in records}
    by_id = {segment.id: segment for segment in manifest.segments} if manifest is not None else {}
    seen: set[str] = set()
    for relation in relations:
        if relation.from_record_id not in record_ids or relation.to_record_id not in record_ids:
            return False
        for evidence in relation.evidence:
            key = evidence_provenance_key(evidence)
            if key not in judged:
                continue
            seen.add(key)
            segment = by_id.get(evidence.segment_id) if manifest is not None else None
            if manifest is not None and segment is None:
                return False
            if not fragment_slice_supports(evidence, fragments, segment):
                return False
    return seen == judged


def _citation_fragments(
    evidence: EvidenceReference,
    segment: ManifestSegmentEntry,
    *,
    max_chars: int,
) -> list[WireSegmentFragment] | None:
    if (evidence.start_ms, evidence.end_ms) != (segment.start_ms, segment.end_ms):
        return None
    if segment.content_hash != segment_content_hash(segment.text):
        return None
    text = segment.text
    if evidence.text_span is None:
        start, end = 0, len(text)
    else:
        start = evidence.text_span.start_cp
        end = evidence.text_span.end_cp
        if end > len(text) or end <= start:
            return None
    if end <= start or not text[start:end].strip():
        return None
    windows: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + max_chars, end)
        windows.append((cursor, window_end))
        cursor = window_end
    total = len(windows)
    fragments: list[WireSegmentFragment] = []
    for index, (span_start, span_end) in enumerate(windows):
        fragments.append(
            WireSegmentFragment(
                segment_id=segment.id,
                sequence=segment.sequence,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=text[span_start:span_end],
                content_hash=segment.content_hash,
                fragment_index=index,
                fragment_count=total,
                text_span_start_cp=span_start,
                text_span_end_cp=span_end,
            )
        )
    return fragments


def build_relation_context_slices(
    relations: list[RecordRelation],
    manifest: SourceSnapshotManifest,
    *,
    max_chars: int,
) -> list[RelationContextSlice] | None:
    """Citation slices only. None when a citation is absent, mistimed, or outside the segment."""
    if max_chars < 1:
        return None
    by_id = {segment.id: segment for segment in manifest.segments}
    slices: list[RelationContextSlice] = []
    for relation_index, relation in enumerate(relations):
        rel_key = relation_wire_key(relation)
        for evidence in relation.evidence:
            segment = by_id.get(evidence.segment_id)
            if segment is None:
                return None
            fragments = _citation_fragments(evidence, segment, max_chars=max_chars)
            if not fragments:
                return None
            evidence_key = evidence_provenance_key(evidence)
            for fragment in fragments:
                receipt_key = f"{len(slices)}|{rel_key}|{fragment_key(fragment)}"
                slices.append(
                    RelationContextSlice(
                        relation_index=relation_index,
                        evidence_key=evidence_key,
                        fragment=fragment,
                        receipt_key=receipt_key,
                    )
                )
    return slices
