from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_sessionmaker
from recantor.models import (
    RecordingSession,
    STTJob,
    STTJobState,
    TranscriptionUtterance,
    TranscriptSegment,
)
from recantor.settings import get_settings
from recantor.stt import (
    STTErrorCategory,
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
    dispatched: int = 0
    enqueue_failures: int = 0


_RETRYABLE_PROVIDER_CATEGORIES = {
    STTErrorCategory.RATE_LIMIT,
    STTErrorCategory.TIMEOUT,
    STTErrorCategory.TRANSIENT,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_message(message: str, *, fallback: str) -> str:
    normalized = " ".join(message.split()).strip()
    if not normalized:
        normalized = fallback
    return normalized[:512]


def _effective_lease_seconds() -> float:
    settings = get_settings()
    # A claim should normally outlive a single provider timeout so duplicate delivery cannot
    # create concurrent provider calls merely because the provider is slow.
    return max(
        float(settings.stt_claim_lease_seconds),
        float(settings.groq_stt_timeout_seconds) + 10.0,
    )


def _retry_delay_seconds(category: STTErrorCategory, attempt_count: int) -> float:
    settings = get_settings()
    if category == STTErrorCategory.CONFIGURATION:
        return float(settings.stt_configuration_retry_seconds)
    exponent = max(0, attempt_count - 1)
    return min(
        float(settings.stt_retry_max_seconds),
        float(settings.stt_retry_base_seconds) * (2**exponent),
    )


async def _database_now(db: AsyncSession) -> datetime:
    current = await db.scalar(select(func.clock_timestamp()))
    if current is None:
        raise STTJobError("database did not return a scheduling clock")
    return current


async def _scheduler_now(now: datetime | None) -> datetime:
    if now is not None:
        return now
    async with get_sessionmaker()() as db:
        return await _database_now(db)


async def _canonical_exists(
    db: AsyncSession,
    *,
    session_id: UUID,
    utterance_id: UUID,
) -> bool:
    return (
        await db.scalar(
            select(TranscriptSegment.id).where(
                TranscriptSegment.session_id == session_id,
                TranscriptSegment.producer_key
                == transcript_producer_key_for_utterance(utterance_id),
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


def _mark_retry_budget_exhausted(job: STTJob) -> None:
    job.state = STTJobState.FAILED.value
    job.next_attempt_at = None
    _clear_claim(job)
    job.last_error_category = "scheduler"
    job.last_error_code = "retry_budget_exhausted"
    job.last_error_message = "automatic STT attempt budget exhausted"


async def claim_stt_job(
    *,
    utterance_id: UUID,
    now: datetime | None = None,
    lease_seconds: float | None = None,
) -> STTClaim | None:
    settings = get_settings()
    lease = lease_seconds if lease_seconds is not None else _effective_lease_seconds()
    if lease <= 0:
        raise STTJobError("STT claim lease must be positive")

    async with get_sessionmaker()() as db, db.begin():
        job = await db.scalar(
            select(STTJob).where(STTJob.utterance_id == utterance_id).with_for_update()
        )
        if job is None:
            return None
        current = now or await _database_now(db)

        if await _canonical_exists(
            db,
            session_id=job.session_id,
            utterance_id=job.utterance_id,
        ):
            _mark_succeeded(job)
            return None

        if job.state in {STTJobState.SUCCEEDED.value, STTJobState.FAILED.value}:
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
        expires_at = current + timedelta(seconds=lease)
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


async def _claim_commit_is_current(db: AsyncSession, claim: STTClaim) -> bool:
    job = await db.scalar(
        select(STTJob).where(STTJob.utterance_id == claim.utterance_id).with_for_update()
    )
    return (
        job is not None
        and job.session_id == claim.session_id
        and job.state == STTJobState.CLAIMED.value
        and job.claim_token == claim.token
    )


async def _complete_claim_success(claim: STTClaim) -> bool:
    async with get_sessionmaker()() as db, db.begin():
        job = await db.scalar(
            select(STTJob).where(STTJob.utterance_id == claim.utterance_id).with_for_update()
        )
        if job is None:
            return False
        if await _canonical_exists(
            db,
            session_id=job.session_id,
            utterance_id=job.utterance_id,
        ):
            _mark_succeeded(job)
            return True
        if job.state != STTJobState.CLAIMED.value or job.claim_token != claim.token:
            return False
        return False


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
        job = await db.scalar(
            select(STTJob).where(STTJob.utterance_id == claim.utterance_id).with_for_update()
        )
        if job is None:
            return STTExecutionResult(
                status=STTExecutionStatus.STALE,
                state=None,
                attempt_count=0,
            )

        if await _canonical_exists(
            db,
            session_id=job.session_id,
            utterance_id=job.utterance_id,
        ):
            _mark_succeeded(job)
            return STTExecutionResult(
                status=STTExecutionStatus.SUCCEEDED,
                state=STTJobState.SUCCEEDED,
                attempt_count=job.attempt_count,
            )

        if job.state != STTJobState.CLAIMED.value or job.claim_token != claim.token:
            return STTExecutionResult(
                status=STTExecutionStatus.STALE,
                state=STTJobState(job.state),
                attempt_count=job.attempt_count,
                next_attempt_at=job.next_attempt_at,
            )

        current = now or await _database_now(db)
        job.last_error_category = category[:64]
        job.last_error_code = code[:96]
        job.last_error_message = _safe_message(message, fallback=code)
        _clear_claim(job)

        retryable = retry_category in _RETRYABLE_PROVIDER_CATEGORIES or (
            retry_category == STTErrorCategory.CONFIGURATION
        )
        if retryable and job.attempt_count < settings.stt_max_attempts:
            delay = _retry_delay_seconds(retry_category, job.attempt_count)
            job.state = STTJobState.RETRY_WAIT.value
            job.next_attempt_at = current + timedelta(seconds=delay)
            return STTExecutionResult(
                status=STTExecutionStatus.RETRY_SCHEDULED,
                state=STTJobState.RETRY_WAIT,
                attempt_count=job.attempt_count,
                next_attempt_at=job.next_attempt_at,
            )

        job.state = STTJobState.FAILED.value
        job.next_attempt_at = None
        return STTExecutionResult(
            status=STTExecutionStatus.FAILED,
            state=STTJobState.FAILED,
            attempt_count=job.attempt_count,
        )


async def _stale_execution_result(claim: STTClaim) -> STTExecutionResult:
    async with get_sessionmaker()() as db:
        job = await db.get(STTJob, claim.utterance_id)
    return STTExecutionResult(
        status=STTExecutionStatus.STALE,
        state=None if job is None else STTJobState(job.state),
        attempt_count=claim.attempt_count if job is None else job.attempt_count,
        next_attempt_at=None if job is None else job.next_attempt_at,
    )


async def execute_stt_job(
    *,
    utterance_id: UUID,
    provider: STTProvider,
    now: datetime | None = None,
    lease_seconds: float | None = None,
) -> STTExecutionResult:
    claim = await claim_stt_job(
        utterance_id=utterance_id,
        now=now,
        lease_seconds=lease_seconds,
    )
    if claim is None:
        async with get_sessionmaker()() as db:
            job = await db.get(STTJob, utterance_id)
        return STTExecutionResult(
            status=STTExecutionStatus.NOT_CLAIMED,
            state=None if job is None else STTJobState(job.state),
            attempt_count=0 if job is None else job.attempt_count,
            next_attempt_at=None if job is None else job.next_attempt_at,
        )

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
    return STTExecutionResult(
        status=STTExecutionStatus.SUCCEEDED,
        state=STTJobState.SUCCEEDED,
        attempt_count=claim.attempt_count,
    )


async def requeue_failed_stt_job(
    db: AsyncSession,
    *,
    session_id: UUID,
    utterance_id: UUID,
) -> STTJob:
    async with db.begin():
        job = await db.scalar(
            select(STTJob)
            .where(
                STTJob.utterance_id == utterance_id,
                STTJob.session_id == session_id,
            )
            .with_for_update()
        )
        if job is None:
            raise STTJobNotFound("STT scheduling job not found")
        if await _canonical_exists(
            db,
            session_id=session_id,
            utterance_id=utterance_id,
        ):
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
        missing = list(
            (
                await db.scalars(
                    select(TranscriptionUtterance)
                    .outerjoin(
                        STTJob,
                        STTJob.utterance_id == TranscriptionUtterance.id,
                    )
                    .where(STTJob.utterance_id.is_(None))
                    .order_by(
                        TranscriptionUtterance.created_at,
                        TranscriptionUtterance.session_id,
                        TranscriptionUtterance.sequence,
                    )
                    .limit(batch_size)
                )
            ).all()
        )
        if not missing:
            return 0
        statement = (
            insert(STTJob)
            .values(
                [
                    {
                        "utterance_id": work.id,
                        "session_id": work.session_id,
                        "state": STTJobState.PENDING.value,
                    }
                    for work in missing
                ]
            )
            .on_conflict_do_nothing(index_elements=[STTJob.utterance_id])
            .returning(STTJob.utterance_id)
        )
        created_ids = list((await db.scalars(statement)).all())
        await db.commit()
        return len(created_ids)


async def _converge_canonical(batch_size: int) -> int:
    transcript_key = func.concat("utterance:", cast(STTJob.utterance_id, String))
    async with get_sessionmaker()() as db, db.begin():
        jobs = list(
            (
                await db.scalars(
                    select(STTJob)
                    .join(
                        TranscriptSegment,
                        and_(
                            TranscriptSegment.session_id == STTJob.session_id,
                            TranscriptSegment.producer_key == transcript_key,
                        ),
                    )
                    .where(STTJob.state != STTJobState.SUCCEEDED.value)
                    .order_by(STTJob.updated_at, STTJob.utterance_id)
                    .limit(batch_size)
                    .with_for_update(of=STTJob, skip_locked=True)
                )
            ).all()
        )
        for job in jobs:
            _mark_succeeded(job)
        return len(jobs)

def _eligible_expression(current: datetime, cooldown_before: datetime):
    state_eligible = or_(
        STTJob.state == STTJobState.PENDING.value,
        and_(
            STTJob.state == STTJobState.RETRY_WAIT.value,
            STTJob.next_attempt_at.is_not(None),
            STTJob.next_attempt_at <= current,
        ),
        and_(
            STTJob.state == STTJobState.CLAIMED.value,
            STTJob.claim_expires_at.is_not(None),
            STTJob.claim_expires_at <= current,
        ),
    )
    dispatch_eligible = or_(
        STTJob.last_delivery_attempt_at.is_(None),
        STTJob.last_delivery_attempt_at <= cooldown_before,
    )
    return and_(state_eligible, dispatch_eligible)


def _outstanding_expression(current: datetime, cooldown_before: datetime):
    active_claim = and_(
        STTJob.state == STTJobState.CLAIMED.value,
        STTJob.claim_expires_at.is_not(None),
        STTJob.claim_expires_at > current,
    )
    recently_published_unclaimed = and_(
        STTJob.last_delivery_attempt_at.is_not(None),
        STTJob.last_delivery_attempt_at > cooldown_before,
        or_(
            STTJob.state == STTJobState.PENDING.value,
            and_(
                STTJob.state == STTJobState.RETRY_WAIT.value,
                STTJob.next_attempt_at.is_not(None),
                STTJob.next_attempt_at <= current,
            ),
            and_(
                STTJob.state == STTJobState.CLAIMED.value,
                STTJob.claim_expires_at.is_not(None),
                STTJob.claim_expires_at <= current,
            ),
        ),
    )
    return or_(active_claim, recently_published_unclaimed)


async def _fair_candidates(
    *,
    now: datetime,
    limit: int,
    per_session_limit: int,
    cooldown_seconds: float,
) -> list[UUID]:
    cooldown_before = now - timedelta(seconds=cooldown_seconds)
    async with get_sessionmaker()() as db:
        outstanding_expression = _outstanding_expression(now, cooldown_before)
        global_outstanding = int(
            await db.scalar(
                select(func.count(STTJob.utterance_id)).where(outstanding_expression)
            )
            or 0
        )
        available_global = max(0, limit - global_outstanding)
        if available_global == 0:
            return []

        outstanding = (
            select(
                STTJob.session_id.label("session_id"),
                func.count(STTJob.utterance_id).label("outstanding_count"),
            )
            .where(outstanding_expression)
            .group_by(STTJob.session_id)
            .subquery()
        )
        session_history = (
            select(
                STTJob.session_id.label("session_id"),
                func.max(STTJob.last_delivery_attempt_at).label("last_delivery_attempt_at"),
            )
            .group_by(STTJob.session_id)
            .subquery()
        )
        session_rank = func.row_number().over(
            partition_by=STTJob.session_id,
            order_by=(
                TranscriptionUtterance.sequence,
                STTJob.created_at,
                STTJob.utterance_id,
            ),
        )
        ranked = (
            select(
                STTJob.utterance_id.label("utterance_id"),
                session_rank.label("session_rank"),
                STTJob.session_id.label("session_id"),
            )
            .join(
                TranscriptionUtterance,
                TranscriptionUtterance.id == STTJob.utterance_id,
            )
            .where(_eligible_expression(now, cooldown_before))
            .subquery()
        )
        outstanding_count = func.coalesce(outstanding.c.outstanding_count, 0)
        rows = (
            await db.execute(
                select(ranked.c.utterance_id)
                .outerjoin(outstanding, outstanding.c.session_id == ranked.c.session_id)
                .outerjoin(
                    session_history,
                    session_history.c.session_id == ranked.c.session_id,
                )
                .where(
                    outstanding_count < per_session_limit,
                    ranked.c.session_rank <= per_session_limit - outstanding_count,
                )
                .order_by(
                    ranked.c.session_rank,
                    session_history.c.last_delivery_attempt_at.asc().nulls_first(),
                    ranked.c.session_id,
                    ranked.c.utterance_id,
                )
                .limit(available_global)
            )
        ).all()
        return [row.utterance_id for row in rows]


async def _mark_delivery_attempt(utterance_id: UUID, attempted_at: datetime) -> None:
    async with get_sessionmaker()() as db, db.begin():
        job = await db.get(STTJob, utterance_id)
        if job is not None and job.state not in {
            STTJobState.SUCCEEDED.value,
            STTJobState.FAILED.value,
        }:
            job.last_delivery_attempt_at = attempted_at


async def reconcile_stt_jobs(
    *,
    enqueue: Callable[[UUID], object],
    now: datetime | None = None,
    limit: int | None = None,
    per_session_limit: int | None = None,
    cooldown_seconds: float | None = None,
) -> STTReconcileResult:
    settings = get_settings()
    current = await _scheduler_now(now)
    batch_size = limit or settings.stt_reconcile_batch_size
    session_limit = per_session_limit or settings.stt_reconcile_per_session_limit
    if batch_size < 1:
        raise STTJobError("STT reconcile batch size must be positive")
    if session_limit < 1:
        raise STTJobError("STT per-session reconcile limit must be positive")
    cooldown = (
        float(settings.stt_dispatch_reenqueue_seconds)
        if cooldown_seconds is None
        else cooldown_seconds
    )
    if cooldown < 0:
        raise STTJobError("STT dispatch cooldown must be non-negative")

    created = await _ensure_missing_jobs(max(batch_size * 4, batch_size))
    converged = await _converge_canonical(max(batch_size * 4, batch_size))
    candidates = await _fair_candidates(
        now=current,
        limit=batch_size,
        per_session_limit=session_limit,
        cooldown_seconds=cooldown,
    )

    dispatched = 0
    enqueue_failures = 0
    for utterance_id in candidates:
        # Reserve the expiring admission hint before broker publication. A crash or publish
        # failure can delay this work only until the hint TTL expires; PostgreSQL remains truth.
        await _mark_delivery_attempt(utterance_id, current)
        try:
            enqueue(utterance_id)
        except Exception:
            enqueue_failures += 1
        else:
            dispatched += 1

    return STTReconcileResult(
        created=created,
        converged=converged,
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
    if (
        await db.scalar(select(RecordingSession.id).where(RecordingSession.id == session_id))
        is None
    ):
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

    failures = list(
        (
            await db.scalars(
                select(STTJob)
                .where(
                    STTJob.session_id == session_id,
                    STTJob.last_error_category.is_not(None),
                )
                .order_by(STTJob.updated_at.desc(), STTJob.utterance_id)
                .limit(failure_limit)
            )
        ).all()
    )
    return counts, failures
