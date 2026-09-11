import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.models import (
    RecordingSession,
    STTJob,
    STTJobState,
    TranscriptSegment,
    TranscriptionUtterance,
)
from recantor.settings import get_settings
from recantor.stt import STTErrorCategory, STTProviderError, STTRequest, STTResult
from recantor.stt_jobs import (
    STTExecutionStatus,
    _ensure_missing_jobs,
    claim_stt_job,
    execute_stt_job,
    reconcile_stt_jobs,
    requeue_failed_stt_job,
)
from recantor.transcript import commit_transcript_segment
from recantor.utterance import commit_utterance_work, transcript_producer_key_for_utterance


class FakeProvider:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.requests: list[STTRequest] = []

    async def transcribe(self, request: STTRequest) -> STTResult:
        self.calls += 1
        self.requests.append(request)
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
        return STTResult(text="race-safe transcript", language="id")


class FailIfCalledProvider:
    def __init__(self):
        self.calls = 0

    async def transcribe(self, request: STTRequest) -> STTResult:
        del request
        self.calls += 1
        raise AssertionError("provider must not be called")


def recovery_token(writer_id: str) -> str:
    return f"recantor-recovery-capability::{writer_id}::stt-jobs"


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
    start_ms: int = 100,
    end_ms: int = 1100,
    payload: bytes | None = None,
):
    async with get_sessionmaker()() as db:
        work, idempotent = await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key=producer_key,
            start_ms=start_ms,
            end_ms=end_ms,
            content_type="audio/wav",
            payload=payload or f"audio:{producer_key}".encode(),
        )
    return work, idempotent


async def load_job(utterance_id: UUID) -> STTJob:
    async with get_sessionmaker()() as db:
        job = await db.get(STTJob, utterance_id)
        assert job is not None
        db.expunge(job)
        return job


async def transcript_count(session_id: UUID, utterance_id: UUID | None = None) -> int:
    async with get_sessionmaker()() as db:
        query = select(func.count(TranscriptSegment.id)).where(
            TranscriptSegment.session_id == session_id
        )
        if utterance_id is not None:
            query = query.where(
                TranscriptSegment.producer_key
                == transcript_producer_key_for_utterance(utterance_id)
            )
        value = await db.scalar(query)
    return int(value or 0)


@pytest.mark.asyncio
async def test_durable_utterance_has_exactly_one_scheduling_identity(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-stt-job-identity-0001")
    work, first_idempotent = await create_work(
        session_id=session_id,
        producer_key="live:identity",
    )
    retry, retry_idempotent = await create_work(
        session_id=session_id,
        producer_key="live:identity",
    )

    assert first_idempotent is False
    assert retry_idempotent is True
    assert retry.id == work.id
    async with get_sessionmaker()() as db:
        count = await db.scalar(
            select(func.count(STTJob.utterance_id)).where(STTJob.utterance_id == work.id)
        )
    assert count == 1
    assert (await load_job(work.id)).state == STTJobState.PENDING.value


@pytest.mark.asyncio
async def test_database_rejects_cross_session_scheduling_identity(client: AsyncClient) -> None:
    session_a = await create_session(client, "writer-stt-job-fk-a-0001")
    session_b = await create_session(client, "writer-stt-job-fk-b-0001")
    work, _ = await create_work(session_id=session_a, producer_key="live:fk:a")

    async with get_sessionmaker()() as db:
        await db.execute(delete(STTJob).where(STTJob.utterance_id == work.id))
        await db.commit()
        db.add(
            STTJob(
                utterance_id=work.id,
                session_id=session_b,
                state=STTJobState.PENDING.value,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

    assert await _ensure_missing_jobs(100) == 1
    repaired = await load_job(work.id)
    assert repaired.session_id == session_a


@pytest.mark.asyncio
async def test_concurrent_reconciliation_materializes_one_job_identity(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-stt-job-materialize-0001")
    work, _ = await create_work(session_id=session_id, producer_key="live:materialize")
    async with get_sessionmaker()() as db:
        await db.execute(delete(STTJob).where(STTJob.utterance_id == work.id))
        await db.commit()

    created = await asyncio.gather(_ensure_missing_jobs(100), _ensure_missing_jobs(100))
    assert sum(created) == 1
    async with get_sessionmaker()() as db:
        count = await db.scalar(
            select(func.count(STTJob.utterance_id)).where(STTJob.utterance_id == work.id)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_duplicate_delivery_short_circuits_after_canonical_success(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-stt-job-duplicate-0001")
    work, _ = await create_work(session_id=session_id, producer_key="live:duplicate")
    provider = FakeProvider([STTResult(text="sekali saja", language="id")])

    first = await execute_stt_job(utterance_id=work.id, provider=provider)
    second = await execute_stt_job(utterance_id=work.id, provider=provider)

    assert first.status == STTExecutionStatus.SUCCEEDED
    assert second.status == STTExecutionStatus.NOT_CLAIMED
    assert provider.calls == 1
    assert await transcript_count(session_id, work.id) == 1


@pytest.mark.asyncio
async def test_two_workers_share_neither_claim_nor_provider_and_provider_holds_no_job_lock(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-stt-job-race-0001")
    work, _ = await create_work(session_id=session_id, producer_key="live:race")
    provider = BlockingProvider()

    first_task = asyncio.create_task(
        execute_stt_job(utterance_id=work.id, provider=provider, lease_seconds=60)
    )
    await asyncio.wait_for(provider.started.wait(), timeout=3)

    second = await execute_stt_job(
        utterance_id=work.id,
        provider=provider,
        lease_seconds=60,
    )
    assert second.status == STTExecutionStatus.NOT_CLAIMED
    assert provider.calls == 1
    assert provider.max_active == 1

    # The provider is blocked, yet another PostgreSQL transaction can NOWAIT-lock the job row.
    # This proves no scheduling transaction/row lock spans the provider network call.
    async with get_sessionmaker()() as db, db.begin():
        locked = await db.scalar(
            select(STTJob).where(STTJob.utterance_id == work.id).with_for_update(nowait=True)
        )
        assert locked is not None

    provider.release.set()
    first = await asyncio.wait_for(first_task, timeout=3)
    assert first.status == STTExecutionStatus.SUCCEEDED
    assert provider.calls == 1
    assert provider.max_active == 1

    third = await execute_stt_job(utterance_id=work.id, provider=provider)
    assert third.status == STTExecutionStatus.NOT_CLAIMED
    assert provider.calls == 1
    assert await transcript_count(session_id, work.id) == 1


@pytest.mark.asyncio
async def test_stale_claim_cannot_commit_canonical_after_reclaim(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-stt-job-stale-0001")
    work, _ = await create_work(session_id=session_id, producer_key="live:stale")
    provider = BlockingProvider()
    t0 = datetime(2026, 9, 11, 2, 0, tzinfo=UTC)

    stale_task = asyncio.create_task(
        execute_stt_job(
            utterance_id=work.id,
            provider=provider,
            now=t0,
            lease_seconds=1,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=3)

    newer = await claim_stt_job(
        utterance_id=work.id,
        now=t0 + timedelta(seconds=2),
        lease_seconds=60,
    )
    assert newer is not None
    assert newer.attempt_count == 2

    provider.release.set()
    stale = await asyncio.wait_for(stale_task, timeout=3)
    assert stale.status == STTExecutionStatus.STALE
    assert await transcript_count(session_id, work.id) == 0

    job = await load_job(work.id)
    assert job.state == STTJobState.CLAIMED.value
    assert job.claim_token == newer.token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        STTJobState.PENDING,
        STTJobState.CLAIMED,
        STTJobState.RETRY_WAIT,
        STTJobState.FAILED,
    ],
)
async def test_canonical_evidence_converges_every_unfinished_projection(
    client: AsyncClient,
    state: STTJobState,
) -> None:
    session_id = await create_session(client, f"writer-stt-job-canonical-{state.value}-0001")
    work, _ = await create_work(
        session_id=session_id,
        producer_key=f"live:canonical:{state.value}",
    )
    t0 = datetime(2026, 9, 11, 2, 5, tzinfo=UTC)

    async with get_sessionmaker()() as db, db.begin():
        job = await db.scalar(
            select(STTJob).where(STTJob.utterance_id == work.id).with_for_update()
        )
        assert job is not None
        job.state = state.value
        job.next_attempt_at = t0 + timedelta(minutes=5) if state == STTJobState.RETRY_WAIT else None
        if state == STTJobState.CLAIMED:
            job.claim_token = "synthetic-current-claim"
            job.claim_expires_at = t0 + timedelta(minutes=5)
        else:
            job.claim_token = None
            job.claim_expires_at = None
        if state == STTJobState.FAILED:
            job.last_error_category = "permanent"
            job.last_error_code = "provider_permanent"
            job.last_error_message = "old failure"

    async with get_sessionmaker()() as db:
        await commit_transcript_segment(
            db,
            session_id=session_id,
            producer_key=transcript_producer_key_for_utterance(work.id),
            start_ms=work.start_ms,
            end_ms=work.end_ms,
            text="canonical wins",
            language="id",
        )

    provider = FailIfCalledProvider()
    result = await reconcile_stt_jobs(enqueue=lambda _: None, now=t0)
    assert result.converged == 1
    assert provider.calls == 0
    job = await load_job(work.id)
    assert job.state == STTJobState.SUCCEEDED.value
    assert job.claim_token is None
    assert job.next_attempt_at is None
    assert job.last_error_category is None


@pytest.mark.asyncio
async def test_missing_job_with_canonical_is_repaired_as_success_without_dispatch(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-stt-job-canonical-missing-0001")
    work, _ = await create_work(session_id=session_id, producer_key="live:canonical:missing")
    async with get_sessionmaker()() as db:
        await commit_transcript_segment(
            db,
            session_id=session_id,
            producer_key=transcript_producer_key_for_utterance(work.id),
            start_ms=work.start_ms,
            end_ms=work.end_ms,
            text="sudah ada",
            language="id",
        )
        await db.execute(delete(STTJob).where(STTJob.utterance_id == work.id))
        await db.commit()

    dispatched: list[UUID] = []
    result = await reconcile_stt_jobs(enqueue=dispatched.append)
    assert result.created == 1
    assert result.converged == 1
    assert dispatched == []
    assert (await load_job(work.id)).state == STTJobState.SUCCEEDED.value


@pytest.mark.asyncio
async def test_enqueue_failure_cannot_undo_durable_work(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-stt-job-enqueue-0001")
    work, _ = await create_work(session_id=session_id, producer_key="live:enqueue")

    def fail_enqueue(utterance_id: UUID) -> None:
        del utterance_id
        raise OSError("redis unavailable")

    result = await reconcile_stt_jobs(
        enqueue=fail_enqueue,
        now=datetime(2026, 9, 11, 2, 10, tzinfo=UTC),
    )
    assert result.enqueue_failures == 1

    async with get_sessionmaker()() as db:
        durable_work = await db.get(TranscriptionUtterance, work.id)
        session = await db.get(RecordingSession, session_id)
    assert durable_work is not None
    assert session is not None
    assert session.state == "recording"
    assert (await load_job(work.id)).state == STTJobState.PENDING.value


@pytest.mark.asyncio
async def test_missing_job_and_lost_delivery_are_rediscovered(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-stt-job-reconcile-0001")
    work, _ = await create_work(session_id=session_id, producer_key="live:reconcile")

    async with get_sessionmaker()() as db:
        await db.execute(delete(STTJob).where(STTJob.utterance_id == work.id))
        await db.commit()

    t0 = datetime(2026, 9, 11, 2, 20, tzinfo=UTC)
    deliveries: list[UUID] = []
    first = await reconcile_stt_jobs(
        enqueue=deliveries.append,
        now=t0,
        cooldown_seconds=10,
    )
    assert first.created == 1
    assert deliveries == [work.id]

    # Simulate a lost Celery message: no worker claims it. PostgreSQL makes it eligible again.
    second = await reconcile_stt_jobs(
        enqueue=deliveries.append,
        now=t0 + timedelta(seconds=11),
        cooldown_seconds=10,
    )
    assert second.dispatched == 1
    assert deliveries == [work.id, work.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category",
    [
        STTErrorCategory.TRANSIENT,
        STTErrorCategory.TIMEOUT,
        STTErrorCategory.RATE_LIMIT,
    ],
)
async def test_retryable_provider_failures_back_off_cap_and_stop_at_attempt_budget(
    client: AsyncClient,
    monkeypatch,
    category: STTErrorCategory,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "stt_max_attempts", 3)
    monkeypatch.setattr(settings, "stt_retry_base_seconds", 2.0)
    monkeypatch.setattr(settings, "stt_retry_max_seconds", 3.0)

    session_id = await create_session(client, f"writer-stt-job-retry-{category.value}-0001")
    work, _ = await create_work(
        session_id=session_id,
        producer_key=f"live:retry:{category.value}",
    )
    provider = FakeProvider(
        [
            STTProviderError(category, "SECRET upstream detail 1"),
            STTProviderError(category, "SECRET upstream detail 2"),
            STTProviderError(category, "SECRET upstream detail 3"),
        ]
    )
    t0 = datetime(2026, 9, 11, 3, 0, tzinfo=UTC)

    first = await execute_stt_job(utterance_id=work.id, provider=provider, now=t0)
    assert first.status == STTExecutionStatus.RETRY_SCHEDULED
    assert first.next_attempt_at == t0 + timedelta(seconds=2)

    early = await execute_stt_job(
        utterance_id=work.id,
        provider=provider,
        now=t0 + timedelta(seconds=1),
    )
    assert early.status == STTExecutionStatus.NOT_CLAIMED
    assert provider.calls == 1

    second = await execute_stt_job(
        utterance_id=work.id,
        provider=provider,
        now=first.next_attempt_at,
    )
    assert second.status == STTExecutionStatus.RETRY_SCHEDULED
    assert second.next_attempt_at == first.next_attempt_at + timedelta(seconds=3)

    third = await execute_stt_job(
        utterance_id=work.id,
        provider=provider,
        now=second.next_attempt_at,
    )
    assert third.status == STTExecutionStatus.FAILED
    assert provider.calls == 3

    after_budget = await execute_stt_job(
        utterance_id=work.id,
        provider=provider,
        now=second.next_attempt_at + timedelta(hours=1),
    )
    assert after_budget.status == STTExecutionStatus.NOT_CLAIMED
    assert provider.calls == 3
    job = await load_job(work.id)
    assert job.attempt_count == 3
    assert job.state == STTJobState.FAILED.value
    assert "SECRET" not in (job.last_error_message or "")


@pytest.mark.asyncio
async def test_configuration_is_delayed_and_terminal_categories_do_not_hot_loop(
    client: AsyncClient,
    monkeypatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "stt_max_attempts", 3)
    monkeypatch.setattr(settings, "stt_configuration_retry_seconds", 300.0)

    session_id = await create_session(client, "writer-stt-job-policy-0001")
    config_work, _ = await create_work(session_id=session_id, producer_key="live:config")
    t0 = datetime(2026, 9, 11, 3, 30, tzinfo=UTC)
    config_provider = FakeProvider(
        [
            STTProviderError(STTErrorCategory.CONFIGURATION, "key missing"),
            STTProviderError(STTErrorCategory.CONFIGURATION, "key missing"),
            STTProviderError(STTErrorCategory.CONFIGURATION, "key missing"),
        ]
    )
    config = await execute_stt_job(
        utterance_id=config_work.id,
        provider=config_provider,
        now=t0,
    )
    assert config.status == STTExecutionStatus.RETRY_SCHEDULED
    assert config.next_attempt_at == t0 + timedelta(seconds=300)
    immediate = await execute_stt_job(
        utterance_id=config_work.id,
        provider=config_provider,
        now=t0 + timedelta(seconds=1),
    )
    assert immediate.status == STTExecutionStatus.NOT_CLAIMED
    assert config_provider.calls == 1

    for index, category in enumerate(
        [STTErrorCategory.PERMANENT, STTErrorCategory.MALFORMED_RESPONSE],
        start=1,
    ):
        work, _ = await create_work(
            session_id=session_id,
            producer_key=f"live:terminal:{index}",
            start_ms=2000 * index,
            end_ms=2000 * index + 1000,
        )
        provider = FakeProvider([STTProviderError(category, "terminal")])
        result = await execute_stt_job(utterance_id=work.id, provider=provider, now=t0)
        assert result.status == STTExecutionStatus.FAILED
        repeated = await execute_stt_job(
            utterance_id=work.id,
            provider=provider,
            now=t0 + timedelta(days=1),
        )
        assert repeated.status == STTExecutionStatus.NOT_CLAIMED
        assert provider.calls == 1
        job = await load_job(work.id)
        assert job.state == STTJobState.FAILED.value
        assert job.last_error_category == category.value


@pytest.mark.asyncio
async def test_successful_retry_commits_once_and_clears_retry_error(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-stt-job-success-retry-0001")
    work, _ = await create_work(session_id=session_id, producer_key="live:success-retry")
    provider = FakeProvider(
        [
            STTProviderError(STTErrorCategory.TRANSIENT, "temporary"),
            STTResult(text="akhirnya berhasil", language="id"),
        ]
    )
    t0 = datetime(2026, 9, 11, 4, 0, tzinfo=UTC)

    first = await execute_stt_job(utterance_id=work.id, provider=provider, now=t0)
    assert first.next_attempt_at is not None
    second = await execute_stt_job(
        utterance_id=work.id,
        provider=provider,
        now=first.next_attempt_at,
    )
    assert second.status == STTExecutionStatus.SUCCEEDED

    job = await load_job(work.id)
    assert job.state == STTJobState.SUCCEEDED.value
    assert job.next_attempt_at is None
    assert job.last_error_category is None
    assert job.last_error_message is None
    assert await transcript_count(session_id, work.id) == 1

    duplicate = await execute_stt_job(utterance_id=work.id, provider=provider)
    assert duplicate.status == STTExecutionStatus.NOT_CLAIMED
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_reconciler_bounds_admission_and_serves_late_session_before_large_backlog_drains(
    client: AsyncClient,
) -> None:
    session_a = await create_session(client, "writer-stt-job-fair-a-0001")
    a_ids: list[UUID] = []
    for index in range(8):
        work, _ = await create_work(
            session_id=session_a,
            producer_key=f"live:fair:a:{index}",
            start_ms=index * 2000,
            end_ms=index * 2000 + 1000,
        )
        a_ids.append(work.id)

    t0 = datetime(2026, 9, 11, 4, 10, tzinfo=UTC)
    first_dispatch: list[UUID] = []
    first = await reconcile_stt_jobs(
        enqueue=first_dispatch.append,
        now=t0,
        limit=100,
        per_session_limit=2,
        cooldown_seconds=60,
    )
    assert first.dispatched == 2
    assert set(first_dispatch).issubset(set(a_ids))

    session_b = await create_session(client, "writer-stt-job-fair-b-0001")
    b_work, _ = await create_work(session_id=session_b, producer_key="live:fair:b:0")

    second_dispatch: list[UUID] = []
    second = await reconcile_stt_jobs(
        enqueue=second_dispatch.append,
        now=t0 + timedelta(seconds=1),
        limit=100,
        per_session_limit=2,
        cooldown_seconds=60,
    )
    assert second.dispatched == 3
    assert b_work.id in second_dispatch
    assert len(set(second_dispatch).intersection(a_ids)) == 2

    b_provider = FakeProvider([STTResult(text="sesi B dilayani", language="id")])
    b_result = await execute_stt_job(utterance_id=b_work.id, provider=b_provider)
    assert b_result.status == STTExecutionStatus.SUCCEEDED
    assert b_provider.calls == 1

    async with get_sessionmaker()() as db:
        remaining_a = await db.scalar(
            select(func.count(STTJob.utterance_id)).where(
                STTJob.session_id == session_a,
                STTJob.state == STTJobState.PENDING.value,
            )
        )
    assert remaining_a and remaining_a > 0


@pytest.mark.asyncio
async def test_concurrent_sessions_remain_isolated(client: AsyncClient) -> None:
    session_a = await create_session(client, "writer-stt-job-isolate-a-0001")
    session_b = await create_session(client, "writer-stt-job-isolate-b-0001")
    work_a, _ = await create_work(session_id=session_a, producer_key="live:isolate:a")
    work_b, _ = await create_work(session_id=session_b, producer_key="live:isolate:b")
    provider_a = FakeProvider([STTResult(text="sesi A", language="id")])
    provider_b = FakeProvider([STTResult(text="sesi B", language="id")])

    result_a, result_b = await asyncio.gather(
        execute_stt_job(utterance_id=work_a.id, provider=provider_a),
        execute_stt_job(utterance_id=work_b.id, provider=provider_b),
    )
    assert result_a.status == STTExecutionStatus.SUCCEEDED
    assert result_b.status == STTExecutionStatus.SUCCEEDED

    async with get_sessionmaker()() as db:
        texts_a = list(
            (
                await db.scalars(
                    select(TranscriptSegment.text).where(TranscriptSegment.session_id == session_a)
                )
            ).all()
        )
        texts_b = list(
            (
                await db.scalars(
                    select(TranscriptSegment.text).where(TranscriptSegment.session_id == session_b)
                )
            ).all()
        )
    assert texts_a == ["sesi A"]
    assert texts_b == ["sesi B"]


@pytest.mark.asyncio
async def test_failed_work_is_inspectable_replayable_and_diagnostics_are_secret_safe(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-stt-job-diagnostics-0001")
    work, _ = await create_work(session_id=session_id, producer_key="live:diagnostics")
    sentinel = "SECRET_GROQ_KEY Authorization: Bearer secret /absolute/audio/path"
    provider = FakeProvider([STTProviderError(STTErrorCategory.PERMANENT, sentinel)])
    failed = await execute_stt_job(utterance_id=work.id, provider=provider)
    assert failed.status == STTExecutionStatus.FAILED

    response = await client.get(f"/api/v1/sessions/{session_id}/stt-scheduling")
    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"]["failed"] == 1
    assert payload["recent_failures"][0]["utterance_id"] == str(work.id)
    assert payload["recent_failures"][0]["last_error_category"] == "permanent"
    assert "storage_key" not in response.text
    assert work.storage_key not in response.text
    assert sentinel not in response.text
    assert "Authorization" not in response.text
    assert "Bearer secret" not in response.text

    async with get_sessionmaker()() as db:
        replay = await requeue_failed_stt_job(
            db,
            session_id=session_id,
            utterance_id=work.id,
        )
    assert replay.state == STTJobState.PENDING.value
    assert replay.attempt_count == 0
