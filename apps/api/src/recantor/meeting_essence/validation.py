from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CheckStatus,
    DeadlineNormalizationBasis,
    EvidenceReference,
    LayeredMeetingResult,
    ManifestSegmentEntry,
    MeetingIntelligenceExampleBundle,
    RecordRelation,
    SourceSnapshotManifest,
)
from recantor.meeting_essence.identity import (
    compute_snapshot_content_hash,
    compute_snapshot_id,
    segment_content_hash,
)

_RELATIVE_DEADLINE_MARKERS = ("besok", "tomorrow", "lusa")


@dataclass
class ValidationIssue:
    code: str
    message: str
    path: str = ""


@dataclass
class ValidationReport:
    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    def add(self, code: str, message: str, path: str = "") -> None:
        self.issues.append(ValidationIssue(code=code, message=message, path=path))
        self.ok = False


def _segment_by_id(manifest: SourceSnapshotManifest) -> dict[UUID, ManifestSegmentEntry]:
    return {segment.id: segment for segment in manifest.segments}


def _validate_evidence_ref(
    manifest: SourceSnapshotManifest,
    ref: EvidenceReference,
    path: str,
    report: ValidationReport,
) -> None:
    segments = _segment_by_id(manifest)
    if ref.segment_id not in segments:
        report.add("missing_segment", f"unknown segment_id {ref.segment_id}", path)
        return
    segment = segments[ref.segment_id]
    if ref.start_ms != segment.start_ms or ref.end_ms != segment.end_ms:
        report.add(
            "evidence_time_not_exact",
            "evidence start_ms/end_ms must exactly match canonical segment timestamps",
            path,
        )
    if ref.text_span is not None:
        length = len(segment.text)
        if ref.text_span.start_cp > length or ref.text_span.end_cp > length:
            report.add(
                "offset_out_of_bounds",
                f"text span [{ref.text_span.start_cp},{ref.text_span.end_cp}) exceeds "
                f"codepoint length {length}",
                path,
            )
        excerpt = segment.text[ref.text_span.start_cp : ref.text_span.end_cp]
        if not excerpt:
            report.add("empty_text_span", "text span selects no characters", path)


def _walk_evidence(
    manifest: SourceSnapshotManifest,
    evidence: list[EvidenceReference],
    path: str,
    report: ValidationReport,
) -> None:
    for index, ref in enumerate(evidence):
        _validate_evidence_ref(manifest, ref, f"{path}[{index}]", report)


def _relative_deadline_wording(raw: str | None) -> bool:
    if raw is None:
        return False
    lowered = raw.casefold()
    return any(marker in lowered for marker in _RELATIVE_DEADLINE_MARKERS)


def _validate_deadline_fields_against_manifest(
    manifest: SourceSnapshotManifest,
    *,
    deadline_raw: str | None,
    deadline_normalized: object | None,
    deadline_normalization_basis: DeadlineNormalizationBasis,
    path: str,
    report: ValidationReport,
) -> None:
    if deadline_normalized is None:
        return
    if deadline_normalization_basis == DeadlineNormalizationBasis.MEETING_DATE_TIMEZONE:
        meeting_date = manifest.meeting_metadata.get("meeting_date")
        timezone = manifest.meeting_metadata.get("timezone")
        if not meeting_date or not timezone:
            report.add(
                "deadline_anchor_missing",
                "meeting_date_timezone basis requires manifest.meeting_metadata "
                "meeting_date and timezone",
                path,
            )
    elif deadline_normalization_basis == DeadlineNormalizationBasis.ABSOLUTE_SOURCE:
        if _relative_deadline_wording(deadline_raw):
            report.add(
                "deadline_relative_without_basis",
                "relative deadline_raw cannot use absolute_source normalization basis",
                path,
            )
    if _relative_deadline_wording(deadline_raw) and (
        deadline_normalization_basis != DeadlineNormalizationBasis.MEETING_DATE_TIMEZONE
    ):
        report.add(
            "deadline_relative_without_basis",
            (
                "normalized relative deadlines require meeting_date_timezone basis "
                "and manifest anchors"
            ),
            path,
        )


def validate_manifest_integrity(manifest: SourceSnapshotManifest) -> ValidationReport:
    report = ValidationReport(ok=True)
    expected_hash = compute_snapshot_content_hash(manifest)
    if manifest.snapshot_content_hash != expected_hash:
        report.add(
            "snapshot_hash_mismatch",
            "snapshot_content_hash does not match deterministic manifest body",
            "manifest.snapshot_content_hash",
        )
    expected_snapshot_id = compute_snapshot_id(str(manifest.session_id), expected_hash)
    if manifest.snapshot_id != expected_snapshot_id:
        report.add(
            "snapshot_id_mismatch",
            "snapshot_id does not match session_id and snapshot_content_hash derivation",
            "manifest.snapshot_id",
        )

    for index, segment in enumerate(manifest.segments):
        expected_segment_hash = segment_content_hash(segment.text)
        if segment.content_hash != expected_segment_hash:
            report.add(
                "segment_hash_mismatch",
                "segment content_hash does not match exact UTF-8 text",
                f"manifest.segments[{index}].content_hash",
            )

    if manifest.segments:
        max_sequence = max(segment.sequence for segment in manifest.segments)
        if manifest.publication_boundary_sequence < max_sequence:
            report.add(
                "publication_boundary_too_low",
                "publication_boundary_sequence must cover the highest present segment sequence",
                "manifest.publication_boundary_sequence",
            )

    for gap_index, gap in enumerate(manifest.coverage.explicit_gaps):
        if (
            gap.sequence_start is not None
            and gap.sequence_end is not None
            and gap.sequence_end < gap.sequence_start
        ):
            report.add(
                "invalid_gap_range",
                "gap sequence_end must be >= sequence_start",
                f"manifest.coverage.explicit_gaps[{gap_index}]",
            )
    return report


def collect_referenced_segment_ids(result: LayeredMeetingResult) -> set[UUID]:
    ids: set[UUID] = set()

    def absorb(evidence: list[EvidenceReference]) -> None:
        ids.update(ref.segment_id for ref in evidence)

    for bullet in result.overview_bullets:
        absorb(bullet.evidence)
    for topic in result.topics:
        for point in topic.points:
            absorb(point.evidence)
    for action in result.actions:
        absorb(action.evidence)
    for item in result.open_items:
        absorb(item.evidence)
    for record in result.atomic_records:
        absorb(record.evidence)
    for relation in result.relations:
        absorb(relation.evidence)
    return ids


def validate_coverage_report(
    manifest: SourceSnapshotManifest,
    result: LayeredMeetingResult,
) -> ValidationReport:
    report = ValidationReport(ok=True)
    coverage = result.coverage
    if coverage.snapshot_id != manifest.snapshot_id:
        report.add(
            "coverage_snapshot_mismatch",
            "coverage.snapshot_id must match manifest.snapshot_id",
            "result.coverage.snapshot_id",
        )

    expected_enumerated = len(manifest.segments)
    if coverage.source_segments_enumerated != expected_enumerated:
        report.add(
            "coverage_enumerated_mismatch",
            f"source_segments_enumerated must equal manifest segment count ({expected_enumerated})",
            "result.coverage.source_segments_enumerated",
        )

    referenced = collect_referenced_segment_ids(result)
    if coverage.source_segments_referenced != len(referenced):
        report.add(
            "coverage_referenced_mismatch",
            "source_segments_referenced must equal uniquely referenced segment ids",
            "result.coverage.source_segments_referenced",
        )

    manifest_ids = {segment.id for segment in manifest.segments}
    for limitation_index, limitation in enumerate(coverage.limitations):
        for segment_id in limitation.evidence_segment_ids:
            if segment_id not in manifest_ids:
                report.add(
                    "limitation_unknown_segment",
                    f"limitation references unknown segment {segment_id}",
                    f"result.coverage.limitations[{limitation_index}]",
                )

    completed_semantic = {
        coverage.forward_semantic_check_status,
        coverage.backward_semantic_check_status,
    }
    if CheckStatus.COMPLETED in completed_semantic and not coverage.semantic_check_method:
        report.add(
            "semantic_check_missing_method",
            "completed semantic checks require semantic_check_method",
            "result.coverage.semantic_check_method",
        )

    if (
        coverage.structural_check_status == CheckStatus.COMPLETED
        and coverage.source_segments_enumerated != expected_enumerated
    ):
        report.add(
            "structural_check_false_complete",
            "structural_check_status cannot be completed with invalid segment counts",
            "result.coverage.structural_check_status",
        )
    return report


def validate_result_against_manifest(
    manifest: SourceSnapshotManifest,
    result: LayeredMeetingResult,
) -> ValidationReport:
    report = ValidationReport(ok=True)
    if result.snapshot_id != manifest.snapshot_id:
        report.add(
            "snapshot_id_mismatch",
            "result.snapshot_id must match manifest.snapshot_id",
            "result.snapshot_id",
        )

    for bullet_index, bullet in enumerate(result.overview_bullets):
        _walk_evidence(
            manifest,
            bullet.evidence,
            f"result.overview_bullets[{bullet_index}].evidence",
            report,
        )

    for topic_index, topic in enumerate(result.topics):
        for point_index, point in enumerate(topic.points):
            _walk_evidence(
                manifest,
                point.evidence,
                f"result.topics[{topic_index}].points[{point_index}].evidence",
                report,
            )
    for action_index, action in enumerate(result.actions):
        _walk_evidence(
            manifest,
            action.evidence,
            f"result.actions[{action_index}].evidence",
            report,
        )
        _validate_deadline_fields_against_manifest(
            manifest,
            deadline_raw=action.deadline_raw,
            deadline_normalized=action.deadline_normalized,
            deadline_normalization_basis=action.deadline_normalization_basis,
            path=f"result.actions[{action_index}]",
            report=report,
        )
    for open_index, item in enumerate(result.open_items):
        _walk_evidence(
            manifest,
            item.evidence,
            f"result.open_items[{open_index}].evidence",
            report,
        )

    for record_index, record in enumerate(result.atomic_records):
        _walk_evidence(
            manifest,
            record.evidence,
            f"result.atomic_records[{record_index}].evidence",
            report,
        )
        _validate_deadline_fields_against_manifest(
            manifest,
            deadline_raw=record.deadline_raw,
            deadline_normalized=record.deadline_normalized,
            deadline_normalization_basis=record.deadline_normalization_basis,
            path=f"result.atomic_records[{record_index}]",
            report=report,
        )

    for relation_index, relation in enumerate(result.relations):
        _walk_evidence(
            manifest,
            relation.evidence,
            f"result.relations[{relation_index}].evidence",
            report,
        )

    coverage_report = validate_coverage_report(manifest, result)
    if not coverage_report.ok:
        report.ok = False
        report.issues.extend(coverage_report.issues)
    return report


def _record_id_set(records: list[AtomicRecord]) -> set[str]:
    return {record.record_id for record in records}


def _records_equivalent(left: list[AtomicRecord], right: list[AtomicRecord]) -> bool:
    if len(left) != len(right):
        return False
    left_map = {record.record_id: record for record in left}
    right_map = {record.record_id: record for record in right}
    if left_map.keys() != right_map.keys():
        return False
    return all(left_map[key] == right_map[key] for key in left_map)


def _relations_equivalent(left: list[RecordRelation], right: list[RecordRelation]) -> bool:
    if len(left) != len(right):
        return False

    def sort_key(rel: RecordRelation) -> tuple[str, str, str]:
        return (rel.from_record_id, rel.to_record_id, rel.relation_type.value)

    left_sorted = sorted(left, key=sort_key)
    right_sorted = sorted(right, key=sort_key)
    return all(
        left_item == right_item
        for left_item, right_item in zip(left_sorted, right_sorted, strict=True)
    )


def resolve_example_ledger(
    bundle: MeetingIntelligenceExampleBundle,
    report: ValidationReport,
) -> tuple[list[AtomicRecord], list[RecordRelation]]:
    bundle_records = bundle.records
    bundle_relations = bundle.relations
    result_records = bundle.result.atomic_records
    result_relations = bundle.result.relations

    if (
        bundle_records
        and result_records
        and not _records_equivalent(bundle_records, result_records)
    ):
        report.add(
            "ledger_records_inconsistent",
            "bundle.records and result.atomic_records must match exactly when both are present",
            "records",
        )

    if (
        bundle_relations
        and result_relations
        and not _relations_equivalent(bundle_relations, result_relations)
    ):
        report.add(
            "ledger_relations_inconsistent",
            "bundle.relations and result.relations must match exactly (including evidence) "
            "when both are present",
            "relations",
        )

    records = result_records if result_records else bundle_records
    relations = result_relations if result_relations else bundle_relations
    return records, relations


def validate_record_ledger(
    manifest: SourceSnapshotManifest,
    records: list[AtomicRecord],
    relations: list[RecordRelation],
) -> ValidationReport:
    report = ValidationReport(ok=True)
    record_ids = [record.record_id for record in records]
    if len(record_ids) != len(set(record_ids)):
        report.add("duplicate_record_id", "atomic record ids must be unique", "records")

    record_id_set = set(record_ids)
    for record_index, record in enumerate(records):
        _walk_evidence(manifest, record.evidence, f"records[{record_index}].evidence", report)

    for relation_index, relation in enumerate(relations):
        if relation.from_record_id not in record_id_set:
            report.add(
                "unknown_record",
                f"from_record_id {relation.from_record_id} not found",
                f"relations[{relation_index}].from_record_id",
            )
        if relation.to_record_id not in record_id_set:
            report.add(
                "unknown_record",
                f"to_record_id {relation.to_record_id} not found",
                f"relations[{relation_index}].to_record_id",
            )
        if relation.from_record_id == relation.to_record_id:
            report.add(
                "self_relation",
                "self-relations are not allowed",
                f"relations[{relation_index}]",
            )
        _walk_evidence(
            manifest,
            relation.evidence,
            f"relations[{relation_index}].evidence",
            report,
        )
    return report


def validate_structural_references(
    manifest: SourceSnapshotManifest,
    records: list[AtomicRecord],
    relations: list[RecordRelation],
) -> ValidationReport:
    return validate_record_ledger(manifest, records, relations)


def validate_example_bundle(bundle: MeetingIntelligenceExampleBundle) -> ValidationReport:
    report = ValidationReport(ok=True)
    for partial in (
        validate_manifest_integrity(bundle.manifest),
        validate_result_against_manifest(bundle.manifest, bundle.result),
    ):
        if not partial.ok:
            report.ok = False
            report.issues.extend(partial.issues)

    records, relations = resolve_example_ledger(bundle, report)

    result_ledger_report = validate_record_ledger(
        bundle.manifest, bundle.result.atomic_records, bundle.result.relations
    )
    if not result_ledger_report.ok:
        report.ok = False
        report.issues.extend(result_ledger_report.issues)

    if records is not bundle.result.atomic_records and records:
        ledger_report = validate_record_ledger(bundle.manifest, records, relations)
        if not ledger_report.ok:
            report.ok = False
            report.issues.extend(ledger_report.issues)

    if (
        bundle.records
        and bundle.result.atomic_records
        and not _records_equivalent(bundle.records, bundle.result.atomic_records)
    ):
        report.add(
            "result_ledger_record_mismatch",
            "result.atomic_records must match bundle.records when both are present",
            "result.atomic_records",
        )

    if (
        bundle.relations
        and bundle.result.relations
        and not _relations_equivalent(bundle.relations, bundle.result.relations)
    ):
        report.add(
            "result_ledger_relation_mismatch",
            "result.relations must match bundle.relations when both are present",
            "result.relations",
        )

    topics_report = validate_topics_non_empty(bundle.result)
    if not topics_report.ok:
        report.ok = False
        report.issues.extend(topics_report.issues)
    return report


def validate_topics_non_empty(result: LayeredMeetingResult) -> ValidationReport:
    report = ValidationReport(ok=True)
    for index, topic in enumerate(result.topics):
        if not topic.points:
            report.add(
                "empty_topic", "topic detail must include at least one point", f"topics[{index}]"
            )
    return report
