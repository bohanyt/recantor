from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from recantor.meeting_essence.contracts import (
    AtomicRecord,
    DeadlineNormalizationBasis,
    EvidenceReference,
    SourceSnapshotManifest,
)
from recantor.meeting_essence.wire_payload import WireSegmentFragment


@dataclass
class ExtractValidationIssue:
    code: str
    message: str
    record_id: str = ""


@dataclass
class ExtractValidationReport:
    ok: bool = True
    issues: list[ExtractValidationIssue] = field(default_factory=list)

    def reject(self, code: str, message: str, *, record_id: str = "") -> None:
        self.ok = False
        self.issues.append(ExtractValidationIssue(code=code, message=message, record_id=record_id))


def _fragment_index(
    fragments: tuple[WireSegmentFragment, ...],
) -> dict[UUID, list[WireSegmentFragment]]:
    out: dict[UUID, list[WireSegmentFragment]] = {}
    for fragment in fragments:
        out.setdefault(fragment.segment_id, []).append(fragment)
    return out


def _fragment_bounds(fragment: WireSegmentFragment) -> tuple[int, int]:
    start = 0 if fragment.text_span_start_cp is None else fragment.text_span_start_cp
    if fragment.text_span_end_cp is None:
        return start, start + len(fragment.text)
    return start, fragment.text_span_end_cp


def _evidence_supported(
    evidence: EvidenceReference,
    fragments_by_segment: dict[UUID, list[WireSegmentFragment]],
    segment_times: dict[UUID, tuple[int, int]],
    segment_text: dict[UUID, str],
    segment_hash: dict[UUID, str],
) -> bool:
    if evidence.segment_id not in fragments_by_segment:
        return False
    expected_times = segment_times.get(evidence.segment_id)
    if expected_times is None or (evidence.start_ms, evidence.end_ms) != expected_times:
        return False
    full_text = segment_text.get(evidence.segment_id, "")
    if evidence.text_span is not None:
        span = evidence.text_span
        if span.end_cp > len(full_text):
            return False
        excerpt = full_text[span.start_cp : span.end_cp]
        if not excerpt.strip():
            return False
    for fragment in fragments_by_segment[evidence.segment_id]:
        if fragment.content_hash != segment_hash.get(evidence.segment_id):
            continue
        if (fragment.start_ms, fragment.end_ms) != expected_times:
            continue
        frag_start, frag_end = _fragment_bounds(fragment)
        if evidence.text_span is None:
            covers_all = (
                frag_start == 0 and frag_end >= len(full_text) and fragment.fragment_count == 1
            )
            if covers_all and fragment.text == full_text:
                return True
            continue
        span = evidence.text_span
        if span.start_cp < frag_start or span.end_cp > frag_end:
            continue
        local = fragment.text[span.start_cp - frag_start : span.end_cp - frag_start]
        expected = full_text[span.start_cp : span.end_cp]
        if local == expected and local.strip():
            return True
    return False


def _supporting_text(
    record: AtomicRecord,
    segment_text: dict[UUID, str],
) -> str:
    parts: list[str] = []
    for evidence in record.evidence:
        full = segment_text.get(evidence.segment_id, "")
        if evidence.text_span is None:
            parts.append(full)
            continue
        parts.append(full[evidence.text_span.start_cp : evidence.text_span.end_cp])
    return "\n".join(parts)


def _provenance(record: AtomicRecord) -> tuple[object, ...]:
    evidence_key = tuple(
        (
            str(ref.segment_id),
            ref.start_ms,
            ref.end_ms,
            None if ref.text_span is None else (ref.text_span.start_cp, ref.text_span.end_cp),
        )
        for ref in record.evidence
    )
    return (
        record.topic,
        record.kind.value,
        record.claim_text,
        record.content_status.value,
        record.assignee,
        record.deadline_raw,
        record.condition_text,
        evidence_key,
    )


def validate_extracted_records_for_request(
    manifest: SourceSnapshotManifest,
    records: list[AtomicRecord],
    *,
    session_id: UUID,
    page_fragments: tuple[WireSegmentFragment, ...],
    context_fragments: tuple[WireSegmentFragment, ...],
    known_records: dict[str, AtomicRecord],
    max_records: int,
) -> ExtractValidationReport:
    report = ExtractValidationReport(ok=True)
    if session_id != manifest.session_id:
        report.reject("foreign_session", "extract response session does not match manifest")
        return report
    if len(records) > max_records:
        report.reject("oversized_response", "extract response exceeds output cardinality limit")
        return report

    visible = page_fragments + context_fragments
    fragments_by_segment = _fragment_index(visible)
    segment_times = {
        segment.id: (segment.start_ms, segment.end_ms) for segment in manifest.segments
    }
    segment_text = {segment.id: segment.text for segment in manifest.segments}
    segment_hash = {segment.id: segment.content_hash for segment in manifest.segments}
    manifest_ids = set(segment_text)
    seen_ids: set[str] = set()
    seen_provenance: dict[tuple[object, ...], str] = {
        _provenance(record): record.record_id for record in known_records.values()
    }

    for record in records:
        if record.record_id in seen_ids:
            report.reject(
                "duplicate_record_id", "duplicate record id in response", record_id=record.record_id
            )
            continue
        seen_ids.add(record.record_id)
        prior = known_records.get(record.record_id)
        if prior is not None and _provenance(prior) != _provenance(record):
            report.reject(
                "duplicate_id_conflict",
                "record id reused with a different body",
                record_id=record.record_id,
            )
            continue
        provenance = _provenance(record)
        owner = seen_provenance.get(provenance)
        if owner is not None and owner != record.record_id:
            report.reject(
                "duplicate_provenance",
                "another record id already carries this provenance",
                record_id=record.record_id,
            )
            continue
        seen_provenance[provenance] = record.record_id
        if not record.evidence:
            report.reject(
                "missing_evidence", "atomic record requires evidence", record_id=record.record_id
            )
            continue
        for ref in record.evidence:
            if ref.segment_id not in manifest_ids:
                report.reject(
                    "foreign_segment",
                    "evidence references a segment outside the frozen snapshot",
                    record_id=record.record_id,
                )
                continue
            if (ref.start_ms, ref.end_ms) != segment_times[ref.segment_id]:
                report.reject(
                    "foreign_timing",
                    "evidence timing does not match the frozen segment",
                    record_id=record.record_id,
                )
                continue
            if not _evidence_supported(
                ref, fragments_by_segment, segment_times, segment_text, segment_hash
            ):
                report.reject(
                    "unsupported_evidence",
                    "evidence is not covered by visible fragment text",
                    record_id=record.record_id,
                )
        support = _supporting_text(record, segment_text)
        if record.assignee is not None and record.assignee not in support:
            report.reject(
                "unsupported_owner",
                "assignee is not present in supporting source text",
                record_id=record.record_id,
            )
        if record.deadline_raw is not None and record.deadline_raw not in support:
            report.reject(
                "unsupported_deadline",
                "raw deadline is not present in supporting source text",
                record_id=record.record_id,
            )
        if record.deadline_normalized is not None:
            basis = record.deadline_normalization_basis
            if basis == DeadlineNormalizationBasis.MEETING_DATE_TIMEZONE:
                meeting_date = manifest.meeting_metadata.get("meeting_date")
                timezone = manifest.meeting_metadata.get("timezone")
                if not meeting_date or not timezone:
                    report.reject(
                        "deadline_anchor_missing",
                        "meeting_date_timezone basis requires manifest meeting_date and timezone",
                        record_id=record.record_id,
                    )
            else:
                iso_date = record.deadline_normalized.date().isoformat()
                if iso_date not in support:
                    report.reject(
                        "unsupported_normalized_date",
                        "normalized deadline is not anchored in supporting source text",
                        record_id=record.record_id,
                    )
    return report


def _evidence_merge_key(ref: EvidenceReference) -> tuple[object, ...]:
    span = None
    if ref.text_span is not None:
        span = (ref.text_span.start_cp, ref.text_span.end_cp)
    return (ref.segment_id, ref.start_ms, ref.end_ms, span)


def merge_atomic_records(
    existing: list[AtomicRecord], incoming: list[AtomicRecord]
) -> list[AtomicRecord]:
    merged: dict[str, AtomicRecord] = {record.record_id: record for record in existing}
    for record in incoming:
        prior = merged.get(record.record_id)
        if prior is None:
            merged[record.record_id] = record
            continue
        if prior == record:
            continue
        if (
            prior.claim_text != record.claim_text
            or prior.content_status != record.content_status
            or prior.kind != record.kind
        ):
            continue
        evidence_keys = {_evidence_merge_key(ref) for ref in prior.evidence}
        combined = list(prior.evidence)
        for ref in record.evidence:
            key = _evidence_merge_key(ref)
            if key in evidence_keys:
                continue
            evidence_keys.add(key)
            combined.append(ref)
        merged[record.record_id] = prior.model_copy(update={"evidence": combined})
    return list(merged.values())
