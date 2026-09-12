from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import String, and_, case, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_sessionmaker
from recantor.models import (
    RecordingSession,
    SessionKind,
    STTJob,
    STTJobState,
    TranscriptionUtterance,
    TranscriptSegment,
)
from recantor.settings import get_settings
from recantor.stt import (
    STTErrorCategory,
    STTNoSpeech,
    STTProvider,
    STTProviderError,
    STTSourceError,
    STTSourceNotFound,
    transcribe_utterance,
)
from recantor.transcript import TranscriptCommitRejected
from recantor.utterance import transcript_producer_key_for_utterance


class STTJobError(RuntimeError):
    pass


class STTJobNotFound(STTJobError):
    pass


class STTWorkloadClass(StrEnum):
    LIVE = "live"
    UPLOAD = "upload"


@dataclass(frozen=True)
class STTClaim:
    utterance_id: UUID
    session_id: UUID
    token: str
    attempt_count: int
    expires_at: datetime


class STTExecutionStatus(StrEnum):
    NOT_CLAIMED = "not_claimed"
    SUCCEEDED = "succeeded"
    NO_SPEECH = "no_speech"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    STALE = "stale"


@dataclass(frozen=True)
class STTExecutionResult:
    status: STTExecutionStatus
    state: STTJobState | None
    attempt_count: int
    next_attempt_at: datetime | None = None


@dataclass(frozen=True)
class STTReconcileResult:
    created: int = 0
    converged: int = 0
    reserved: int = 0
    wake_target: int = 0
    dispatched: int = 0
    enqueue_failures: int = 0


@dataclass(frozen=True)
class _ReservationBatch:
    reserved_ids: tuple[UUID, ...]
    wake_target: int


_RETRYABLE_PROVIDER_CATEGORIES = {
    STTErrorCategory.RATE_LIMIT,
    STTErrorCategory.TIMEOUT,
    STTErrorCategory.TRANSIENT,
}
_STT_SCHEDULER_ADVISORY_LOCK = 0x524543414E544F52


def utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_message(message: str, *, fallback: str) -> str:
    normalized = " ".join(message.split()).strip() or fallback
    return normalized[:512]


def _effective_lease_seconds() -> float:
    settings = get_settings()
    return max(float(settings.stt_claim_lease_seconds), float(settings.groq_stt_timeout_seconds) + 10.0)


def _retry_delay_seconds(category: STTErrorCategory, attempt_count: int) -> float:
    settings = get_settings()
    if category == STTErrorCategory.CONFIGURATION:
        return float(settings.stt_configuration_retry_seconds)
    exponent = max(0, attempt_count - 1)
    return min(
        float(settings.stt_retry_max_seconds),
        float(settings.stt_retry_base_seconds) * (2**exponent),
    )


def _session_class_expression(workload_class: STTWorkloadClass):
    if workload_class == STTWorkloadClass.UPLOAD:
        return RecordingSession.kind == SessionKind.UPLOAD.value
    return RecordingSession.kind != SessionKind.UPLOAD.value


async def _database_now(db: AsyncSession) -> datetime:
    current = await db.scalar(select(func.clock_timestamp()))
    if current is None:
        raise STTJobError("database did not return a scheduling clock")
    return current


async def _canonical_exists(db: AsyncSession, *, session_id: UUID, utterance_id: UUID) -> bool:
    return (
        await db.scalar(
            select(TranscriptSegment.id).where(
                TranscriptSegment.session_id == session_id,
                TranscriptSegment.producer_key == transcript_producer_key_for_utterance(utterance_id),
            )
        )
        is not None
    )


def _clear_claim(job: STTJob) -> None:
    job.claim_token = None
    job.claim_expires_at = None


def _mark_succeeded(job: STTJob) -> None:
    job.state = STTJobState.SUCCEEDED.value
    job.next_attempt_at = None
    _clear_claim(job)
    job.last_error_category = None
    job.last_error_code = None
    job.last_error_message = None


def _mark_no_speech(job: STTJob) -> None:
    job.state = STTJobState.NO_SPEECH.value
    job.next_attempt_at = None
    _clear_claim(job)
    job.last_error_category = None
    job.last_error_code = None
    job.last_error_message = None


def _repair_false_success(job: STTJob) -> None:
    job.state = STTJobState.PENDING.value
    job.attempt_count = 0
    job.next_attempt_at = None
    _clear_claim(job)
    job.last_delivery_attempt_at = None
    job.last_error_category = None
    job.last_error_code = None
    job.last_error_message = None


def _mark_retry_budget_exhausted(job: STTJob) -> None:
    job.state = STTJobState.FAILED.value
    job.next_attempt_at = None
    _clear_claim(job)
    job.last_error_category = "scheduler"
    job.last_error_code = "retry_budget_exhausted"
    job.last_error_message = "automatic STT attempt budget exhausted"


async def _claim_locked_job(
    db: AsyncSession,
    *,
    job: STTJob,
    current: datetime,
    lease_seconds: float,
) -> STTClaim | None:
    settings = get_settings()
    canonical = await _canonical_exists(db, session_id=job.session_id, utterance_id=job.utterance_id)
    if canonical:
        _mark_succeeded(job)
        return None
    if job.state == STTJobState.SUCCEEDED.value:
        _repair_false_success(job)
    if job.state in {STTJobState.NO_SPEECH.value, STTJobState.FAILED.value}:
        return None
    if (
        job.state == STTJobState.CLAIMED.value
        and job.claim_expires_at is not None
        and job.claim_expires_at > current
    ):
        return None
    if (
        job.state == STTJobState.RETRY_WAIT.value
        and job.next_attempt_at is not None
        and job.next_attempt_at > current
    ):
        return None
    if job.attempt_count >= settings.stt_max_attempts:
        _mark_retry_budget_exhausted(job)
        return None

    token = uuid4().hex
    expires_at = current + timedelta(seconds=lease_seconds)
    job.state = STTJobState.CLAIMED.value
    job.attempt_count += 1
    job.next_attempt_at = None
    job.claim_token = token
    job.claim_expires_at = expires_at
    await db.flush()
    return STTClaim(
        utterance_id=job.utterance_id,
        session_id=job.session_id,
        token=token,
        attempt_count=job.attempt_count,
        expires_at=expires_at,
    )


async def claim_stt_job(
    *,
    utterance_id: UUID,
    now: datetime | None = None,
    lease_seconds: float | None = None,
) -> STTClaim | None:
    lease = lease_seconds if lease_seconds is not None else _effective_lease_seconds()
    if lease <= 0:
        raise STTJobError("STT claim lease must be positive")
    async with get_sessionmaker()() as db, db.begin():
        job = await db.scalar(select(STTJob).where(STTJob.utterance_id == utterance_id).with_for_update())
        if job is None:
            return None
        current = now or await _database_now(db)
        return await _claim_locked_job(db, job=job, current=current, lease_seconds=lease)


async def _claim_next_reserved_stt_job(
    *,
    workload_class: STTWorkloadClass = STTWorkloadClass.LIVE,
    now: datetime | None = None,
    lease_seconds: float | None = None,
    cooldown_seconds: float | None = None,
) -> STTClaim | None:
    settings = get_settings()
    lease = lease_seconds if lease_seconds is not None else _effective_lease_seconds()
    if lease <= 0:
        raise STTJobError("STT claim lease must be positive")
    cooldown = float(settings.stt_dispatch_reenqueue_seconds) if cooldown_seconds is None else cooldown_seconds
    if cooldown < 0:
        raise STTJobError("STT dispatch cooldown must be non-negative")

    for _ in range(8):
        async with get_sessionmaker()() as db, db.begin():
            current = now or await _database_now(db)
            cooldown_before = current - timedelta(seconds=cooldown)
            job = await db.scalar(
                select(STTJob)
                .join(TranscriptionUtterance, TranscriptionUtterance.id == STTJob.utterance_id)
                .join(RecordingSession, RecordingSession.id == STTJob.session_id)
                .where(
                    _session_class_expression(workload_class),
                    _delivery_reservation_expression(current, cooldown_before),
                )
                .order_by(
                    STTJob.last_delivery_attempt_at,
                    TranscriptionUtterance.created_at,
                    STTJob.session_id,
                    TranscriptionUtterance.sequence,
                    STTJob.utterance_id,
                )
                .with_for_update(of=STTJob, skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            claim = await _claim_locked_job(db, job=job, current=current, lease_seconds=lease)
            if claim is not None:
                return claim
    return None


async def _claim_commit_is_current(db: AsyncSession, claim: STTClaim) -> bool:
    job = await db.scalar(select(STTJob).where(STTJob.utterance_id == claim.utterance_id).with_for_update())
    return (
        job is not None
        and job.session_id == claim.session_id
        and job.state == STTJobState.CLAIMED.value
        and job.claim_token == claim.token
    )


async def _complete_claim_success(claim: STTClaim) -> bool:
    async with get_sessionmaker()() as db, db.begin():
        job = await db.scalar(select(STTJob).where(STTJob.utterance_id == claim.utterance_id).with_for_update())
        if job is None:
            return False
        if await _canonical_exists(db, session_id=job.session_id, utterance_id=job.utterance_id):
            _mark_succeeded(job)
            return True
        return False


async def _complete_claim_no_speech(claim: STTClaim) -> bool:
    async with get_sessionmaker()() as db, db.begin():
        job = await db.scalar(select(STTJob).where(STTJob.utterance_id == claim.utterance_id).with_for_update())
        if job is None:
            return False
        if await _canonical_exists(db, session_id=job.session_id, utterance_id=job.utterance_id):
            _mark_succeeded(job)
            return True
        if job.state != STTJobState.CLAIMED.value or job.claim_token != claim.token:
            return False
        _mark_no_speech(job)
        return True


async def _record_claim_failure(
    claim: STTClaim,
    *,
    category: str,
    code: str,
    message: str,
    retry_category: STTErrorCategory | None,
    now: datetime | None = None,
) -> STTExecutionResult:
    settings = get_settings()
    async with get_sessionmaker()() as db, db.begin():
        job = await db.scalar(select(STTJob).where(STTJob.utterance_id == claim.utterance_id).with_for_update())
        if job is None:
            return STTExecutionResult(STTExecutionStatus.STALE, None, 0)
        if await _canonical_exists(db, session_id=job.session_id, utterance_id=job.utterance_id):
            _mark_succeeded(job)
            return STTExecutionResult(STTExecutionStatus.SUCCEEDED, STTJobState.SUCCEEDED, job.attempt_count)
        if job.state != STTJobState.CLAIMED.value or job.claim_token != claim.token:
            return STTExecutionResult(
                STTExecutionStatus.STALE,
                STTJobState(job.state),
                job.attempt_count,
                job.next_attempt_at,
            )
        current = now or await _database_now(db)
        job.last_error_category = category[:64]
        job.last_error_code = code[:96]
        job.last_error_message = _safe_message(message, fallback=code)
        _clear_claim(job)
        retryable = retry_category in _RETRYABLE_PROVIDER_CATEGORIES or retry_category == STTErrorCategory.CONFIGURATION
        if retryable and job.attempt_count < settings.stt_max_attempts:
            delay = _retry_delay_seconds(retry_category, job.attempt_count)
            job.state = STTJobState.RETRY_WAIT.value
            job.next_attempt_at = current + timedelta(seconds=delay)
            return STTExecutionResult(
                STTExecutionStatus.RETRY_SCHEDULED,
                STTJobState.RETRY_WAIT,
                job.attempt_count,
                job.next_attempt_at,
            )
        job.state = STTJobState.FAILED.value
        job.next_attempt_at = None
        return STTExecutionResult(STTExecutionStatus.FAILED, STTJobState.FAILED, job.attempt_count)


async def _stale_execution_result(claim: STTClaim) -> STTExecutionResult:
    async with get_sessionmaker()() as db:
        job = await db.get(STTJob, claim.utterance_id)
    return STTExecutionResult(
        STTExecutionStatus.STALE,
        None if job is None else STTJobState(job.state),
        claim.attempt_count if job is None else job.attempt_count,
        None if job is None else job.next_attempt_at,
    )


async def _execute_claim(*, claim: STTClaim, provider: STTProvider, now: datetime | None = None) -> STTExecutionResult:
    async def commit_guard(db: AsyncSession) -> bool:
        return await _claim_commit_is_current(db, claim)

    try:
        await transcribe_utterance(
            session_id=claim.session_id,
            work_id=claim.utterance_id,
            provider=provider,
            commit_guard=commit_guard,
        )
    except TranscriptCommitRejected:
        return await _stale_execution_result(claim)
    except STTNoSpeech:
        completed = await _complete_claim_no_speech(claim)
        if not completed:
            return await _stale_execution_result(claim)
        async with get_sessionmaker()() as db:
            job = await db.get(STTJob, claim.utterance_id)
        state = None if job is None else STTJobState(job.state)
        status = STTExecutionStatus.SUCCEEDED if state == STTJobState.SUCCEEDED else STTExecutionStatus.NO_SPEECH
        return STTExecutionResult(status, state, claim.attempt_count)
    except STTProviderError as exc:
        return await _record_claim_failure(
            claim,
            category=exc.category.value,
            code=f"provider_{exc.category.value}",
            message=f"STT provider {exc.category.value} failure",
            retry_category=exc.category,
            now=now,
        )
    except STTSourceNotFound:
        return await _record_claim_failure(
            claim,
            category="source",
            code="source_not_found",
            message="transcription utterance source was not found",
            retry_category=None,
            now=now,
        )
    except STTSourceError:
        return await _record_claim_failure(
            claim,
            category="source",
            code="source_validation_failed",
            message="durable utterance source failed verification",
            retry_category=None,
            now=now,
        )
    except Exception as exc:
        return await _record_claim_failure(
            claim,
            category=STTErrorCategory.TRANSIENT.value,
            code="unexpected_worker_error",
            message=f"unexpected STT worker error: {type(exc).__name__}",
            retry_category=STTErrorCategory.TRANSIENT,
            now=now,
        )

    completed = await _complete_claim_success(claim)
    if not completed:
        return await _stale_execution_result(claim)
    return STTExecutionResult(STTExecutionStatus.SUCCEEDED, STTJobState.SUCCEEDED, claim.attempt_count)


async def execute_stt_job(
    *,
    utterance_id: UUID,
    provider: STTProvider,
    now: datetime | None = None,
    lease_seconds: float | None = None,
) -> STTExecutionResult:
    claim = await claim_stt_job(utterance_id=utterance_id, now=now, lease_seconds=lease_seconds)
    if claim is None:
        async with get_sessionmaker()() as db:
            job = await db.get(STTJob, utterance_id)
        return STTExecutionResult(
            STTExecutionStatus.NOT_CLAIMED,
            None if job is None else STTJobState(job.state),
            0 if job is None else job.attempt_count,
            None if job is None else job.next_attempt_at,
        )
    return await _execute_claim(claim=claim, provider=provider, now=now)


async def execute_next_reserved_stt_job(
    *,
    provider: STTProvider,
    workload_class: STTWorkloadClass = STTWorkloadClass.LIVE,
    now: datetime | None = None,
    lease_seconds: float | None = None,
    cooldown_seconds: float | None = None,
) -> STTExecutionResult:
    claim = await _claim_next_reserved_stt_job(
        workload_class=workload_class,
        now=now,
        lease_seconds=lease_seconds,
        cooldown_seconds=cooldown_seconds,
    )
    if claim is None:
        return STTExecutionResult(STTExecutionStatus.NOT_CLAIMED, None, 0)
    return await _execute_claim(claim=claim, provider=provider, now=now)


async def requeue_failed_stt_job(
    db: AsyncSession,
    *,
    session_id: UUID,
    utterance_id: UUID,
) -> STTJob:
    async with db.begin():
        job = await db.scalar(
            select(STTJob)
            .where(STTJob.utterance_id == utterance_id, STTJob.session_id == session_id)
            .with_for_update()
        )
        if job is None:
            raise STTJobNotFound("STT scheduling job not found")
        if await _canonical_exists(db, session_id=session_id, utterance_id=utterance_id):
            _mark_succeeded(job)
            return job
        if job.state == STTJobState.CLAIMED.value:
            raise STTJobError("active STT claim cannot be requeued")
        job.state = STTJobState.PENDING.value
        job.attempt_count = 0
        job.next_attempt_at = None
        _clear_claim(job)
        job.last_error_category = None
        job.last_error_code = None
        job.last_error_message = None
        job.last_delivery_attempt_at = None
        return job


async def _ensure_missing_jobs(batch_size: int) -> int:
    async with get_sessionmaker()() as db:
        missing = list((await db.scalars(
            select(TranscriptionUtterance)
            .outerjoin(STTJob, STTJob.utterance_id == TranscriptionUtterance.id)
            .where(STTJob.utterance_id.is_(None))
            .order_by(
                TranscriptionUtterance.created_at,
                TranscriptionUtterance.session_id,
                TranscriptionUtterance.sequence,
            )
            .limit(batch_size)
        )).all())
        if not missing:
            return 0
        statement = (
            insert(STTJob)
            .values([
                {"utterance_id": work.id, "session_id": work.session_id, "state": STTJobState.PENDING.value}
                for work in missing
            ])
            .on_conflict_do_nothing(index_elements=[STTJob.utterance_id])
            .returning(STTJob.utterance_id)
        )
        created_ids = list((await db.scalars(statement)).all())
        await db.commit()
        return len(created_ids)


async def _converge_canonical(batch_size: int) -> int:
    transcript_key = func.concat("utterance:", cast(STTJob.utterance_id, String))
    async with get_sessionmaker()() as db, db.begin():
        jobs = list((await db.scalars(
            select(STTJob)
            .join(TranscriptSegment, and_(
                TranscriptSegment.session_id == STTJob.session_id,
                TranscriptSegment.producer_key == transcript_key,
            ))
            .where(STTJob.state != STTJobState.SUCCEEDED.value)
            .order_by(STTJob.updated_at, STTJob.utterance_id)
            .limit(batch_size)
            .with_for_update(of=STTJob, skip_locked=True)
        )).all())
        for job in jobs:
            _mark_succeeded(job)
        return len(jobs)


async def _repair_false_succeeded(batch_size: int) -> int:
    transcript_key = func.concat("utterance:", cast(STTJob.utterance_id, String))
    async with get_sessionmaker()() as db, db.begin():
        jobs = list((await db.scalars(
            select(STTJob)
            .outerjoin(TranscriptSegment, and_(
                TranscriptSegment.session_id == STTJob.session_id,
                TranscriptSegment.producer_key == transcript_key,
            ))
            .where(STTJob.state == STTJobState.SUCCEEDED.value, TranscriptSegment.id.is_(None))
            .order_by(STTJob.updated_at, STTJob.utterance_id)
            .limit(batch_size)
            .with_for_update(of=STTJob, skip_locked=True)
        )).all())
        for job in jobs:
            _repair_false_success(job)
        return len(jobs)


def _delivery_reservation_expression(current: datetime, cooldown_before: datetime):
    recent = and_(STTJob.last_delivery_attempt_at.is_not(None), STTJob.last_delivery_attempt_at > cooldown_before)
    pending = STTJob.state == STTJobState.PENDING.value
    due_retry = and_(
        STTJob.state == STTJobState.RETRY_WAIT.value,
        STTJob.next_attempt_at.is_not(None),
        STTJob.next_attempt_at <= current,
        STTJob.last_delivery_attempt_at >= STTJob.next_attempt_at,
    )
    expired_claim = and_(
        STTJob.state == STTJobState.CLAIMED.value,
        STTJob.claim_expires_at.is_not(None),
        STTJob.claim_expires_at <= current,
        STTJob.last_delivery_attempt_at >= STTJob.claim_expires_at,
    )
    return and_(recent, or_(pending, due_retry, expired_claim))


def _eligible_expression(current: datetime, cooldown_before: datetime):
    no_recent_delivery = or_(
        STTJob.last_delivery_attempt_at.is_(None),
        STTJob.last_delivery_attempt_at <= cooldown_before,
    )
    pending = and_(STTJob.state == STTJobState.PENDING.value, no_recent_delivery)
    due_retry = and_(
        STTJob.state == STTJobState.RETRY_WAIT.value,
        STTJob.next_attempt_at.is_not(None),
        STTJob.next_attempt_at <= current,
        or_(no_recent_delivery, STTJob.last_delivery_attempt_at < STTJob.next_attempt_at),
    )
    expired_claim = and_(
        STTJob.state == STTJobState.CLAIMED.value,
        STTJob.claim_expires_at.is_not(None),
        STTJob.claim_expires_at <= current,
        or_(no_recent_delivery, STTJob.last_delivery_attempt_at < STTJob.claim_expires_at),
    )
    return or_(pending, due_retry, expired_claim)


def _outstanding_expression(current: datetime, cooldown_before: datetime):
    active_claim = and_(
        STTJob.state == STTJobState.CLAIMED.value,
        STTJob.claim_expires_at.is_not(None),
        STTJob.claim_expires_at > current,
    )
    return or_(active_claim, _delivery_reservation_expression(current, cooldown_before))


async def _reserve_fair_candidates(
    *,
    workload_class: STTWorkloadClass,
    now: datetime | None,
    limit: int,
    per_session_limit: int,
    cooldown_seconds: float,
) -> _ReservationBatch:
    async with get_sessionmaker()() as db, db.begin():
        # Independent class queues still serialize this tiny selection window. Provider/network
        # calls never hold this lock and upload workers cannot claim live-class reservations.
        await db.execute(select(func.pg_advisory_xact_lock(_STT_SCHEDULER_ADVISORY_LOCK)))
        current = now or await _database_now(db)
        cooldown_before = current - timedelta(seconds=cooldown_seconds)
        outstanding_expression = _outstanding_expression(current, cooldown_before)
        class_expression = _session_class_expression(workload_class)

        global_outstanding = int(
            await db.scalar(
                select(func.count(STTJob.utterance_id))
                .join(RecordingSession, RecordingSession.id == STTJob.session_id)
                .where(class_expression, outstanding_expression)
            )
            or 0
        )
        available_global = max(0, limit - global_outstanding)

        if available_global > 0:
            outstanding = (
                select(
                    STTJob.session_id.label("session_id"),
                    func.count(STTJob.utterance_id).label("outstanding_count"),
                )
                .join(RecordingSession, RecordingSession.id == STTJob.session_id)
                .where(_session_class_expression(workload_class), outstanding_expression)
                .group_by(STTJob.session_id)
                .subquery()
            )
            session_history = (
                select(
                    STTJob.session_id.label("session_id"),
                    func.max(STTJob.last_delivery_attempt_at).label("last_delivery_attempt_at"),
                    func.min(STTJob.created_at).label("oldest_created_at"),
                )
                .join(RecordingSession, RecordingSession.id == STTJob.session_id)
                .where(_session_class_expression(workload_class))
                .group_by(STTJob.session_id)
                .subquery()
            )
            session_rank = func.row_number().over(
                partition_by=STTJob.session_id,
                order_by=(TranscriptionUtterance.sequence, STTJob.created_at, STTJob.utterance_id),
            )
            ranked = (
                select(
                    STTJob.utterance_id.label("utterance_id"),
                    session_rank.label("session_rank"),
                    STTJob.session_id.label("session_id"),
                )
                .join(TranscriptionUtterance, TranscriptionUtterance.id == STTJob.utterance_id)
                .join(RecordingSession, RecordingSession.id == STTJob.session_id)
                .where(_session_class_expression(workload_class), _eligible_expression(current, cooldown_before))
                .subquery()
            )
            outstanding_count = func.coalesce(outstanding.c.outstanding_count, 0)
            service_turn = func.coalesce(
                session_history.c.last_delivery_attempt_at,
                session_history.c.oldest_created_at,
            )
            candidate_ids = list((await db.scalars(
                select(ranked.c.utterance_id)
                .outerjoin(outstanding, outstanding.c.session_id == ranked.c.session_id)
                .outerjoin(session_history, session_history.c.session_id == ranked.c.session_id)
                .where(
                    outstanding_count < per_session_limit,
                    ranked.c.session_rank <= per_session_limit - outstanding_count,
                )
                .order_by(ranked.c.session_rank, service_turn, ranked.c.session_id, ranked.c.utterance_id)
                .limit(available_global)
            )).all())
        else:
            candidate_ids = []

        reserved_ids: list[UUID] = []
        if candidate_ids:
            stamp = case(
                (STTJob.last_delivery_attempt_at.is_(None), current),
                else_=func.greatest(STTJob.last_delivery_attempt_at, current),
            )
            statement = (
                update(STTJob)
                .where(STTJob.utterance_id.in_(candidate_ids), _eligible_expression(current, cooldown_before))
                .values(last_delivery_attempt_at=stamp)
                .returning(STTJob.utterance_id)
            )
            reserved_ids = list((await db.scalars(statement)).all())

        wake_target = int(
            await db.scalar(
                select(func.count(STTJob.utterance_id))
                .join(RecordingSession, RecordingSession.id == STTJob.session_id)
                .where(
                    _session_class_expression(workload_class),
                    _delivery_reservation_expression(current, cooldown_before),
                )
            )
            or 0
        )
        return _ReservationBatch(tuple(reserved_ids), wake_target)


async def reconcile_stt_jobs(
    *,
    enqueue: Callable[[UUID], object] | None = None,
    ensure_wake_capacity: Callable[[int], int] | None = None,
    workload_class: STTWorkloadClass = STTWorkloadClass.LIVE,
    now: datetime | None = None,
    limit: int | None = None,
    per_session_limit: int | None = None,
    cooldown_seconds: float | None = None,
) -> STTReconcileResult:
    settings = get_settings()
    if limit is not None:
        batch_size = limit
    elif workload_class == STTWorkloadClass.UPLOAD:
        batch_size = settings.stt_upload_reconcile_batch_size
    else:
        batch_size = settings.stt_reconcile_batch_size
    if per_session_limit is not None:
        session_limit = per_session_limit
    elif workload_class == STTWorkloadClass.UPLOAD:
        session_limit = settings.stt_upload_reconcile_per_session_limit
    else:
        session_limit = settings.stt_reconcile_per_session_limit
    if batch_size < 1:
        raise STTJobError("STT reconcile batch size must be positive")
    if session_limit < 1:
        raise STTJobError("STT per-session reconcile limit must be positive")
    cooldown = float(settings.stt_dispatch_reenqueue_seconds) if cooldown_seconds is None else cooldown_seconds
    if cooldown < 0:
        raise STTJobError("STT dispatch cooldown must be non-negative")
    if enqueue is None and ensure_wake_capacity is None:
        raise STTJobError("STT reconciliation requires a broker wake callback")

    repair_batch = max(batch_size * 4, batch_size)
    created = await _ensure_missing_jobs(repair_batch)
    converged = await _converge_canonical(repair_batch)
    converged += await _repair_false_succeeded(repair_batch)
    reservations = await _reserve_fair_candidates(
        workload_class=workload_class,
        now=now,
        limit=batch_size,
        per_session_limit=session_limit,
        cooldown_seconds=cooldown,
    )

    dispatched = 0
    enqueue_failures = 0
    if ensure_wake_capacity is not None:
        try:
            dispatched = int(ensure_wake_capacity(reservations.wake_target))
        except Exception:
            enqueue_failures = 1
    elif enqueue is not None:
        for utterance_id in reservations.reserved_ids:
            try:
                enqueue(utterance_id)
            except Exception:
                enqueue_failures += 1
            else:
                dispatched += 1

    return STTReconcileResult(
        created=created,
        converged=converged,
        reserved=len(reservations.reserved_ids),
        wake_target=reservations.wake_target,
        dispatched=dispatched,
        enqueue_failures=enqueue_failures,
    )


async def read_stt_diagnostics(
    db: AsyncSession,
    *,
    session_id: UUID,
    failure_limit: int = 20,
) -> tuple[dict[str, int], list[STTJob]]:
    if failure_limit < 1 or failure_limit > 100:
        raise STTJobError("failure_limit must be between 1 and 100")
    if await db.scalar(select(RecordingSession.id).where(RecordingSession.id == session_id)) is None:
        raise STTJobNotFound("recording session not found")
    grouped = (
        await db.execute(
            select(STTJob.state, func.count(STTJob.utterance_id))
            .where(STTJob.session_id == session_id)
            .group_by(STTJob.state)
        )
    ).all()
    counts = {state.value: 0 for state in STTJobState}
    for state, count in grouped:
        counts[state] = int(count)
    failures = list((await db.scalars(
        select(STTJob)
        .where(STTJob.session_id == session_id, STTJob.last_error_category.is_not(None))
        .order_by(STTJob.updated_at.desc(), STTJob.utterance_id)
        .limit(failure_limit)
    )).all())
    return counts, failures
