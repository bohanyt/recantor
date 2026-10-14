from __future__ import annotations

from recantor.meeting_essence.contracts import (
    ActionFollowUpItem,
    AtomicRecord,
    CheckStatus,
    CoverageLimitation,
    LayeredMeetingResult,
    OpenItem,
    OverviewBullet,
    ProcessingJobStatus,
    RecordContentStatus,
    RecordKind,
    RecordRelation,
    RecordRelationType,
    ResultCoverageReport,
    TopicDetail,
    TopicDetailPoint,
)
from recantor.meeting_essence.validation import collect_referenced_segment_ids

_STATUS_RANK = {
    RecordContentStatus.DECISION: 0,
    RecordContentStatus.UNRESOLVED: 1,
    RecordContentStatus.ACCEPTANCE: 2,
    RecordContentStatus.REQUEST: 3,
    RecordContentStatus.ASSIGNMENT: 4,
    RecordContentStatus.FINDING: 5,
    RecordContentStatus.PROPOSAL: 6,
    RecordContentStatus.CANCELLATION: 7,
    RecordContentStatus.REPORTED_COMPLETION: 8,
}


def _superseded_ids(relations: list[RecordRelation]) -> set[str]:
    blocked: set[str] = set()
    for relation in relations:
        if relation.relation_type in {RecordRelationType.SUPERSEDES, RecordRelationType.CANCELS}:
            blocked.add(relation.to_record_id)
    return blocked


def _contradicted_ids(relations: list[RecordRelation]) -> set[str]:
    ids: set[str] = set()
    for relation in relations:
        if relation.relation_type == RecordRelationType.CONTRADICTS:
            ids.add(relation.from_record_id)
            ids.add(relation.to_record_id)
    return ids


def _latest_end_ms(record: AtomicRecord) -> int:
    if not record.evidence:
        return 0
    return max(ref.end_ms for ref in record.evidence)


def _point_from_record(record: AtomicRecord, *, superseded: bool) -> TopicDetailPoint:
    return TopicDetailPoint(
        text=record.claim_text,
        evidence=list(record.evidence),
        kind=record.kind,
        content_status=record.content_status,
        condition_text=record.condition_text,
        rationale=record.rationale,
        uncertainty=record.uncertainty,
        assignee=record.assignee,
        assignee_explicitly_undetermined=record.assignee_explicitly_undetermined,
        deadline_raw=record.deadline_raw,
        deadline_normalized=record.deadline_normalized,
        deadline_unmentioned=record.deadline_unmentioned,
        deadline_normalization_basis=record.deadline_normalization_basis,
        superseded=superseded,
    )


def _is_open(record: AtomicRecord, contradicted: set[str]) -> bool:
    if record.kind in {RecordKind.OBJECTION, RecordKind.UNRESOLVED_QUESTION}:
        return True
    if record.content_status == RecordContentStatus.UNRESOLVED:
        return True
    return (
        record.record_id in contradicted and record.content_status != RecordContentStatus.DECISION
    )


def build_layered_from_ledger(
    *,
    snapshot_id: str,
    enumerated_segment_count: int,
    records: list[AtomicRecord],
    relations: list[RecordRelation],
    processing_status: ProcessingJobStatus,
    forward_check: CheckStatus,
    backward_check: CheckStatus,
    semantic_check_method: str | None,
    limitations: list[CoverageLimitation] | None = None,
) -> LayeredMeetingResult:
    """Render only the records and relations passed in. Does not read fixture gold results."""
    if not records:
        msg = "render requires at least one atomic record"
        raise ValueError(msg)
    blocked = _superseded_ids(relations)
    contradicted = _contradicted_ids(relations)
    active = [record for record in records if record.record_id not in blocked]
    ranked = sorted(
        active or records,
        key=lambda record: (_STATUS_RANK.get(record.content_status, 9), -_latest_end_ms(record)),
    )
    overview_source = ranked[:7]
    overview = [
        OverviewBullet(text=record.claim_text, evidence=list(record.evidence))
        for record in overview_source
    ]
    topics_map: dict[str, list[AtomicRecord]] = {}
    for record in records:
        topics_map.setdefault(record.topic, []).append(record)
    topics = [
        TopicDetail(
            topic=topic,
            points=[
                _point_from_record(record, superseded=record.record_id in blocked)
                for record in topic_records
            ],
        )
        for topic, topic_records in sorted(topics_map.items())
    ]
    actions: list[ActionFollowUpItem] = []
    for record in records:
        if record.kind != RecordKind.ACTION or record.record_id in blocked:
            continue
        actions.append(
            ActionFollowUpItem(
                task=record.claim_text,
                assignee=record.assignee,
                assignee_explicitly_undetermined=record.assignee_explicitly_undetermined,
                deadline_raw=record.deadline_raw,
                deadline_normalized=record.deadline_normalized,
                deadline_unmentioned=record.deadline_unmentioned,
                deadline_normalization_basis=record.deadline_normalization_basis,
                content_status=record.content_status,
                condition_text=record.condition_text,
                evidence=list(record.evidence),
            )
        )
    open_items: list[OpenItem] = []
    seen_open: set[str] = set()
    for record in records:
        if record.record_id in blocked or not _is_open(record, contradicted):
            continue
        if record.record_id in seen_open:
            continue
        seen_open.add(record.record_id)
        open_items.append(OpenItem(text=record.claim_text, evidence=list(record.evidence)))
    provisional = LayeredMeetingResult(
        snapshot_id=snapshot_id,
        overview_bullets=overview,
        topics=topics,
        actions=actions,
        open_items=open_items,
        atomic_records=list(records),
        relations=list(relations),
        coverage=ResultCoverageReport(
            snapshot_id=snapshot_id,
            source_segments_enumerated=enumerated_segment_count,
            source_segments_referenced=0,
            processing_job_status=processing_status,
            structural_check_status=CheckStatus.COMPLETED,
            forward_semantic_check_status=forward_check,
            backward_semantic_check_status=backward_check,
            semantic_check_method=semantic_check_method,
            limitations=list(limitations or []),
        ),
    )
    referenced = len(collect_referenced_segment_ids(provisional))
    return provisional.model_copy(
        update={
            "coverage": provisional.coverage.model_copy(
                update={"source_segments_referenced": referenced},
            )
        }
    )


def _select_overview(
    bullets: list[OverviewBullet],
    records: list[AtomicRecord],
    relations: list[RecordRelation],
) -> list[OverviewBullet]:
    blocked = _superseded_ids(relations)
    by_claim = {record.claim_text.strip(): record for record in records}

    def sort_key(bullet: OverviewBullet) -> tuple[int, int, int]:
        record = by_claim.get(bullet.text.strip())
        if record is None:
            return (2, 9, 0)
        superseded = 1 if record.record_id in blocked else 0
        rank = _STATUS_RANK.get(record.content_status, 9)
        return (superseded, rank, -_latest_end_ms(record))

    matched = [bullet for bullet in bullets if bullet.text.strip() in by_claim]
    unmatched = [bullet for bullet in bullets if bullet.text.strip() not in by_claim]
    ordered = sorted(matched, key=sort_key)
    active = [bullet for bullet in ordered if sort_key(bullet)[0] == 0]
    chosen = (active or ordered)[:7]
    # Keep text that does not match a ledger claim. Dropping it would hide an unsupported summary.
    visible = [*unmatched, *chosen]
    return visible or ordered[:1]


def merge_rendered_pages(
    pages: list[LayeredMeetingResult],
    *,
    snapshot_id: str,
    enumerated_segment_count: int,
    processing_status: ProcessingJobStatus,
    forward_check: CheckStatus,
    backward_check: CheckStatus,
    semantic_check_method: str | None,
    limitations: list[CoverageLimitation] | None = None,
) -> LayeredMeetingResult:
    """Host-merge provider pages. Does not rebuild claims the provider did not return."""
    records: dict[str, AtomicRecord] = {}
    relations: list[RecordRelation] = []
    seen_relations: set[tuple[str, str, str]] = set()
    bullets: list[OverviewBullet] = []
    seen_bullets: set[str] = set()
    topic_points: dict[str, list[TopicDetailPoint]] = {}
    seen_points: set[tuple[str, str, str, bool]] = set()
    actions: list[ActionFollowUpItem] = []
    seen_actions: set[str] = set()
    open_items: list[OpenItem] = []
    seen_open: set[str] = set()
    for page in pages:
        for record in page.atomic_records:
            records.setdefault(record.record_id, record)
        for relation in page.relations:
            key = (relation.relation_type.value, relation.from_record_id, relation.to_record_id)
            if key in seen_relations:
                continue
            seen_relations.add(key)
            relations.append(relation)
        for bullet in page.overview_bullets:
            if bullet.text in seen_bullets:
                continue
            seen_bullets.add(bullet.text)
            bullets.append(bullet)
        for topic in page.topics:
            bucket = topic_points.setdefault(topic.topic, [])
            for point in topic.points:
                status = point.content_status.value if point.content_status is not None else ""
                marker = (topic.topic, point.text, status, point.superseded)
                if marker in seen_points:
                    continue
                seen_points.add(marker)
                bucket.append(point)
        for action in page.actions:
            if action.task in seen_actions:
                continue
            seen_actions.add(action.task)
            actions.append(action)
        for item in page.open_items:
            if item.text in seen_open:
                continue
            seen_open.add(item.text)
            open_items.append(item)
    record_list = list(records.values())
    overview = _select_overview(bullets, record_list, relations)
    topics = [
        TopicDetail(topic=topic, points=points)
        for topic, points in sorted(topic_points.items())
        if points
    ]
    provisional = LayeredMeetingResult(
        snapshot_id=snapshot_id,
        overview_bullets=overview,
        topics=topics,
        actions=actions,
        open_items=open_items,
        atomic_records=record_list,
        relations=relations,
        coverage=ResultCoverageReport(
            snapshot_id=snapshot_id,
            source_segments_enumerated=enumerated_segment_count,
            source_segments_referenced=0,
            processing_job_status=processing_status,
            structural_check_status=CheckStatus.NOT_STARTED,
            forward_semantic_check_status=forward_check,
            backward_semantic_check_status=backward_check,
            semantic_check_method=semantic_check_method,
            limitations=list(limitations or []),
        ),
    )
    referenced = len(collect_referenced_segment_ids(provisional))
    return provisional.model_copy(
        update={
            "coverage": provisional.coverage.model_copy(
                update={"source_segments_referenced": referenced},
            )
        }
    )
