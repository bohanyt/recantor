"""Offline fixture-annotation evaluator. Behavioral only; not a live-quality claim.

Expected records and relations are independent annotations. The generated ledger is a
candidate output, not its own support proof.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from recantor.meeting_essence.check_binding import (
    evidence_key,
    evidence_on_fragments,
    relation_page_covers_citations,
    superseded_ids,
)
from recantor.meeting_essence.contracts import (
    ActionFollowUpItem,
    AtomicRecord,
    CheckStatus,
    LayeredMeetingResult,
    OpenItem,
    OverviewBullet,
    RecordKind,
    RecordRelation,
    SourceSnapshotManifest,
    TopicDetailPoint,
)
from recantor.meeting_essence.identity import segment_content_hash
from recantor.meeting_essence.stage_contracts import (
    AccessibleOutputClaim,
    evidence_provenance_key,
)
from recantor.meeting_essence.wire_payload import WireSegmentFragment

FIXTURE_ANNOTATION_METHOD = "fixture-annotation-v1"

_OPEN_KINDS = {RecordKind.OBJECTION, RecordKind.UNRESOLVED_QUESTION}


@dataclass
class AnnotationCheck:
    forward: CheckStatus
    backward: CheckStatus
    method: str | None
    notes: list[str] = field(default_factory=list)


def _norm(value: str | None) -> str:
    return (value or "").strip()


def _iso(value: object) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _evidence_signature(evidence: list[object]) -> tuple[tuple[object, ...], ...]:
    return tuple(sorted(evidence_key(item) for item in evidence))  # type: ignore[arg-type]


def record_signature(record: AtomicRecord) -> tuple[object, ...]:
    return (
        record.topic.strip(),
        record.kind.value,
        record.claim_text.strip(),
        record.content_status.value,
        _norm(record.condition_text),
        _norm(record.rationale),
        _norm(record.uncertainty),
        _norm(record.assignee),
        record.assignee_explicitly_undetermined,
        _norm(record.deadline_raw),
        _iso(record.deadline_normalized),
        record.deadline_unmentioned,
        record.deadline_normalization_basis.value,
        _evidence_signature(record.evidence),
    )


def relation_signature(
    relation: RecordRelation, manifest: SourceSnapshotManifest | None = None
) -> tuple[object, ...]:
    """Type, endpoints, and each citation's segment, time, span, and frozen content hash."""
    by_id = {segment.id: segment for segment in manifest.segments} if manifest is not None else {}
    evidence = []
    for item in relation.evidence:
        segment = by_id.get(item.segment_id)
        content_hash = segment.content_hash if segment is not None else ""
        evidence.append((evidence_provenance_key(item), content_hash))
    return (
        relation.relation_type.value,
        relation.from_record_id,
        relation.to_record_id,
        tuple(evidence),
    )


def _same_evidence(left: list[object], right: list[object]) -> bool:
    return _evidence_signature(left) == _evidence_signature(right)


def _deadlines_match(claim: AccessibleOutputClaim, record: AtomicRecord) -> bool:
    return (
        _norm(claim.deadline_raw) == _norm(record.deadline_raw)
        and (claim.deadline_normalized or "") == _iso(record.deadline_normalized)
        and claim.deadline_unmentioned == record.deadline_unmentioned
        and claim.deadline_normalization_basis == record.deadline_normalization_basis.value
    )


def _fields_match_record(claim: AccessibleOutputClaim, record: AtomicRecord) -> bool:
    if claim.surface in {"overview", "open_item"}:
        return True
    if claim.kind is not None and claim.kind != record.kind.value:
        return False
    if claim.content_status is not None and claim.content_status != record.content_status.value:
        return False
    if _norm(claim.condition_text) != _norm(record.condition_text):
        return False
    if _norm(claim.assignee) != _norm(record.assignee):
        return False
    if claim.assignee_explicitly_undetermined != record.assignee_explicitly_undetermined:
        return False
    if not _deadlines_match(claim, record):
        return False
    if claim.surface == "topic":
        if claim.topic is None or claim.topic.strip() != record.topic.strip():
            return False
        if _norm(claim.rationale) != _norm(record.rationale):
            return False
        if _norm(claim.uncertainty) != _norm(record.uncertainty):
            return False
    return True


def claim_supported_by_annotations(
    claim: AccessibleOutputClaim,
    expected_records: list[AtomicRecord],
) -> bool:
    """Match an output claim to an independent annotation. The candidate ledger is not support."""
    for record in expected_records:
        if claim.text.strip() != record.claim_text.strip():
            continue
        if not _same_evidence(claim.evidence, record.evidence):
            continue
        return _fields_match_record(claim, record)
    return False


def _evidence_supported(
    evidence: list[object],
    fragments: tuple[WireSegmentFragment, ...] | list[WireSegmentFragment],
) -> bool:
    if not evidence:
        return False
    saw_support = False
    for item in evidence:
        bound = evidence_on_fragments(item, fragments)  # type: ignore[arg-type]
        if bound is False:
            return False
        if bound is True:
            saw_support = True
    return saw_support


def request_page_supported(
    *,
    records: list[AtomicRecord],
    relations: list[RecordRelation],
    claims: list[AccessibleOutputClaim],
    fragments: tuple[WireSegmentFragment, ...] | list[WireSegmentFragment],
    expected_records: list[AtomicRecord],
    expected_relations: list[RecordRelation],
    manifest: SourceSnapshotManifest | None = None,
    judged_evidence_keys: tuple[str, ...] | list[str] = (),
) -> bool:
    """Closed-world support for one page. The candidate ledger is not its own proof."""
    expected_counts = Counter(record_signature(record) for record in expected_records)
    seen: Counter[tuple[object, ...]] = Counter()
    for record in records:
        signature = record_signature(record)
        seen[signature] += 1
        if seen[signature] > expected_counts[signature]:
            return False
        if relations:
            for item in record.evidence:
                if evidence_on_fragments(item, fragments) is False:
                    return False
        elif not _evidence_supported(record.evidence, fragments):
            return False
    expected_relation_counts = Counter(
        relation_signature(relation, manifest) for relation in expected_relations
    )
    seen_relations: Counter[tuple[object, ...]] = Counter()
    for relation in relations:
        signature = relation_signature(relation, manifest)
        seen_relations[signature] += 1
        if seen_relations[signature] > expected_relation_counts[signature]:
            return False
    if relations and not relation_page_covers_citations(
        relations,
        records,
        fragments,
        judged_evidence_keys,
        manifest,
    ):
        return False
    for claim in claims:
        if not claim_supported_by_annotations(claim, expected_records):
            return False
        if not _evidence_supported(claim.evidence, fragments):
            return False
    return True


def evidence_matches_manifest(
    evidence: AtomicRecord | TopicDetailPoint | ActionFollowUpItem | OpenItem | object,
    manifest: SourceSnapshotManifest,
) -> bool:
    by_id = {segment.id: segment for segment in manifest.segments}
    refs = getattr(evidence, "evidence", None)
    if not refs:
        return False
    for ref in refs:
        segment = by_id.get(ref.segment_id)
        if segment is None:
            return False
        if (ref.start_ms, ref.end_ms) != (segment.start_ms, segment.end_ms):
            return False
        if segment.content_hash != segment_content_hash(segment.text):
            return False
        if ref.text_span is not None:
            if ref.text_span.end_cp > len(segment.text):
                return False
            excerpt = segment.text[ref.text_span.start_cp : ref.text_span.end_cp]
            if not excerpt.strip():
                return False
    return True


def _point_matches(
    record: AtomicRecord,
    topic: str,
    point: TopicDetailPoint,
    *,
    superseded: bool,
) -> bool:
    status = point.content_status.value if point.content_status is not None else None
    kind = point.kind.value if point.kind is not None else None
    return (
        topic.strip() == record.topic.strip()
        and point.text.strip() == record.claim_text.strip()
        and kind == record.kind.value
        and status == record.content_status.value
        and _norm(point.condition_text) == _norm(record.condition_text)
        and _norm(point.rationale) == _norm(record.rationale)
        and _norm(point.uncertainty) == _norm(record.uncertainty)
        and _norm(point.assignee) == _norm(record.assignee)
        and point.assignee_explicitly_undetermined == record.assignee_explicitly_undetermined
        and _norm(point.deadline_raw) == _norm(record.deadline_raw)
        and _iso(point.deadline_normalized) == _iso(record.deadline_normalized)
        and point.deadline_unmentioned == record.deadline_unmentioned
        and point.deadline_normalization_basis == record.deadline_normalization_basis
        and point.superseded == superseded
        and _evidence_signature(point.evidence) == _evidence_signature(record.evidence)
    )


def _action_matches(record: AtomicRecord, action: ActionFollowUpItem) -> bool:
    return (
        action.task.strip() == record.claim_text.strip()
        and action.content_status == record.content_status
        and _norm(action.condition_text) == _norm(record.condition_text)
        and _norm(action.assignee) == _norm(record.assignee)
        and action.assignee_explicitly_undetermined == record.assignee_explicitly_undetermined
        and _norm(action.deadline_raw) == _norm(record.deadline_raw)
        and _iso(action.deadline_normalized) == _iso(record.deadline_normalized)
        and action.deadline_unmentioned == record.deadline_unmentioned
        and action.deadline_normalization_basis == record.deadline_normalization_basis
        and _evidence_signature(action.evidence) == _evidence_signature(record.evidence)
    )


def _overview_matches(record: AtomicRecord, bullet: OverviewBullet) -> bool:
    return bullet.text.strip() == record.claim_text.strip() and _same_evidence(
        bullet.evidence, record.evidence
    )


def _open_matches(record: AtomicRecord, item: OpenItem) -> bool:
    return item.text.strip() == record.claim_text.strip() and _evidence_signature(
        item.evidence
    ) == _evidence_signature(record.evidence)


def _is_open_record(record: AtomicRecord, contradicted: set[str]) -> bool:
    if record.kind in _OPEN_KINDS or record.content_status.value == "unresolved":
        return True
    return record.record_id in contradicted and record.content_status.value != "decision"


def _contradicted_ids(relations: list[RecordRelation]) -> set[str]:
    ids: set[str] = set()
    for relation in relations:
        if relation.relation_type.value == "contradicts":
            ids.add(relation.from_record_id)
            ids.add(relation.to_record_id)
    return ids


def _topic_points(result: LayeredMeetingResult) -> list[tuple[str, TopicDetailPoint]]:
    pairs: list[tuple[str, TopicDetailPoint]] = []
    for topic in result.topics:
        for point in topic.points:
            pairs.append((topic.topic, point))
    return pairs


def evaluate_fixture_annotations(
    manifest: SourceSnapshotManifest,
    ledger: list[AtomicRecord],
    relations: list[RecordRelation],
    result: LayeredMeetingResult | None,
    *,
    expected_records: list[AtomicRecord] | None,
    expected_relations: list[RecordRelation] | None,
) -> AnnotationCheck:
    if expected_records is None:
        return AnnotationCheck(
            forward=CheckStatus.NOT_STARTED,
            backward=CheckStatus.NOT_STARTED,
            method=None,
            notes=["no-independent-annotation"],
        )
    forward_notes: list[str] = []
    backward_notes: list[str] = []
    expected_rels = expected_relations or []
    for expected in expected_records:
        if not evidence_matches_manifest(expected, manifest):
            forward_notes.append("annotation-evidence-invalid")
            backward_notes.append("annotation-evidence-invalid")
    expected_counts = Counter(record_signature(record) for record in expected_records)
    ledger_counts = Counter(record_signature(record) for record in ledger)
    extra = ledger_counts - expected_counts
    missing = expected_counts - ledger_counts
    if extra:
        forward_notes.append("unsupported-extra-ledger")
    if missing:
        backward_notes.append("annotated-record-missing-from-ledger")
    for record in ledger:
        if not evidence_matches_manifest(record, manifest):
            forward_notes.append("ledger-evidence-not-in-source")
    for relation in [*expected_rels, *relations]:
        if not evidence_matches_manifest(relation, manifest):
            forward_notes.append("relation-evidence-not-in-source")
    expected_rel = Counter(relation_signature(relation, manifest) for relation in expected_rels)
    actual_rel = Counter(relation_signature(relation, manifest) for relation in relations)
    if actual_rel - expected_rel:
        forward_notes.append("unsupported-extra-relation")
    if expected_rel - actual_rel:
        backward_notes.append("annotated-relation-missing")
    blocked = superseded_ids(expected_rels)
    contradicted = _contradicted_ids(expected_rels)
    if result is None:
        if expected_records:
            backward_notes.append("missing-display-detail")
        return AnnotationCheck(
            forward=CheckStatus.FAILED if forward_notes else CheckStatus.NOT_STARTED,
            backward=CheckStatus.FAILED if backward_notes else CheckStatus.COMPLETED,
            method=FIXTURE_ANNOTATION_METHOD,
            notes=[*forward_notes, *backward_notes],
        )
    rendered_rel = Counter(relation_signature(relation, manifest) for relation in result.relations)
    if rendered_rel - expected_rel:
        forward_notes.append("unsupported-extra-relation")
    if expected_rel - rendered_rel:
        backward_notes.append("annotated-relation-missing")
    points = _topic_points(result)
    for topic, point in points:
        if not any(
            _point_matches(expected, topic, point, superseded=expected.record_id in blocked)
            for expected in expected_records
        ):
            forward_notes.append("unsupported-extra-display")
        if not evidence_matches_manifest(point, manifest):
            forward_notes.append("display-evidence-not-in-source")
    for action in result.actions:
        if not any(_action_matches(expected, action) for expected in expected_records):
            forward_notes.append("unsupported-extra-action")
        if not evidence_matches_manifest(action, manifest):
            forward_notes.append("display-evidence-not-in-source")
    for item in result.open_items:
        if not any(_open_matches(expected, item) for expected in expected_records):
            forward_notes.append("unsupported-extra-open-item")
        if not evidence_matches_manifest(item, manifest):
            forward_notes.append("display-evidence-not-in-source")
    for bullet in result.overview_bullets:
        if not any(_overview_matches(expected, bullet) for expected in expected_records):
            forward_notes.append("unsupported-extra-overview")
        if not evidence_matches_manifest(bullet, manifest):
            forward_notes.append("display-evidence-not-in-source")
    for expected in expected_records:
        superseded = expected.record_id in blocked
        if not any(
            _point_matches(expected, topic, point, superseded=superseded) for topic, point in points
        ):
            backward_notes.append("missing-display-detail")
        if expected.kind == RecordKind.ACTION and not any(
            _action_matches(expected, action) for action in result.actions
        ):
            backward_notes.append("missing-action-detail")
        if _is_open_record(expected, contradicted) and not any(
            _open_matches(expected, item) for item in result.open_items
        ):
            backward_notes.append("missing-open-detail")
    notes = list(dict.fromkeys([*forward_notes, *backward_notes]))
    return AnnotationCheck(
        forward=CheckStatus.FAILED if forward_notes else CheckStatus.COMPLETED,
        backward=CheckStatus.FAILED if backward_notes else CheckStatus.COMPLETED,
        method=FIXTURE_ANNOTATION_METHOD,
        notes=notes,
    )
