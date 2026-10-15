"""Durable summary submit, claim, reconcile, and one-quantum execution."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.meeting_essence.budget import EngineLimits
from recantor.meeting_essence.checkpoint_binding import limits_fingerprint
from recantor.meeting_essence.contracts import ProcessingJobStatus
from recantor.meeting_essence.engine import EngineRunConfig
from recantor.meeting_essence.persistence.caching_provider import EnteredProviderCalls
from recantor.meeting_essence.persistence.repository import (
    AttemptBusyError,
    AttemptFenceError,
    MeetingIntelligenceRepository,
)
from recantor.meeting_essence.persistence.service import MeetingIntelligenceLedgerService
from recantor.meeting_essence.prompt_loader import load_prompt_bundle
from recantor.meeting_essence.provider import MeetingEssenceProvider
from recantor.meeting_essence.provider_errors import ProviderErrorCategory
from recantor.meeting_essence.scheduling.host_permit import HostInvocationBusy, HostInvocationPermit
from recantor.meeting_essence.scheduling.models import (
    MISummaryDispatch,
    MISummaryDispatchStatus,
    MISummaryRequestReservation,
    MISummaryScanCursor,
    MISummarySessionState,
)
from recantor.meeting_essence.scheduling.permit_provider import PermitBoundMeetingEssenceProvider
from recantor.meeting_essence.scheduling.provider_factory import get_summary_provider_factory
from recantor.meeting_essence.scheduling.source_material import (
    MaterialSnapshot,
    build_coverage_flags,
    load_material_snapshot,
    load_session_reader,
)
from recantor.meeting_essence.scheduling.telemetry import (
    SchedulerRunMetrics,
    metrics_from_outcome,
    metrics_input_fingerprint,
    persist_integrated_metrics,
    progress_token,
    utcnow,
)
from recantor.meeting_essence.scheduling.triggers import (
    note_session_finalized,
    note_transcript_publication,
)
from recantor.models import RecordingSession, SessionState, TranscriptSegment
from recantor.settings import get_settings

logger = logging.getLogger(__name__)
_MI_SUMMARY_SCHEDULER_ADVISORY_LOCK = 0x5245434D493733
_CANONICAL_PAGE = 32
_LIVE_PROVIDERS: dict[UUID, PermitBoundMeetingEssenceProvider] = {}
_CANCEL_TASKS: set[asyncio.Task[None]] = set()

_NON_RETRYABLE = frozenset(
    {
        ProviderErrorCategory.AUTH.value,
        ProviderErrorCategory.CONFIG.value,
        ProviderErrorCategory.CONTAINMENT.value,
        ProviderErrorCategory.UNAVAILABLE.value,
    }
)
_OPEN = (
    MISummaryDispatchStatus.PENDING.value,
    MISummaryDispatchStatus.CLAIMED.value,
)
_RUNNABLE = "runnable"
_AWAITING = "awaiting_material"
_TERMINAL_PARTIAL = "terminal_partial"
_RESUMABLE_CODES = frozenset(
    {
        "wall_clock_exhausted",
        "extract_incomplete",
        "reconcile_incomplete",
        "render_incomplete",
        "check_incomplete",
    }
)
_AWAITING_CODES = frozenset({"source_coverage_gap"})
_TERMINAL_PARTIAL_CODES = frozenset({"metadata_value_exceeds_budget"})


class SummarySubmitStatus(StrEnum):
    ACCEPTED = "accepted"
    COALESCED = "coalesced"
    QUEUE_FULL = "queue_full"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


class SummaryExecutionStatus(StrEnum):
    NOT_CLAIMED = "not_claimed"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    CANCELLED = "cancelled"
    QUARANTINED = "quarantined"
    PARKED = "parked"


class SummaryRetryDenied(RuntimeError):
    """Retry is refused while an attempt is claimed or already completed."""


@dataclass(frozen=True)
class SummarySubmitResult:
    status: SummarySubmitStatus
    dispatch_id: UUID | None = None
    client_request_id: UUID | None = None


@dataclass(frozen=True)
class SummaryExecutionResult:
    status: SummaryExecutionStatus
    dispatch_id: UUID | None = None
    session_id: UUID | None = None
    job_id: UUID | None = None
    metrics: SchedulerRunMetrics | None = None


@dataclass(frozen=True)
class SummaryReconcileResult:
    coalesced: int = 0
    created: int = 0
    scanned: int = 0
    wake_target: int = 0


@dataclass(frozen=True)
class _DispatchClaim:
    dispatch_id: UUID
    session_id: UUID
    token: str
    failure_count: int


def config_fingerprint() -> str:
    settings = get_settings()
    factory = get_summary_provider_factory()
    bundle = load_prompt_bundle()
    model = settings.meeting_essence_requested_model
    effort = settings.meeting_essence_requested_reasoning_effort
    limits = limits_fingerprint(
        EngineLimits(),
        requested_model=model or None,
        requested_reasoning_effort=effort or None,
    )
    raw = "|".join(
        (
            model,
            effort,
            factory.adapter_id,
            bundle.cache_key,
            bundle.schema_identity,
            bundle.algorithm_identity,
            limits,
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def request_payload_fingerprint(session_id: UUID, fingerprint: str | None = None) -> str:
    bound = fingerprint or config_fingerprint()
    return hashlib.sha256(f"{session_id}|{bound}".encode()).hexdigest()


def _retry_delay_seconds(category: str | None, failure_count: int) -> float:
    settings = get_settings()
    if category in _NON_RETRYABLE:
        return float(settings.meeting_essence_configuration_retry_seconds)
    exponent = max(0, failure_count - 1)
    return min(
        float(settings.meeting_essence_retry_max_seconds),
        float(settings.meeting_essence_retry_base_seconds) * (2**exponent),
    )


async def _database_now(db: AsyncSession) -> datetime:
    current = await db.scalar(select(func.clock_timestamp()))
    if current is None:
        msg = "database did not return scheduling clock"
        raise RuntimeError(msg)
    return current


async def _open_count(db: AsyncSession) -> int:
    counted = await db.scalar(
        select(func.count())
        .select_from(MISummaryDispatch)
        .where(
            MISummaryDispatch.status.in_(_OPEN),
            or_(
                MISummaryDispatch.disposition == _RUNNABLE,
                MISummaryDispatch.status == MISummaryDispatchStatus.CLAIMED.value,
            ),
        )
    )
    return int(counted or 0)


async def _activate_or_defer(
    db: AsyncSession,
    dispatch: MISummaryDispatch,
    *,
    now: datetime | None = None,
) -> None:
    """Promote a parked intent only when the runnable depth still has room."""
    if dispatch.status != MISummaryDispatchStatus.PENDING.value:
        return
    if dispatch.disposition == _RUNNABLE:
        return
    state = await db.get(MISummarySessionState, dispatch.session_id)
    if state is not None and state.work_intent == "cancelled":
        return
    if await _open_count(db) >= get_settings().meeting_essence_queue_max_depth:
        dispatch.capacity_deferred = True
        return
    dispatch.disposition = _RUNNABLE
    dispatch.capacity_deferred = False
    dispatch.parked_material_epoch = None
    dispatch.next_attempt_at = now or utcnow()


async def _release_deferred(db: AsyncSession) -> None:
    settings = get_settings()
    while await _open_count(db) < settings.meeting_essence_queue_max_depth:
        waiting = await db.scalar(
            select(MISummaryDispatch)
            .where(
                MISummaryDispatch.status == MISummaryDispatchStatus.PENDING.value,
                MISummaryDispatch.capacity_deferred.is_(True),
            )
            .order_by(MISummaryDispatch.created_at, MISummaryDispatch.id)
            .limit(1)
        )
        if waiting is None:
            return
        state = await db.get(MISummarySessionState, waiting.session_id)
        if state is not None and state.work_intent == "cancelled":
            waiting.capacity_deferred = False
            await db.flush()
            continue
        if state is not None and state.work_intent == "blocked":
            material = await load_material_snapshot(
                db,
                session_id=waiting.session_id,
                publication_boundary_sequence=int(state.observed_publication_high_water),
            )
            if not _intent_accepts_new_work(
                state,
                config_fingerprint(),
                source_fingerprint=material.source_fingerprint,
            ):
                waiting.capacity_deferred = False
                await db.flush()
                continue
        waiting.disposition = _RUNNABLE
        waiting.capacity_deferred = False
        waiting.parked_material_epoch = None
        waiting.next_attempt_at = utcnow()
        await db.flush()


async def _block_retry_budget(db: AsyncSession, dispatch: MISummaryDispatch) -> None:
    state = await db.get(MISummarySessionState, dispatch.session_id)
    if state is None:
        state = MISummarySessionState(session_id=dispatch.session_id, work_intent="blocked")
        db.add(state)
        await db.flush()
    boundary = int(state.observed_publication_high_water or 0)
    material = await load_material_snapshot(
        db, session_id=dispatch.session_id, publication_boundary_sequence=boundary
    )
    state.work_intent = "blocked"
    state.blocked_category = dispatch.last_error_category or "retry_budget"
    state.blocked_disposition = "retry_budget_exhausted"
    state.blocked_config_fingerprint = dispatch.config_fingerprint or config_fingerprint()
    state.blocked_source_fingerprint = material.source_fingerprint
    state.desired_config_fingerprint = state.blocked_config_fingerprint


async def _admit_lock(db: AsyncSession) -> None:
    await db.execute(select(func.pg_advisory_xact_lock(_MI_SUMMARY_SCHEDULER_ADVISORY_LOCK)))


async def submit_summary_work(
    db: AsyncSession,
    *,
    session_id: UUID,
    client_request_id: UUID,
    payload_fingerprint: str | None = None,
) -> SummarySubmitResult:
    await _admit_lock(db)
    fingerprint = payload_fingerprint or config_fingerprint()
    reserved = await db.get(MISummaryRequestReservation, client_request_id)
    if reserved is not None:
        return _reservation_result(reserved, session_id=session_id, fingerprint=fingerprint)
    existing = await db.scalar(
        select(MISummaryDispatch).where(MISummaryDispatch.client_request_id == client_request_id)
    )
    if existing is not None:
        if existing.session_id != session_id:
            return SummarySubmitResult(
                status=SummarySubmitStatus.CONFLICT, client_request_id=client_request_id
            )
        if existing.payload_fingerprint not in {None, fingerprint}:
            return SummarySubmitResult(
                status=SummarySubmitStatus.CONFLICT, client_request_id=client_request_id
            )
        await _reserve(
            db,
            session_id=session_id,
            request_id=client_request_id,
            dispatch=existing,
            fingerprint=fingerprint,
        )
        return SummarySubmitResult(
            status=SummarySubmitStatus.DUPLICATE,
            dispatch_id=existing.id,
            client_request_id=client_request_id,
        )
    settings = get_settings()
    open_row = await db.scalar(
        select(MISummaryDispatch)
        .where(
            MISummaryDispatch.session_id == session_id,
            MISummaryDispatch.status.in_(_OPEN),
        )
        .order_by(MISummaryDispatch.created_at)
        .limit(1)
    )
    if open_row is not None:
        await _promote_open_intent(db, open_row, session_id=session_id, fingerprint=fingerprint)
        await _reserve(
            db,
            session_id=session_id,
            request_id=client_request_id,
            dispatch=open_row,
            fingerprint=fingerprint,
        )
        return SummarySubmitResult(
            status=SummarySubmitStatus.COALESCED,
            dispatch_id=open_row.id,
            client_request_id=client_request_id,
        )
    if await _open_count(db) >= settings.meeting_essence_queue_max_depth:
        return SummarySubmitResult(status=SummarySubmitStatus.QUEUE_FULL)
    dispatch = MISummaryDispatch(
        id=uuid4(),
        session_id=session_id,
        client_request_id=client_request_id,
        status=MISummaryDispatchStatus.PENDING.value,
        next_attempt_at=utcnow(),
        payload_fingerprint=request_payload_fingerprint(session_id, fingerprint),
        config_fingerprint=fingerprint,
    )
    db.add(dispatch)
    try:
        await db.flush()
        await _reserve(
            db,
            session_id=session_id,
            request_id=client_request_id,
            dispatch=dispatch,
            fingerprint=fingerprint,
        )
        await _promote_open_intent(db, dispatch, session_id=session_id, fingerprint=fingerprint)
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raced = await db.get(MISummaryRequestReservation, client_request_id)
        if raced is not None:
            return _reservation_result(raced, session_id=session_id, fingerprint=fingerprint)
        open_again = await db.scalar(
            select(MISummaryDispatch)
            .where(
                MISummaryDispatch.session_id == session_id,
                MISummaryDispatch.status.in_(_OPEN),
            )
            .limit(1)
        )
        if open_again is None:
            return SummarySubmitResult(
                status=SummarySubmitStatus.CONFLICT, client_request_id=client_request_id
            )
        await _promote_open_intent(db, open_again, session_id=session_id, fingerprint=fingerprint)
        await _reserve(
            db,
            session_id=session_id,
            request_id=client_request_id,
            dispatch=open_again,
            fingerprint=fingerprint,
        )
        await db.flush()
        return SummarySubmitResult(
            status=SummarySubmitStatus.COALESCED,
            dispatch_id=open_again.id,
            client_request_id=client_request_id,
        )
    return SummarySubmitResult(
        status=SummarySubmitStatus.ACCEPTED,
        dispatch_id=dispatch.id,
        client_request_id=client_request_id,
    )


def _reservation_result(
    reserved: MISummaryRequestReservation, *, session_id: UUID, fingerprint: str
) -> SummarySubmitResult:
    if reserved.session_id != session_id or reserved.payload_fingerprint != fingerprint:
        return SummarySubmitResult(
            status=SummarySubmitStatus.CONFLICT,
            client_request_id=reserved.client_request_id,
        )
    status = (
        SummarySubmitStatus.DUPLICATE
        if reserved.client_request_id
        else SummarySubmitStatus.COALESCED
    )
    return SummarySubmitResult(
        status=status,
        dispatch_id=reserved.dispatch_id,
        client_request_id=reserved.client_request_id,
    )


async def _reserve(
    db: AsyncSession,
    *,
    session_id: UUID,
    request_id: UUID,
    dispatch: MISummaryDispatch,
    fingerprint: str,
) -> None:
    existing = await db.get(MISummaryRequestReservation, request_id)
    if existing is not None:
        return
    db.add(
        MISummaryRequestReservation(
            client_request_id=request_id,
            session_id=session_id,
            dispatch_id=dispatch.id,
            payload_fingerprint=fingerprint,
        )
    )


async def _promote_open_intent(
    db: AsyncSession,
    dispatch: MISummaryDispatch,
    *,
    session_id: UUID,
    fingerprint: str,
) -> None:
    state = await db.get(MISummarySessionState, session_id)
    if state is None:
        changed = dispatch.config_fingerprint not in {None, fingerprint}
        state = MISummarySessionState(
            session_id=session_id,
            desired_config_fingerprint=fingerprint,
            desired_config_epoch=1 if changed else 0,
            material_epoch=1 if changed else 0,
        )
        db.add(state)
    elif state.desired_config_fingerprint != fingerprint:
        state.desired_config_fingerprint = fingerprint
        state.desired_config_epoch = int(state.desired_config_epoch) + 1
        state.material_epoch = int(state.material_epoch) + 1
    if (
        dispatch.status == MISummaryDispatchStatus.PENDING.value
        and dispatch.config_fingerprint != fingerprint
    ):
        dispatch.config_fingerprint = fingerprint
        dispatch.payload_fingerprint = request_payload_fingerprint(session_id, fingerprint)
        if dispatch.disposition != _RUNNABLE and state.work_intent != "cancelled":
            await _activate_or_defer(db, dispatch)


async def reconcile_summary_work(db: AsyncSession) -> SummaryReconcileResult:
    settings = get_settings()
    await _admit_lock(db)
    await _release_deferred(db)
    now = await _database_now(db)
    scanned = await _scan_canonical_page(db)
    await _recover_lost_finalized(db)
    created, coalesced = await _enqueue_due_page(db, now=now)
    pending_count = await _open_count(db)
    wake_target = min(pending_count, settings.meeting_essence_wake_batch_size)
    return SummaryReconcileResult(
        coalesced=coalesced, created=created, scanned=scanned, wake_target=wake_target
    )


def _intent_accepts_new_work(
    state: MISummarySessionState,
    fingerprint: str,
    *,
    source_fingerprint: str | None = None,
) -> bool:
    if state.work_intent == "cancelled":
        return False
    if state.work_intent != "blocked":
        return True
    if state.blocked_disposition == "retry_budget_exhausted":
        config_changed = (
            state.blocked_config_fingerprint is not None
            and fingerprint != state.blocked_config_fingerprint
        )
        source_changed = (
            source_fingerprint is not None
            and state.blocked_source_fingerprint is not None
            and source_fingerprint != state.blocked_source_fingerprint
        )
        return config_changed or source_changed
    return state.desired_config_fingerprint not in {None, fingerprint}


def _material_due(
    state: MISummarySessionState, fingerprint: str, material: object | None = None
) -> bool:
    source_fingerprint = getattr(material, "source_fingerprint", None)
    if not _intent_accepts_new_work(state, fingerprint, source_fingerprint=source_fingerprint):
        return False
    if state.work_intent == "blocked":
        return True
    if int(state.observed_publication_high_water) > int(state.highest_processed_publication):
        return True
    if (state.desired_config_fingerprint or "") != (state.processed_config_fingerprint or ""):
        return True
    if int(state.desired_config_epoch) != int(state.processed_config_epoch):
        return True
    if int(state.finalize_epoch) != int(state.processed_finalize_epoch):
        return True
    if (state.desired_coverage_fingerprint or "") != (state.processed_coverage_fingerprint or ""):
        return True
    if (state.desired_stt_fingerprint or "") != (state.processed_stt_fingerprint or ""):
        return True
    return (state.desired_source_fingerprint or "") != (state.processed_source_fingerprint or "")


async def _cursor_row(db: AsyncSession) -> MISummaryScanCursor:
    row = await db.get(MISummaryScanCursor, 1)
    if row is None:
        row = MISummaryScanCursor(id=1)
        db.add(row)
        await db.flush()
    return row


async def _scan_canonical_page(db: AsyncSession) -> int:
    cursor = await _cursor_row(db)
    rows = await _canonical_keyset(db, cursor.canonical_after)
    wrapped = False
    if not rows and cursor.canonical_after is not None:
        rows = await _canonical_keyset(db, None)
        wrapped = True
    for session_id, observed in rows:
        await note_transcript_publication(
            db, session_id=session_id, publication_sequence=int(observed or 0)
        )
    if rows:
        cursor.canonical_after = None if wrapped and len(rows) < _CANONICAL_PAGE else rows[-1][0]
    elif wrapped:
        cursor.canonical_after = None
    return len(rows)


async def _canonical_keyset(db: AsyncSession, after: UUID | None) -> list[tuple[UUID, int]]:
    max_seq = (
        select(
            TranscriptSegment.session_id.label("session_id"),
            func.max(TranscriptSegment.sequence).label("mx"),
        )
        .group_by(TranscriptSegment.session_id)
        .subquery()
    )
    query = select(max_seq.c.session_id, max_seq.c.mx).order_by(max_seq.c.session_id)
    if after is not None:
        query = query.where(max_seq.c.session_id > after)
    rows = (await db.execute(query.limit(_CANONICAL_PAGE))).all()
    return [(row[0], int(row[1] or 0)) for row in rows]


async def _recover_lost_finalized(db: AsyncSession) -> None:
    sessions = list(
        await db.scalars(
            select(RecordingSession)
            .outerjoin(
                MISummarySessionState,
                MISummarySessionState.session_id == RecordingSession.id,
            )
            .where(
                RecordingSession.state == SessionState.COMPLETE.value,
                RecordingSession.final_sequence.is_not(None),
                or_(
                    MISummarySessionState.session_id.is_(None),
                    MISummarySessionState.frozen_artifact_publication.is_(None),
                ),
            )
            .order_by(RecordingSession.id)
            .limit(_CANONICAL_PAGE)
        )
    )
    for session in sessions:
        from recantor.meeting_essence.scheduling.source_material import load_transcript_artifact

        transcript_sequence, transcript_hash = await load_transcript_artifact(
            db, session_id=session.id
        )
        await note_session_finalized(
            db,
            session_id=session.id,
            audio_final_sequence=int(session.final_sequence or 0),
            transcript_sequence=transcript_sequence,
            transcript_hash=transcript_hash,
        )


async def _enqueue_due_page(db: AsyncSession, *, now: datetime) -> tuple[int, int]:
    settings = get_settings()
    fingerprint = config_fingerprint()
    cursor = await _cursor_row(db)
    states = await _state_keyset(db, cursor.state_after)
    wrapped = False
    if not states and cursor.state_after is not None:
        states = await _state_keyset(db, None)
        wrapped = True
    created = 0
    coalesced = 0
    pending_count = await _open_count(db)
    for state in states:
        material = await load_material_snapshot(
            db,
            session_id=state.session_id,
            publication_boundary_sequence=int(state.observed_publication_high_water),
        )
        _apply_material(state, material, fingerprint)
        if material.max_sequence <= 0:
            continue
        if pending_count >= settings.meeting_essence_queue_max_depth:
            continue
        if not _material_due(state, fingerprint, material):
            continue
        existing = await db.scalar(
            select(MISummaryDispatch).where(
                MISummaryDispatch.session_id == state.session_id,
                MISummaryDispatch.status.in_(_OPEN),
            )
        )
        if existing is not None:
            if existing.disposition != _RUNNABLE and int(state.material_epoch) > int(
                existing.parked_material_epoch or 0
            ):
                await _activate_or_defer(db, existing, now=now)
            elif existing.disposition == _RUNNABLE:
                await _promote_open_intent(
                    db, existing, session_id=state.session_id, fingerprint=fingerprint
                )
            coalesced += 1
            continue
        due = now
        if (
            settings.meeting_essence_debounce_seconds > 0
            and state.debounce_until is not None
            and state.debounce_until > now
        ):
            due = state.debounce_until
        request_id = uuid4()
        dispatch = MISummaryDispatch(
            id=uuid4(),
            session_id=state.session_id,
            client_request_id=request_id,
            status=MISummaryDispatchStatus.PENDING.value,
            next_attempt_at=due,
            payload_fingerprint=request_payload_fingerprint(state.session_id, fingerprint),
            config_fingerprint=fingerprint,
            disposition=_RUNNABLE,
        )
        db.add(dispatch)
        await db.flush()
        await _reserve(
            db,
            session_id=state.session_id,
            request_id=request_id,
            dispatch=dispatch,
            fingerprint=fingerprint,
        )
        created += 1
        pending_count += 1
    if states:
        cursor.state_after = (
            None if wrapped and len(states) < _CANONICAL_PAGE else states[-1].session_id
        )
    elif wrapped:
        cursor.state_after = None
    return created, coalesced


async def _state_keyset(db: AsyncSession, after: UUID | None) -> list[MISummarySessionState]:
    query = select(MISummarySessionState).order_by(MISummarySessionState.session_id)
    if after is not None:
        query = query.where(MISummarySessionState.session_id > after)
    return list(await db.scalars(query.limit(_CANONICAL_PAGE)))


def _apply_material(
    state: MISummarySessionState, material: MaterialSnapshot, fingerprint: str
) -> None:
    changed = False
    if state.desired_source_fingerprint != material.source_fingerprint:
        state.desired_source_fingerprint = material.source_fingerprint
        changed = True
    if state.desired_coverage_fingerprint != material.coverage_fingerprint:
        state.desired_coverage_fingerprint = material.coverage_fingerprint
        changed = True
    if state.desired_stt_fingerprint != material.stt_fingerprint:
        state.desired_stt_fingerprint = material.stt_fingerprint
        changed = True
    if state.desired_config_fingerprint != fingerprint and state.work_intent != "blocked":
        state.desired_config_fingerprint = fingerprint
        state.desired_config_epoch = int(state.desired_config_epoch) + 1
        changed = True
    if changed:
        state.material_epoch = int(state.material_epoch) + 1


async def _claim_next_dispatch(db: AsyncSession) -> _DispatchClaim | None:
    settings = get_settings()
    lease = float(settings.meeting_essence_dispatch_lease_seconds)
    now = await _database_now(db)
    dispatch = await db.scalar(
        select(MISummaryDispatch)
        .where(
            or_(
                and_(
                    MISummaryDispatch.status == MISummaryDispatchStatus.PENDING.value,
                    MISummaryDispatch.disposition == _RUNNABLE,
                    or_(
                        MISummaryDispatch.next_attempt_at.is_(None),
                        MISummaryDispatch.next_attempt_at <= now,
                    ),
                ),
                and_(
                    MISummaryDispatch.status == MISummaryDispatchStatus.CLAIMED.value,
                    MISummaryDispatch.claim_expires_at.is_not(None),
                    MISummaryDispatch.claim_expires_at <= now,
                ),
            )
        )
        .order_by(
            MISummaryDispatch.next_attempt_at,
            MISummaryDispatch.created_at,
            MISummaryDispatch.session_id,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if dispatch is None:
        return None
    if int(dispatch.failure_count) >= settings.meeting_essence_max_attempts:
        dispatch.status = MISummaryDispatchStatus.CANCELLED.value
        dispatch.claim_token = None
        dispatch.claim_expires_at = None
        dispatch.last_error_category = dispatch.last_error_category or "retry_budget"
        dispatch.last_error_code = "retry_budget_exhausted"
        await _block_retry_budget(db, dispatch)
        await _release_deferred(db)
        await db.flush()
        return None
    token = uuid4().hex
    dispatch.status = MISummaryDispatchStatus.CLAIMED.value
    dispatch.claim_token = token
    dispatch.claim_expires_at = now + timedelta(seconds=lease)
    dispatch.attempt_count = int(dispatch.attempt_count) + 1
    await db.flush()
    return _DispatchClaim(
        dispatch_id=dispatch.id,
        session_id=dispatch.session_id,
        token=token,
        failure_count=int(dispatch.failure_count),
    )


async def _fenced(
    db: AsyncSession,
    claim: _DispatchClaim,
    values: dict[str, object],
) -> bool:
    result = await db.execute(
        update(MISummaryDispatch)
        .where(
            MISummaryDispatch.id == claim.dispatch_id,
            MISummaryDispatch.claim_token == claim.token,
            MISummaryDispatch.status == MISummaryDispatchStatus.CLAIMED.value,
        )
        .values(**values)
    )
    return result.rowcount == 1


async def execute_next_summary_job(
    db: AsyncSession,
    *,
    entrypoint: str = "celery-summary-worker",
    provider: MeetingEssenceProvider | None = None,
) -> SummaryExecutionResult:
    await reconcile_summary_work(db)
    await db.commit()
    permit = HostInvocationPermit()
    await permit.recover_proven_dead()
    if await permit.blocks_new_invocation():
        return SummaryExecutionResult(status=SummaryExecutionStatus.BUSY)
    claim = await _claim_next_dispatch(db)
    if claim is None:
        await db.commit()
        return SummaryExecutionResult(status=SummaryExecutionStatus.NOT_CLAIMED)
    await db.commit()
    handle = await permit.try_acquire(entrypoint=entrypoint)
    if handle is None:
        async with _session() as retry_db:
            await _fenced(
                retry_db,
                claim,
                {
                    "status": MISummaryDispatchStatus.PENDING.value,
                    "claim_token": None,
                    "claim_expires_at": None,
                    "next_attempt_at": utcnow(),
                },
            )
            await retry_db.commit()
        return SummaryExecutionResult(
            status=SummaryExecutionStatus.BUSY,
            dispatch_id=claim.dispatch_id,
            session_id=claim.session_id,
            metrics=SchedulerRunMetrics(
                dispatch_id=str(claim.dispatch_id),
                session_id=str(claim.session_id),
                host_permit_busy=True,
            ),
        )
    bound: PermitBoundMeetingEssenceProvider | None = None
    started = utcnow()
    try:
        inner = provider if provider is not None else get_summary_provider_factory().build()
        # Count the call only once admission has started the real provider.
        bound = PermitBoundMeetingEssenceProvider(
            inner=EnteredProviderCalls(inner),
            permit=permit,
            entrypoint=entrypoint,
            shared_handle=handle,
        )
        return await _run_claimed(
            db, claim, bound, provider_supplied=provider is not None, started=started
        )
    finally:
        if bound is not None:
            cleared = await bound.aclose()
            if not cleared:
                logger.info(
                    "host permit quarantined after summary call dispatch=%s", claim.dispatch_id
                )
        else:
            await permit.release(handle)


async def _run_claimed(
    db: AsyncSession,
    claim: _DispatchClaim,
    provider: PermitBoundMeetingEssenceProvider,
    *,
    provider_supplied: bool,
    started: datetime,
) -> SummaryExecutionResult:
    settings = get_settings()
    dispatch = await db.get(MISummaryDispatch, claim.dispatch_id)
    if dispatch is None or dispatch.claim_token != claim.token:
        return SummaryExecutionResult(status=SummaryExecutionStatus.CANCELLED)
    if not provider_supplied and get_summary_provider_factory().adapter_id == "unconfigured":
        await _block_unconfigured(db, claim, dispatch.session_id)
        return SummaryExecutionResult(
            status=SummaryExecutionStatus.UNAVAILABLE,
            dispatch_id=claim.dispatch_id,
            session_id=claim.session_id,
        )
    state = await db.get(MISummarySessionState, claim.session_id)
    boundary = int(state.observed_publication_high_water if state is not None else 0)
    if boundary <= 0:
        await _fenced(
            db,
            claim,
            {
                "status": MISummaryDispatchStatus.PENDING.value,
                "disposition": _AWAITING,
                "claim_token": None,
                "claim_expires_at": None,
                "next_attempt_at": None,
                "parked_material_epoch": 0 if state is None else int(state.material_epoch),
            },
        )
        await db.commit()
        return SummaryExecutionResult(
            status=SummaryExecutionStatus.PARKED,
            dispatch_id=claim.dispatch_id,
            session_id=claim.session_id,
        )
    reader = await load_session_reader(
        db, session_id=claim.session_id, publication_boundary_sequence=boundary
    )
    coverage = await build_coverage_flags(
        db,
        session_id=claim.session_id,
        publication_boundary_sequence=boundary,
        summary_state=state,
    )
    limits = EngineLimits()
    model = settings.meeting_essence_requested_model
    effort = settings.meeting_essence_requested_reasoning_effort
    fingerprint = limits_fingerprint(
        limits, requested_model=model or None, requested_reasoning_effort=effort or None
    )
    repo = MeetingIntelligenceRepository(db)
    resume = await repo.load_latest_checkpoint_for_job_session(
        session_id=claim.session_id,
        requested_model=model,
        requested_reasoning_effort=effort,
        limits_fingerprint=fingerprint,
    )
    queued_at = dispatch.created_at
    explicit_retry = bool(dispatch.explicit_retry)
    client_request_id = dispatch.client_request_id
    before_token = progress_token(resume)
    material = await load_material_snapshot(
        db, session_id=claim.session_id, publication_boundary_sequence=boundary
    )
    captured_config = config_fingerprint()
    captured_config_epoch = 0 if state is None else int(state.desired_config_epoch)
    captured_finalize_epoch = 0 if state is None else int(state.finalize_epoch)
    captured_material_epoch = 0 if state is None else int(state.material_epoch)
    _LIVE_PROVIDERS[claim.dispatch_id] = provider

    async def on_attempt_started(job: object, attempt: object) -> None:
        linked = await _fenced(
            db,
            claim,
            {
                "job_id": job.id,  # type: ignore[attr-defined]
                "attempt_id": attempt.id,  # type: ignore[attr-defined]
                "attempt_generation": attempt.generation,  # type: ignore[attr-defined]
                "publication_boundary": boundary,
                "config_fingerprint": config_fingerprint(),
            },
        )
        if not linked:
            raise AttemptFenceError("dispatch claim lost before the provider call")
        await db.commit()

    ledger = MeetingIntelligenceLedgerService(db)
    adapter_id = get_summary_provider_factory().adapter_id if not provider_supplied else "injected"
    config = EngineRunConfig(
        max_wall_clock_seconds=float(settings.meeting_essence_engine_quantum_seconds),
        requested_model=model or None,
        requested_reasoning_effort=effort or None,
    )
    watcher = asyncio.create_task(_watch_cancel(claim.dispatch_id, provider))
    try:
        try:
            outcome = await ledger.run_engine(
                session_id=claim.session_id,
                client_request_id=client_request_id,
                reader=reader,
                provider=provider,
                publication_boundary_sequence=boundary,
                coverage=coverage,
                provider_adapter_id=adapter_id,
                config=config,
                resume_checkpoint=resume,
                retry=explicit_retry,
                on_attempt_started=on_attempt_started,
            )
        except AttemptBusyError as busy:
            if await _dispatch_cancelled(claim.dispatch_id):
                raise AttemptFenceError(
                    "dispatch cancelled while the previous attempt was busy"
                ) from busy
            outcome = await ledger.run_engine(
                session_id=claim.session_id,
                client_request_id=client_request_id,
                reader=reader,
                provider=provider,
                publication_boundary_sequence=boundary,
                coverage=coverage,
                provider_adapter_id=adapter_id,
                config=config,
                resume_checkpoint=resume,
                recover=True,
                retry=explicit_retry,
                on_attempt_started=on_attempt_started,
            )
    except AttemptFenceError:
        await _mark_dispatch_cancelled(db, claim)
        return SummaryExecutionResult(
            status=SummaryExecutionStatus.CANCELLED,
            dispatch_id=claim.dispatch_id,
            session_id=claim.session_id,
        )
    except HostInvocationBusy:
        if provider.cancel_requested() or await _dispatch_cancelled(claim.dispatch_id):
            await _mark_dispatch_cancelled(db, claim)
            return SummaryExecutionResult(
                status=SummaryExecutionStatus.CANCELLED,
                dispatch_id=claim.dispatch_id,
                session_id=claim.session_id,
            )
        await _requeue(db, claim, failure=False)
        return SummaryExecutionResult(
            status=SummaryExecutionStatus.BUSY,
            dispatch_id=claim.dispatch_id,
            session_id=claim.session_id,
        )
    finally:
        watcher.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(watcher), timeout=1.0)
        _LIVE_PROVIDERS.pop(claim.dispatch_id, None)
    finished = utcnow()
    category = _category_from_outcome(outcome.result)
    processing = outcome.result.processing_status
    current = await db.get(MISummaryDispatch, claim.dispatch_id)
    if (
        current is None
        or current.claim_token != claim.token
        or current.status != MISummaryDispatchStatus.CLAIMED.value
    ):
        return SummaryExecutionResult(
            status=SummaryExecutionStatus.CANCELLED,
            dispatch_id=claim.dispatch_id,
            session_id=claim.session_id,
            job_id=outcome.job.id,
        )
    values, exec_status = _terminal_values(
        processing=processing,
        category=category,
        failure_count=int(current.failure_count),
        boundary=boundary,
        limitations=list(getattr(outcome.result, "limitations", []) or []),
        before_token=before_token,
        after_token=progress_token(outcome.result.checkpoint),
        material_epoch=captured_material_epoch,
        checkpoint=outcome.result.checkpoint,
        stall_count=int(current.stall_count or 0),
    )
    values["job_id"] = outcome.job.id
    wrote = await _fenced(db, claim, values)
    if not wrote:
        return SummaryExecutionResult(
            status=SummaryExecutionStatus.CANCELLED,
            dispatch_id=claim.dispatch_id,
            session_id=claim.session_id,
            job_id=outcome.job.id,
        )
    if state is not None and category in _NON_RETRYABLE:
        state.work_intent = "blocked"
        state.blocked_category = category
        state.desired_config_fingerprint = config_fingerprint()
    if state is not None and exec_status == SummaryExecutionStatus.SUCCEEDED:
        fresh = await db.get(MISummarySessionState, claim.session_id)
        if fresh is not None:
            fresh.highest_processed_publication = max(
                int(fresh.highest_processed_publication), boundary
            )
            fresh.processed_config_fingerprint = captured_config
            fresh.processed_config_epoch = captured_config_epoch
            fresh.processed_source_fingerprint = material.source_fingerprint
            fresh.processed_coverage_fingerprint = material.coverage_fingerprint
            fresh.processed_stt_fingerprint = material.stt_fingerprint
            fresh.processed_finalize_epoch = captured_finalize_epoch
            fresh.intelligence_semantically_complete = _semantic_complete(coverage)
    await persist_integrated_metrics(
        db,
        session_id=claim.session_id,
        queued_at=queued_at,
        finished_at=finished,
        result=outcome.result,
        provider_calls=outcome.provider_calls,
        cache_hits=outcome.cache_hits,
        retries=1 if exec_status == SummaryExecutionStatus.RETRY_SCHEDULED else 0,
        source_fingerprint=material.source_fingerprint,
        config_fingerprint=captured_config,
        input_fingerprint=metrics_input_fingerprint(
            manifest=outcome.result.manifest,
            source_fingerprint=material.source_fingerprint,
            coverage_fingerprint=material.coverage_fingerprint,
            stt_fingerprint=material.stt_fingerprint,
        ),
        job_id=str(outcome.job.id),
        attempt_id=str(outcome.attempt.id),
        served_from_cache=outcome.served_from_completed_cache,
    )
    await db.commit()
    metrics = metrics_from_outcome(
        dispatch_id=str(claim.dispatch_id),
        session_id=str(claim.session_id),
        started_at=started,
        finished_at=finished,
        queued_at=queued_at,
        result=outcome.result,
        provider_calls=outcome.provider_calls,
        cache_hits=outcome.cache_hits,
        served_from_cache=outcome.served_from_completed_cache,
        job_id=str(outcome.job.id),
        attempt_id=str(outcome.attempt.id),
        retries=1 if exec_status == SummaryExecutionStatus.RETRY_SCHEDULED else 0,
    )
    return SummaryExecutionResult(
        status=exec_status,
        dispatch_id=claim.dispatch_id,
        session_id=claim.session_id,
        job_id=outcome.job.id,
        metrics=metrics,
    )


def _semantic_complete(coverage: object) -> bool:
    return bool(
        getattr(coverage, "recording_audio_complete", False)
        and getattr(coverage, "transcript_publication_complete", False)
        and not getattr(coverage, "stt_backlog_pending", True)
        and not getattr(coverage, "source_pending", True)
    )


def _category_from_outcome(result: object) -> str | None:
    checkpoint = getattr(result, "checkpoint", None)
    category = getattr(checkpoint, "last_error_category", None)
    if category:
        return str(category)
    for item in getattr(result, "limitations", []) or []:
        code = getattr(item, "code", None)
        if code in _NON_RETRYABLE:
            return str(code)
    return None


def _terminal_values(
    *,
    processing: ProcessingJobStatus,
    category: str | None,
    failure_count: int,
    boundary: int,
    limitations: list[object] | None = None,
    before_token: str = "",
    after_token: str = "",
    material_epoch: int = 0,
    checkpoint: object | None = None,
    stall_count: int = 0,
) -> tuple[dict[str, object], SummaryExecutionStatus]:
    settings = get_settings()
    cleared = {"claim_token": None, "claim_expires_at": None, "explicit_retry": False}
    if processing == ProcessingJobStatus.CANCELLED:
        return (
            {**cleared, "status": MISummaryDispatchStatus.CANCELLED.value},
            SummaryExecutionStatus.CANCELLED,
        )
    if category in _NON_RETRYABLE:
        return (
            {
                **cleared,
                "status": MISummaryDispatchStatus.CANCELLED.value,
                "last_error_category": category,
                "last_error_code": "nonretryable",
                "last_error_message": "summary provider rejected the call",
                "failure_count": failure_count + 1,
            },
            SummaryExecutionStatus.FAILED,
        )
    if processing == ProcessingJobStatus.SUCCEEDED:
        return (
            {
                **cleared,
                "status": MISummaryDispatchStatus.COMPLETED.value,
                "publication_boundary": boundary,
                "disposition": _RUNNABLE,
                "last_error_category": None,
                "last_error_code": None,
                "last_error_message": None,
            },
            SummaryExecutionStatus.SUCCEEDED,
        )
    if processing == ProcessingJobStatus.PARTIAL:
        codes = {str(getattr(item, "code", "")) for item in (limitations or [])}
        if "timeout" in codes and "wall_clock_exhausted" not in codes:
            return (
                {
                    **cleared,
                    "status": MISummaryDispatchStatus.PENDING.value,
                    "disposition": _RUNNABLE,
                    "failure_count": failure_count + 1,
                    "last_error_category": ProviderErrorCategory.TIMEOUT.value,
                    "last_error_code": "provider_timeout",
                    "last_error_message": "provider call exceeded deadline",
                    "next_attempt_at": utcnow()
                    + timedelta(seconds=_retry_delay_seconds("timeout", failure_count + 1)),
                    "publication_boundary": boundary,
                    "stall_count": 0,
                },
                SummaryExecutionStatus.RETRY_SCHEDULED,
            )
        return _partial_disposition(
            limitations=limitations or [],
            before_token=before_token,
            after_token=after_token,
            material_epoch=material_epoch,
            checkpoint=checkpoint,
            boundary=boundary,
            yield_seconds=float(settings.meeting_essence_fairness_yield_seconds),
            stall_count=stall_count,
            cleared=cleared,
        )
    return (
        {
            **cleared,
            "status": MISummaryDispatchStatus.PENDING.value,
            "failure_count": failure_count + 1,
            "last_error_category": category or ProviderErrorCategory.UNKNOWN.value,
            "last_error_code": "execution_failed",
            "last_error_message": "summary execution failed",
            "next_attempt_at": utcnow()
            + timedelta(seconds=_retry_delay_seconds(category, failure_count + 1)),
        },
        SummaryExecutionStatus.RETRY_SCHEDULED,
    )


async def _block_unconfigured(db: AsyncSession, claim: _DispatchClaim, session_id: UUID) -> None:
    await _fenced(
        db,
        claim,
        {
            "status": MISummaryDispatchStatus.CANCELLED.value,
            "claim_token": None,
            "claim_expires_at": None,
            "last_error_category": ProviderErrorCategory.CONFIG.value,
            "last_error_code": "provider_unconfigured",
            "last_error_message": "summary provider is not configured",
        },
    )
    state = await db.get(MISummarySessionState, session_id)
    if state is not None:
        state.work_intent = "blocked"
        state.blocked_category = ProviderErrorCategory.CONFIG.value
        state.desired_config_fingerprint = config_fingerprint()
    await db.commit()


async def _requeue(db: AsyncSession, claim: _DispatchClaim, *, failure: bool) -> None:
    values: dict[str, object] = {
        "status": MISummaryDispatchStatus.PENDING.value,
        "claim_token": None,
        "claim_expires_at": None,
        "next_attempt_at": utcnow(),
    }
    if failure:
        values["failure_count"] = claim.failure_count + 1
    await _fenced(db, claim, values)
    await db.commit()


def _session():
    from recantor.db import get_sessionmaker

    return get_sessionmaker()()


async def cancel_summary_dispatch(
    db: AsyncSession,
    *,
    dispatch_id: UUID,
    session_id: UUID,
) -> asyncio.Task[None] | None:
    dispatch = await db.scalar(
        select(MISummaryDispatch)
        .where(
            MISummaryDispatch.id == dispatch_id,
            MISummaryDispatch.session_id == session_id,
        )
        .with_for_update()
    )
    if dispatch is None:
        msg = "summary dispatch not found"
        raise LookupError(msg)
    if dispatch.attempt_id is not None and dispatch.attempt_generation is not None:
        repo = MeetingIntelligenceRepository(db)
        with contextlib.suppress(AttemptFenceError):
            await repo.cancel_attempt(
                attempt_id=dispatch.attempt_id,
                session_id=session_id,
                generation=int(dispatch.attempt_generation),
            )
    live = _LIVE_PROVIDERS.get(dispatch_id)
    if live is not None:
        live.request_cancel()
    if dispatch.status != MISummaryDispatchStatus.CANCELLED.value:
        dispatch.status = MISummaryDispatchStatus.CANCELLED.value
        dispatch.claim_token = None
        dispatch.claim_expires_at = None
        dispatch.intent_generation = int(dispatch.intent_generation) + 1
        state = await db.get(MISummarySessionState, session_id)
        if state is None:
            state = MISummarySessionState(session_id=session_id, work_intent="cancelled")
            db.add(state)
        else:
            state.work_intent = "cancelled"
    await db.flush()
    if live is None:
        return None
    stop = asyncio.create_task(live.cancel_running())
    _CANCEL_TASKS.add(stop)
    stop.add_done_callback(_CANCEL_TASKS.discard)
    return stop


async def retry_summary_dispatch(
    db: AsyncSession,
    *,
    dispatch_id: UUID,
    session_id: UUID,
) -> None:
    """Reopen one dispatch under the same depth lock as submit.

    A pending row is not forced runnable. A cancelled or blocked row becomes a
    deferred intent and is promoted only when claimed and runnable work is under
    the depth. Another session the user cancelled is left cancelled. A second
    retry updates this row and does not insert another open dispatch.
    """
    await _admit_lock(db)
    dispatch = await db.scalar(
        select(MISummaryDispatch)
        .where(
            MISummaryDispatch.id == dispatch_id,
            MISummaryDispatch.session_id == session_id,
        )
        .with_for_update()
    )
    if dispatch is None:
        msg = "summary dispatch not found"
        raise LookupError(msg)
    if dispatch.status == MISummaryDispatchStatus.CLAIMED.value:
        raise SummaryRetryDenied("summary attempt is still claimed")
    if dispatch.status == MISummaryDispatchStatus.COMPLETED.value:
        raise SummaryRetryDenied("completed summary dispatch is not retried")
    other_open = await db.scalar(
        select(MISummaryDispatch.id).where(
            MISummaryDispatch.session_id == session_id,
            MISummaryDispatch.status.in_(_OPEN),
            MISummaryDispatch.id != dispatch.id,
        )
    )
    if other_open is not None:
        raise SummaryRetryDenied("session already has an open summary dispatch")
    if dispatch.status == MISummaryDispatchStatus.PENDING.value:
        dispatch.explicit_retry = True
        if dispatch.disposition != _RUNNABLE:
            await _activate_or_defer(db, dispatch)
        await db.flush()
        return
    dispatch.status = MISummaryDispatchStatus.PENDING.value
    dispatch.disposition = _AWAITING
    dispatch.capacity_deferred = True
    dispatch.explicit_retry = True
    dispatch.failure_count = 0
    dispatch.last_error_category = None
    dispatch.last_error_code = None
    dispatch.last_error_message = None
    dispatch.claim_token = None
    dispatch.claim_expires_at = None
    dispatch.next_attempt_at = None
    dispatch.intent_generation = int(dispatch.intent_generation) + 1
    state = await db.get(MISummarySessionState, session_id)
    if state is None:
        state = MISummarySessionState(session_id=session_id, work_intent="open")
        db.add(state)
        await db.flush()
    else:
        state.work_intent = "open"
        state.blocked_category = None
        state.blocked_disposition = None
        state.blocked_source_fingerprint = None
        state.blocked_config_fingerprint = None
    await _activate_or_defer(db, dispatch)
    await db.flush()


def _partial_disposition(
    *,
    limitations: list[object],
    before_token: str,
    after_token: str,
    material_epoch: int,
    checkpoint: object | None,
    boundary: int,
    yield_seconds: float,
    stall_count: int,
    cleared: dict[str, object],
) -> tuple[dict[str, object], SummaryExecutionStatus]:
    codes = {str(getattr(item, "code", "")) for item in limitations}
    advanced = bool(after_token) and after_token != before_token
    if (
        not advanced
        and "wall_clock_exhausted" in codes
        and not (codes & _TERMINAL_PARTIAL_CODES)
        and stall_count < 2
    ):
        return (
            {
                **cleared,
                "status": MISummaryDispatchStatus.PENDING.value,
                "disposition": _RUNNABLE,
                "next_attempt_at": utcnow() + timedelta(seconds=yield_seconds),
                "publication_boundary": boundary,
                "progress_token": after_token or before_token or None,
                "stall_count": stall_count + 1,
            },
            SummaryExecutionStatus.PARTIAL,
        )
    forward = getattr(checkpoint, "forward_semantic_check_status", None)
    forward_value = getattr(forward, "value", forward)
    semantic_failed = forward_value == "failed" and "wall_clock_exhausted" not in codes
    resumable = bool(codes & _RESUMABLE_CODES) or "wall_clock_exhausted" in codes
    awaiting = bool(codes & _AWAITING_CODES) and not semantic_failed
    if advanced and resumable and not (codes & _TERMINAL_PARTIAL_CODES) and not semantic_failed:
        return (
            {
                **cleared,
                "status": MISummaryDispatchStatus.PENDING.value,
                "disposition": _RUNNABLE,
                "next_attempt_at": utcnow() + timedelta(seconds=yield_seconds),
                "publication_boundary": boundary,
                "progress_token": after_token,
                "stall_count": 0,
            },
            SummaryExecutionStatus.PARTIAL,
        )
    disposition = _AWAITING if awaiting and not advanced else _TERMINAL_PARTIAL
    return (
        {
            **cleared,
            "status": MISummaryDispatchStatus.PENDING.value,
            "disposition": disposition,
            "parked_material_epoch": material_epoch,
            "progress_token": after_token or None,
            "next_attempt_at": None,
        },
        SummaryExecutionStatus.PARKED,
    )


async def _dispatch_cancelled(dispatch_id: UUID) -> bool:
    async with _session() as db:
        status = await db.scalar(
            select(MISummaryDispatch.status).where(MISummaryDispatch.id == dispatch_id)
        )
    return status == MISummaryDispatchStatus.CANCELLED.value


async def _watch_cancel(dispatch_id: UUID, provider: PermitBoundMeetingEssenceProvider) -> None:
    while True:
        await asyncio.sleep(0.05)
        if await _dispatch_cancelled(dispatch_id):
            await provider.cancel_running()
            return


async def _mark_dispatch_cancelled(db: AsyncSession, claim: _DispatchClaim) -> None:
    current = await db.get(MISummaryDispatch, claim.dispatch_id)
    if current is None or current.status == MISummaryDispatchStatus.CANCELLED.value:
        return
    if current.claim_token != claim.token:
        return
    current.status = MISummaryDispatchStatus.CANCELLED.value
    current.claim_token = None
    current.claim_expires_at = None
    current.intent_generation = int(current.intent_generation) + 1
    await db.commit()
