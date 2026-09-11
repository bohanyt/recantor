import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.models import STTJob, STTJobState
from recantor.settings import Settings
from recantor.stt import STTRequest, STTResult
from recantor.stt_jobs import (
    STTExecutionStatus,
    claim_stt_job,
    execute_stt_job,
    reconcile_stt_jobs,
)
from recantor.transcript import commit_transcript_segment
from recantor.utterance import commit_utterance_work, transcript_producer_key_for_utterance


class SequenceProvider:
    def __init__(self, count: int):
        self.results = [
            STTResult(text=f"pass2 transcript {index}", language="id") for index in range(count)
        ]
        self.calls = 0

    async def transcribe(self, request: STTRequest) -> STTResult:
        del request
        self.calls += 1
        return self.results.pop(0)


class FailIfCalledProvider:
    def __init__(self):
        self.calls = 0

    async def transcribe(self, request: STTRequest) -> STTResult:
        del request
        self.calls += 1
        raise AssertionError("provider must not be called when canonical evidence already exists")


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


@pytest.mark.asyncio
async def test_cross_pass_admission_bounds_broker_prefix_and_late_session_reaches_frontier(
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
    broker_fifo: list[UUID] = []

    for step in range(10):
        await reconcile_stt_jobs(
            enqueue=broker_fifo.append,
            now=t0 + timedelta(seconds=step),
            limit=4,
            per_session_limit=2,
            cooldown_seconds=30,
        )

    a_ids = {work.id for work in a_works}
    assert [work_id for work_id in broker_fifo if work_id in a_ids] == broker_fifo
    assert len(broker_fifo) == 2

    session_b = await create_session(client, "writer-pass2-fair-b-0001")
    b_work = await create_work(
        session_id=session_b,
        producer_key="pass2:fair:b:0",
        start_ms=0,
    )
    result = await reconcile_stt_jobs(
        enqueue=broker_fifo.append,
        now=t0 + timedelta(seconds=10),
        limit=4,
        per_session_limit=2,
        cooldown_seconds=30,
    )
    assert result.dispatched == 1
    assert broker_fifo == [broker_fifo[0], broker_fifo[1], b_work.id]

    provider = SequenceProvider(len(broker_fifo))
    for work_id in broker_fifo:
        execution = await execute_stt_job(utterance_id=work_id, provider=provider)
        assert execution.status == STTExecutionStatus.SUCCEEDED

    assert provider.calls == 3
    assert (await load_job(b_work.id)).state == STTJobState.SUCCEEDED.value
    remaining_a = [
        work for work in a_works if (await load_job(work.id)).state != STTJobState.SUCCEEDED.value
    ]
    assert len(remaining_a) == 6


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

    t0 = datetime(2026, 9, 11, 3, 50, tzinfo=UTC)
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

    provider = SequenceProvider(2)
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
    t0 = datetime(2026, 9, 11, 4, 0, tzinfo=UTC)
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
        {"stt_retry_base_seconds": 0},
        {"stt_retry_max_seconds": 0},
        {"stt_configuration_retry_seconds": 0},
        {"stt_reconcile_interval_seconds": 0},
        {"stt_reconcile_batch_size": 0},
        {"stt_reconcile_per_session_limit": 0},
        {"stt_dispatch_reenqueue_seconds": 0},
        {"stt_queue_name": "   "},
        {"stt_retry_base_seconds": 10, "stt_retry_max_seconds": 5},
    ],
)
def test_scheduler_settings_reject_pathological_values(overrides) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_scheduler_queue_name_is_normalized() -> None:
    settings = Settings(_env_file=None, stt_queue_name="  stt-priority  ")
    assert settings.stt_queue_name == "stt-priority"
