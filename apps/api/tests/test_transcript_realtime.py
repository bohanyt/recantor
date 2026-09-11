import asyncio
import json
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.settings import get_settings
from recantor.transcript import commit_transcript_segment
from recantor.transcript_realtime import transcript_channel


def recovery_token(writer_id: str) -> str:
    return f"recantor-recovery-capability::{writer_id}::transcript-realtime"


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


async def commit_segment(session_id: UUID, key: str, start_ms: int, text: str):
    async with get_sessionmaker()() as db:
        return await commit_transcript_segment(
            db,
            session_id=session_id,
            producer_key=key,
            start_ms=start_ms,
            end_ms=start_ms + 800,
            text=text,
            language="id",
        )


async def next_notice(pubsub, *, timeout: float = 1.5) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if message is not None and message.get("type") == "message":
            return json.loads(message["data"])
    raise AssertionError("timed out waiting for transcript notice")


@pytest.mark.asyncio
async def test_canonical_commit_publishes_ephemeral_session_scoped_wakeup(
    client: AsyncClient,
) -> None:
    first_id = await create_session(client, "writer-transcript-live-0001")
    second_id = await create_session(client, "writer-transcript-live-0002")
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    first_sub = redis.pubsub()
    second_sub = redis.pubsub()
    try:
        await first_sub.subscribe(transcript_channel(first_id))
        await second_sub.subscribe(transcript_channel(second_id))

        first, idempotent = await commit_segment(first_id, "live-a", 0, "meeting A")
        assert idempotent is False
        notice = await next_notice(first_sub)
        assert notice == {
            "type": "transcript_available",
            "session_id": str(first_id),
            "sequence": first.sequence,
        }

        unrelated = await second_sub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        assert unrelated is None

        second, _ = await commit_segment(second_id, "live-b", 0, "meeting B")
        second_notice = await next_notice(second_sub)
        assert second_notice["session_id"] == str(second_id)
        assert second_notice["sequence"] == second.sequence == 1
    finally:
        await first_sub.aclose()
        await second_sub.aclose()
        await redis.aclose()


@pytest.mark.asyncio
async def test_disconnect_misses_ephemeral_event_but_http_cursor_recovers_it(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-transcript-recover-0001")
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    first_sub = redis.pubsub()
    await first_sub.subscribe(transcript_channel(session_id))
    first, _ = await commit_segment(session_id, "recover-1", 0, "pertama")
    assert (await next_notice(first_sub))["sequence"] == first.sequence
    await first_sub.aclose()

    second, _ = await commit_segment(session_id, "recover-2", 1000, "kedua")

    resumed_sub = redis.pubsub()
    try:
        await resumed_sub.subscribe(transcript_channel(session_id))
        missed_history = await resumed_sub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        assert missed_history is None

        response = await client.get(
            f"/api/v1/sessions/{session_id}/transcript",
            params={"after_sequence": first.sequence},
        )
        assert response.status_code == 200, response.text
        assert response.json()["next_after_sequence"] == second.sequence
        assert [item["text"] for item in response.json()["segments"]] == ["kedua"]
    finally:
        await resumed_sub.aclose()
        await redis.aclose()


@pytest.mark.asyncio
async def test_delivery_failure_cannot_roll_back_transcript_or_recording(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await create_session(client, "writer-transcript-degraded-0001")

    async def unavailable_delivery(*, session_id: UUID, sequence: int) -> bool:
        del session_id, sequence
        raise OSError("forced delivery outage")

    monkeypatch.setattr("recantor.transcript.publish_transcript_available", unavailable_delivery)

    segment, idempotent = await commit_segment(session_id, "degraded-1", 0, "tetap tersimpan")
    assert idempotent is False
    assert segment.sequence == 1

    transcript = await client.get(f"/api/v1/sessions/{session_id}/transcript")
    assert transcript.status_code == 200
    assert [item["text"] for item in transcript.json()["segments"]] == ["tetap tersimpan"]

    recording = await client.get(f"/api/v1/sessions/{session_id}/recording-state")
    assert recording.status_code == 200
    assert recording.json()["session"]["state"] == "RECORDING"
