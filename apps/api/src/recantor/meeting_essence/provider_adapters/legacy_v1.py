from __future__ import annotations

from dataclasses import dataclass

from recantor.meeting_essence.contracts import LayeredMeetingResult, RecordKind
from recantor.meeting_intelligence import MeetingIntelligenceResult

LEGACY_WIRE_LIMIT = 64


@dataclass(frozen=True)
class LegacyV1Projection:
    """Four-array helper shape plus an explicit overflow report.

    Arrays keep every item. ``overflow_by_array`` counts how many items in each
    array sit past the historical 64-item helper wire budget. Nothing is dropped.
    """

    result: MeetingIntelligenceResult
    overflow_by_array: tuple[tuple[str, int], ...]

    @property
    def detail_limited(self) -> bool:
        return any(count > 0 for _, count in self.overflow_by_array)


def _clean(values: list[str]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value.strip())


def _overflow(name: str, values: tuple[str, ...]) -> tuple[str, int] | None:
    extra = len(values) - LEGACY_WIRE_LIMIT
    if extra > 0:
        return (name, extra)
    return None


def _action_line(action: object) -> str:
    task = str(getattr(action, "task", "")).strip()
    status = getattr(getattr(action, "content_status", None), "value", None)
    parts = [task]
    if status:
        parts.append(f"status:{status}")
    assignee = getattr(action, "assignee", None)
    if assignee:
        parts.append(f"PIC: {assignee}")
    elif getattr(action, "assignee_explicitly_undetermined", False):
        parts.append("PIC: belum ditentukan")
    deadline = getattr(action, "deadline_raw", None)
    if deadline:
        parts.append(f"tenggat: {deadline}")
    elif getattr(action, "deadline_unmentioned", False):
        parts.append("tenggat: belum disebutkan")
    condition = getattr(action, "condition_text", None)
    if condition:
        parts.append(f"kondisi: {condition}")
    return " — ".join(part for part in parts if part)


def layered_to_legacy_v1(
    layered: LayeredMeetingResult,
    *,
    requested_model: str | None,
    requested_reasoning_effort: str | None,
) -> LegacyV1Projection:
    """Map layered essence onto the #68 four array names without hiding extra detail."""
    key_points = _clean([bullet.text for bullet in layered.overview_bullets])
    decisions = _clean(
        [
            record.claim_text
            for record in layered.atomic_records
            if record.kind == RecordKind.DECISION
        ]
    )
    action_items = _clean([_action_line(action) for action in layered.actions])
    open_questions = _clean([item.text for item in layered.open_items])
    overflow = tuple(
        item
        for item in (
            _overflow("key_points", key_points),
            _overflow("decisions", decisions),
            _overflow("action_items", action_items),
            _overflow("open_questions", open_questions),
        )
        if item is not None
    )
    result = MeetingIntelligenceResult(
        key_points=key_points,
        decisions=decisions,
        action_items=action_items,
        open_questions=open_questions,
        requested_model=requested_model,
        requested_reasoning_effort=requested_reasoning_effort,
        legacy_wire_limit=LEGACY_WIRE_LIMIT,
        overflow_by_array=overflow,
    )
    return LegacyV1Projection(result=result, overflow_by_array=overflow)
