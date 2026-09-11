import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.models import STTJob, STTJobState, TranscriptSegment
from recantor.settings import Settings
from recantor.stt import STTErrorCategory, STTProviderError, STTRequest, STTResult
from recantor.stt_jobs import (
    STTExecutionStatus,
    claim_stt_job,
    execute_next_reserved_stt_job,
    execute_stt_job,
    reconcile_stt_jobs,
)
from recantor.transcript import commit_transcript_segment
from recantor.utterance import commit_utterance_work, transcript_producer_key_for_utterance


class SequenceProvider:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def transcribe(self, request: STTRequest) -> STTResult:
        del request
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class BlockingProvider:
    def __init__(self):
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, request: STTRequest) -> STTResult:
        del request
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return STTResult(text="bounded provider call", language="id")


class FailIfCalledProvider:
    def __init__(self):
        self.calls = 0

    async def transcribe(self, request: STTRequest) -> STTResult:
        del request
        self.calls += 1
        raise AssertionError("provider must not be called when canonical evidence already exists")


class FakeWakeBroker:
    """Deterministic zero-consumer broker model for generic coalesced wakes."""

    def __init__(self):
        self.queued = 0
        self.published = 0
        self.max_queued = 0
        self.fail_next = False

    def ensure_capacity(self, target: int) -> int:
        if self.fail_next:
            self.fail_next = False
            raise OSError("redis unavailable")
        missing = max(0, target - self.queued)
        self.queued += missing
        self.published += missing
        self.max_queued = max(self.max_queued, self.queued)
        return missing

    def flush(self) -> None:
        self.queued = 0

    def pop(self) -> None:
        assert self.queued > 0
        self.queued -= 1


def recovery_token(writer_id: str) -> str:
    return f"recantor-recovery-capability::{writer_id}::pass2"


@pytest_asyncio.fixture
async def client(clean_recording_state) -> AsyncClient:
    del clean_recording_state
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


async def create_session(client: AsyncClient, writer_id: str) -> UUID:
    response = await client.post(
        "/api/v1/sessions/live",
        json={
            "client_request_id": str(uuid4()),
            "writer_id": writer_id,
            "recovery_token": recovery_token(writer_id),
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def create_work(
    *,
    session_id: UUID,
    producer_key: str,
    start_ms: int,
):
    async with get_sessionmaker()() as db:
        work, _ = await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key=producer_key,
            start_ms=start_ms,
            end_ms=start_ms + 900,
            content_type="audio/wav",
            payload=f"audio:{producer_key}".encode(),
        )
    return work


async def load_job(utterance_id: UUID) -> STTJob:
    async with get_sessionmaker()() as db:
        job = await db.get(STTJob, utterance_id)
        assert job is not None
        db.expunge(job)
        return job


async def canonical_count(session_id: UUID, utterance_id: UUID) -> int:
    async with get_sessionmaker()() as db:
        value = await db.scalar(
            select(func.count(TranscriptSegment.id)).where(
                TranscriptSegment.session_id == session_id,
                TranscriptSegment.producer_key
                == transcript_producer_key_for_utterance(utterance_id),
            )
        )
    return int(value or 0)


@pytest.mark.asyncio
async def test_generic_wakes_stay_bounded_across_full_cooldown_windows_and_serve_late_session(
    client: AsyncClient,
) -> None:
    session_a = await create_session(client, "writer-pass2-fair-a-0001")
    a_works = [
        await create_work(
            session_id=session_a,
            producer_key=f"pass2:fair:a:{index}",
            start_ms=index * 1000,
        )
        for index in range(8)
    ]
    t0 = datetime(2026, 9, 11, 3, 40, tzinfo=UTC)
    broker = FakeWakeBroker()

    # Zero consumers for more than three complete reservation/cooldown windows. PostgreSQL may
    # rotate/refresh work, but Redis stores only generic wake capacity rather than one task copy
    # per durable utterance per window.
    for seconds in (0, 11, 22, 33):
        result = await reconcile_stt_jobs(
            ensure_wake_capacity=broker.ensure_capacity,
            now=t0 + timedelta(seconds=seconds),
            limit=2,
            per_session_limit=1,
            cooldown_seconds=10,
        )
        assert result.wake_target == 1
        assert broker.queued == 1
        assert broker.max_queued == 1

    session_b = await create_session(client, "writer-pass2-fair-b-0001")
    b_work = await create_work(
        session_id=session_b,
        producer_key="pass2:fair:b:0",
        start_ms=0,
    )
    current = t0 + timedelta(seconds=44)
    result = await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=current,
        limit=2,
        per_session_limit=1,
        cooldown_seconds=10,
    )
    assert result.wake_target == 2
    assert broker.queued == 2
    assert broker.max_queued == 2

    provider = SequenceProvider(
        [
            STTResult(text="frontier one", language="id"),
            STTResult(text="frontier two", language="id"),
        ]
    )
    for _ in range(2):
        broker.pop()
        execution = await execute_next_reserved_stt_job(
            provider=provider,
            now=current,
            cooldown_seconds=10,
        )
        assert execution.status == STTExecutionStatus.SUCCEEDED

    assert provider.calls == 2
    assert (await load_job(b_work.id)).state == STTJobState.SUCCEEDED.value
    remaining_a = [
        work for work in a_works if (await load_job(work.id)).state != STTJobState.SUCCEEDED.value
    ]
    assert remaining_a


@pytest.mark.asyncio
async def test_stale_generic_wakes_cannot_raise_provider_concurrency_above_current_cap(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-pass2-stale-wake-cap-0001")
    for index in range(3):
        await create_work(
            session_id=session_id,
            producer_key=f"pass2:stale-wake:{index}",
            start_ms=index * 1000,
        )

    t0 = datetime(2026, 9, 11, 3, 45, tzinfo=UTC)
    broker = FakeWakeBroker()
    await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=t0,
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert broker.queued == 1

    # Pretend three generic messages survived from broker history. They carry no work identity.
    broker.queued = 3
    provider = BlockingProvider()
    tasks = [
        asyncio.create_task(
            execute_next_reserved_stt_job(
                provider=provider,
                now=t0,
                cooldown_seconds=30,
            )
        )
        for _ in range(3)
    ]
    await asyncio.wait_for(provider.started.wait(), timeout=3)
    await asyncio.sleep(0.05)
    assert provider.calls == 1
    assert provider.max_active == 1

    provider.release.set()
    results = await asyncio.gather(*tasks)
    assert sum(result.status == STTExecutionStatus.SUCCEEDED for result in results) == 1
    assert sum(result.status == STTExecutionStatus.NOT_CLAIMED for result in results) == 2


@pytest.mark.asyncio
async def test_two_concurrent_reconcilers_share_serialized_global_and_session_capacity(
    client: AsyncClient,
) -> None:
    works = []
    for session_index in range(4):
        session_id = await create_session(client, f"writer-pass2-reconcile-{session_index:02d}")
        works.append(
            await create_work(
                session_id=session_id,
                producer_key=f"pass2:reconcile:{session_index}",
                start_ms=0,
            )
        )

    t0 = datetime(2026, 9, 11, 3, 50, tzinfo=UTC)
    broker = FakeWakeBroker()
    gate = asyncio.Event()

    async def contender():
        await gate.wait()
        return await reconcile_stt_jobs(
            ensure_wake_capacity=broker.ensure_capacity,
            now=t0,
            limit=2,
            per_session_limit=1,
            cooldown_seconds=30,
        )

    tasks = [asyncio.create_task(contender()) for _ in range(2)]
    await asyncio.sleep(0)
    gate.set()
    first, second = await asyncio.gather(*tasks)

    jobs = [await load_job(work.id) for work in works]
    current_reservations = [job for job in jobs if job.last_delivery_attempt_at == t0]
    assert len(current_reservations) == 2
    assert len({job.session_id for job in current_reservations}) == 2
    assert first.reserved + second.reserved == 2
    assert broker.queued == 2
    assert broker.max_queued == 2


@pytest.mark.asyncio
async def test_older_reconciler_timestamp_cannot_regress_newer_reservation(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-pass2-no-regress-0001")
    work = await create_work(
        session_id=session_id,
        producer_key="pass2:no-regress",
        start_ms=0,
    )
    newer = datetime(2026, 9, 11, 4, 0, 5, tzinfo=UTC)
    older = newer - timedelta(seconds=5)

    await reconcile_stt_jobs(
        enqueue=lambda _: None,
        now=newer,
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert (await load_job(work.id)).last_delivery_attempt_at == newer

    await reconcile_stt_jobs(
        enqueue=lambda _: None,
        now=older,
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert (await load_job(work.id)).last_delivery_attempt_at == newer


@pytest.mark.asyncio
async def test_false_succeeded_without_canonical_repairs_and_runs_automatically(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-pass2-false-success-0001")
    work = await create_work(
        session_id=session_id,
        producer_key="pass2:false-success",
        start_ms=0,
    )
    t0 = datetime(2026, 9, 11, 4, 5, tzinfo=UTC)
    async with get_sessionmaker()() as db, db.begin():
        job = await db.get(STTJob, work.id)
        assert job is not None
        job.state = STTJobState.SUCCEEDED.value
        job.attempt_count = 5
        job.last_delivery_attempt_at = t0 - timedelta(minutes=1)

    broker = FakeWakeBroker()
    result = await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=t0,
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert result.converged >= 1
    repaired = await load_job(work.id)
    assert repaired.state == STTJobState.PENDING.value
    assert repaired.attempt_count == 0
    assert repaired.last_delivery_attempt_at == t0
    assert broker.queued == 1

    broker.pop()
    provider = SequenceProvider([STTResult(text="repaired transcript", language="id")])
    execution = await execute_next_reserved_stt_job(
        provider=provider,
        now=t0,
        cooldown_seconds=30,
    )
    assert execution.status == STTExecutionStatus.SUCCEEDED
    assert provider.calls == 1
    assert await canonical_count(session_id, work.id) == 1


@pytest.mark.asyncio
async def test_direct_task_entry_repairs_false_succeeded_projection(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-pass2-false-success-direct-0001")
    work = await create_work(
        session_id=session_id,
        producer_key="pass2:false-success-direct",
        start_ms=0,
    )
    async with get_sessionmaker()() as db, db.begin():
        job = await db.get(STTJob, work.id)
        assert job is not None
        job.state = STTJobState.SUCCEEDED.value
        job.attempt_count = 4

    provider = SequenceProvider([STTResult(text="direct repair", language="id")])
    result = await execute_stt_job(utterance_id=work.id, provider=provider)
    assert result.status == STTExecutionStatus.SUCCEEDED
    assert provider.calls == 1
    assert await canonical_count(session_id, work.id) == 1


@pytest.mark.asyncio
async def test_total_redis_loss_replenishes_existing_postgres_reservation_without_waiting_cooldown(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-pass2-redis-loss-0001")
    work = await create_work(
        session_id=session_id,
        producer_key="pass2:redis-loss",
        start_ms=0,
    )
    t0 = datetime(2026, 9, 11, 4, 10, tzinfo=UTC)
    broker = FakeWakeBroker()

    first = await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=t0,
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert first.reserved == 1
    assert first.dispatched == 1
    assert broker.queued == 1
    first_stamp = (await load_job(work.id)).last_delivery_attempt_at

    broker.flush()
    second = await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=t0 + timedelta(seconds=1),
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert second.reserved == 0
    assert second.wake_target == 1
    assert second.dispatched == 1
    assert broker.queued == 1
    assert (await load_job(work.id)).last_delivery_attempt_at == first_stamp


@pytest.mark.asyncio
async def test_publish_failure_retries_same_reservation_on_next_pass_without_cooldown_delay(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-pass2-publish-retry-0001")
    await create_work(
        session_id=session_id,
        producer_key="pass2:publish-retry",
        start_ms=0,
    )
    t0 = datetime(2026, 9, 11, 4, 12, tzinfo=UTC)
    broker = FakeWakeBroker()
    broker.fail_next = True

    first = await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=t0,
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert first.reserved == 1
    assert first.enqueue_failures == 1
    assert broker.queued == 0

    second = await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=t0 + timedelta(seconds=1),
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert second.reserved == 0
    assert second.dispatched == 1
    assert broker.queued == 1


@pytest.mark.asyncio
async def test_consumed_transient_failure_reenters_at_next_attempt_before_delivery_cooldown(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-pass2-retry-reservation-0001")
    await create_work(
        session_id=session_id,
        producer_key="pass2:retry-reservation",
        start_ms=0,
    )
    t0 = datetime(2026, 9, 11, 4, 15, tzinfo=UTC)
    broker = FakeWakeBroker()
    await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=t0,
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    broker.pop()

    provider = SequenceProvider(
        [STTProviderError(STTErrorCategory.TRANSIENT, "temporary provider issue")]
    )
    execution = await execute_next_reserved_stt_job(
        provider=provider,
        now=t0,
        cooldown_seconds=30,
    )
    assert execution.status == STTExecutionStatus.RETRY_SCHEDULED
    assert execution.next_attempt_at is not None
    assert execution.next_attempt_at < t0 + timedelta(seconds=30)

    retry_pass = await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=execution.next_attempt_at,
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert retry_pass.reserved == 1
    assert retry_pass.dispatched == 1
    assert broker.queued == 1


@pytest.mark.asyncio
async def test_continuous_new_session_churn_cannot_starve_already_active_session(
    client: AsyncClient,
) -> None:
    session_a = await create_session(client, "writer-pass2-churn-a-0001")
    a1 = await create_work(session_id=session_a, producer_key="pass2:churn:a:1", start_ms=0)
    a2 = await create_work(session_id=session_a, producer_key="pass2:churn:a:2", start_ms=1000)
    t0 = datetime(2026, 9, 11, 4, 20, tzinfo=UTC)

    async with get_sessionmaker()() as db, db.begin():
        for work in (a1, a2):
            job = await db.get(STTJob, work.id)
            assert job is not None
            job.created_at = t0 - timedelta(minutes=10)

    broker = FakeWakeBroker()
    await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=t0,
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    broker.pop()
    provider = SequenceProvider([STTResult(text="a first", language="id")])
    result = await execute_next_reserved_stt_job(
        provider=provider,
        now=t0,
        cooldown_seconds=30,
    )
    assert result.status == STTExecutionStatus.SUCCEEDED

    newcomer = await create_session(client, "writer-pass2-churn-new-0001")
    newcomer_work = await create_work(
        session_id=newcomer,
        producer_key="pass2:churn:new:1",
        start_ms=0,
    )
    async with get_sessionmaker()() as db, db.begin():
        job = await db.get(STTJob, newcomer_work.id)
        assert job is not None
        job.created_at = t0 + timedelta(seconds=1)

    # A's last service turn (t0) is older than the never-served newcomer's arrival (t0+1),
    # so A gets a bounded follow-up turn rather than being starved by endless NULL-first churn.
    await reconcile_stt_jobs(
        ensure_wake_capacity=broker.ensure_capacity,
        now=t0 + timedelta(seconds=2),
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    reserved_a2 = await load_job(a2.id)
    reserved_new = await load_job(newcomer_work.id)
    assert reserved_a2.last_delivery_attempt_at == t0 + timedelta(seconds=2)
    assert reserved_new.last_delivery_attempt_at is None


@pytest.mark.asyncio
async def test_session_selection_rotates_when_active_sessions_exceed_global_batch(
    client: AsyncClient,
) -> None:
    sessions: list[UUID] = []
    work_to_session: dict[UUID, UUID] = {}
    for session_index in range(4):
        session_id = await create_session(client, f"writer-pass2-rotate-{session_index:02d}")
        sessions.append(session_id)
        for work_index in range(2):
            work = await create_work(
                session_id=session_id,
                producer_key=f"pass2:rotate:{session_index}:{work_index}",
                start_ms=work_index * 1000,
            )
            work_to_session[work.id] = session_id

    t0 = datetime(2026, 9, 11, 12, 25, tzinfo=UTC)
    first_batch: list[UUID] = []
    await reconcile_stt_jobs(
        enqueue=first_batch.append,
        now=t0,
        limit=2,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert len(first_batch) == 2
    first_sessions = {work_to_session[work_id] for work_id in first_batch}
    assert len(first_sessions) == 2

    provider = SequenceProvider(
        [STTResult(text="one", language="id"), STTResult(text="two", language="id")]
    )
    for work_id in first_batch:
        execution = await execute_stt_job(utterance_id=work_id, provider=provider)
        assert execution.status == STTExecutionStatus.SUCCEEDED

    second_batch: list[UUID] = []
    await reconcile_stt_jobs(
        enqueue=second_batch.append,
        now=t0 + timedelta(seconds=1),
        limit=2,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert len(second_batch) == 2
    second_sessions = {work_to_session[work_id] for work_id in second_batch}
    assert len(second_sessions) == 2
    assert first_sessions.isdisjoint(second_sessions)
    assert first_sessions | second_sessions == set(sessions)


@pytest.mark.asyncio
async def test_canonical_convergence_filters_before_batch_limit(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-pass2-converge-0001")
    works = [
        await create_work(
            session_id=session_id,
            producer_key=f"pass2:converge:{index}",
            start_ms=index * 1000,
        )
        for index in range(6)
    ]
    t0 = datetime(2026, 9, 11, 4, 30, tzinfo=UTC)
    async with get_sessionmaker()() as db, db.begin():
        for index, work in enumerate(works):
            job = await db.get(STTJob, work.id)
            assert job is not None
            job.state = STTJobState.FAILED.value
            job.updated_at = t0 + timedelta(seconds=index)
            job.last_error_category = "permanent"
            job.last_error_code = "prefix"
            job.last_error_message = "noncanonical terminal prefix"

    target = works[-1]
    async with get_sessionmaker()() as db:
        await commit_transcript_segment(
            db,
            session_id=session_id,
            producer_key=transcript_producer_key_for_utterance(target.id),
            start_ms=target.start_ms,
            end_ms=target.end_ms,
            text="authoritative recovered transcript",
            language="id",
        )

    result = await reconcile_stt_jobs(
        enqueue=lambda _: None,
        now=t0 + timedelta(minutes=1),
        limit=1,
        per_session_limit=1,
        cooldown_seconds=30,
    )
    assert result.converged == 1
    assert (await load_job(target.id)).state == STTJobState.SUCCEEDED.value
    for work in works[:-1]:
        assert (await load_job(work.id)).state == STTJobState.FAILED.value


@pytest.mark.asyncio
async def test_two_claimers_released_together_have_one_winner(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-pass2-claim-race-0001")
    work = await create_work(
        session_id=session_id,
        producer_key="pass2:claim-race",
        start_ms=0,
    )
    gate = asyncio.Event()

    async def contender():
        await gate.wait()
        return await claim_stt_job(utterance_id=work.id, lease_seconds=60)

    first_task = asyncio.create_task(contender())
    second_task = asyncio.create_task(contender())
    await asyncio.sleep(0)
    gate.set()
    claims = await asyncio.gather(first_task, second_task)

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    job = await load_job(work.id)
    assert job.state == STTJobState.CLAIMED.value
    assert job.attempt_count == 1
    assert job.claim_token == winners[0].token


@pytest.mark.asyncio
async def test_task_entry_with_preexisting_canonical_skips_provider(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-pass2-canonical-task-0001")
    work = await create_work(
        session_id=session_id,
        producer_key="pass2:canonical-task",
        start_ms=0,
    )
    async with get_sessionmaker()() as db, db.begin():
        job = await db.get(STTJob, work.id)
        assert job is not None
        job.state = STTJobState.FAILED.value
        job.last_error_category = "permanent"
        job.last_error_code = "old_failure"
        job.last_error_message = "old projection"
    async with get_sessionmaker()() as db:
        await commit_transcript_segment(
            db,
            session_id=session_id,
            producer_key=transcript_producer_key_for_utterance(work.id),
            start_ms=work.start_ms,
            end_ms=work.end_ms,
            text="canonical already committed",
            language="id",
        )

    provider = FailIfCalledProvider()
    result = await execute_stt_job(utterance_id=work.id, provider=provider)
    assert result.status == STTExecutionStatus.NOT_CLAIMED
    assert result.state == STTJobState.SUCCEEDED
    assert provider.calls == 0
    assert (await load_job(work.id)).state == STTJobState.SUCCEEDED.value


@pytest.mark.parametrize(
    "overrides",
    [
        {"stt_max_attempts": 0},
        {"stt_max_attempts": 21},
        {"stt_retry_base_seconds": 0},
        {"stt_retry_base_seconds": 301},
        {"stt_retry_max_seconds": 0},
        {"stt_retry_max_seconds": 3601},
        {"stt_configuration_retry_seconds": 0},
        {"stt_configuration_retry_seconds": 86401},
        {"stt_reconcile_interval_seconds": 0},
        {"stt_reconcile_interval_seconds": 61},
        {"stt_reconcile_batch_size": 0},
        {"stt_reconcile_batch_size": 257},
        {"stt_reconcile_per_session_limit": 0},
        {"stt_reconcile_per_session_limit": 33},
        {"stt_dispatch_reenqueue_seconds": 0},
        {"stt_dispatch_reenqueue_seconds": 3601},
        {"stt_queue_name": "   "},
        {"stt_queue_name": "invalid queue name"},
        {"stt_retry_base_seconds": 10, "stt_retry_max_seconds": 5},
    ],
)
def test_scheduler_settings_reject_pathological_values(overrides) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_scheduler_queue_name_is_normalized() -> None:
    settings = Settings(_env_file=None, stt_queue_name="  stt-priority  ")
    assert settings.stt_queue_name == "stt-priority"
