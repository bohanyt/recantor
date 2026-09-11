import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import uvicorn
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import RedisError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.settings import get_settings
from recantor.transcript import commit_transcript_segment
from recantor.transcript_realtime import (
    TranscriptRealtimeNotifier,
    transcript_channel,
    transcript_notice,
)


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


def transcript_ws_url(port: int, session_id: UUID) -> str:
    return f"ws://127.0.0.1:{port}/api/v1/sessions/{session_id}/transcript/live"


@asynccontextmanager
async def live_api_server() -> AsyncIterator[int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("uvicorn test server did not start")
        yield port
    finally:
        server.should_exit = True
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=5)
        listener.close()


async def recv_json(websocket, *, timeout: float = 2.0) -> dict[str, object]:
    payload = await asyncio.wait_for(websocket.recv(), timeout=timeout)
    assert isinstance(payload, str)
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return parsed


def test_bounded_notifier_does_not_wait_for_slow_publisher() -> None:
    started = Event()
    release = Event()

    def slow_publisher(session_id: UUID, sequence: int) -> bool:
        del session_id, sequence
        started.set()
        release.wait(timeout=2)
        return True

    notifier = TranscriptRealtimeNotifier(max_pending=1, publisher=slow_publisher)
    session_id = uuid4()
    try:
        first_started_at = monotonic()
        assert notifier.enqueue(session_id=session_id, sequence=1) is True
        assert monotonic() - first_started_at < 0.25
        assert started.wait(timeout=1)

        queued_at = monotonic()
        assert notifier.enqueue(session_id=session_id, sequence=2) is True
        assert notifier.enqueue(session_id=session_id, sequence=3) is False
        assert monotonic() - queued_at < 0.1
    finally:
        release.set()


@pytest.mark.asyncio
async def test_commit_enqueues_realtime_only_after_database_commit(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await create_session(client, "writer-transcript-post-commit-0001")
    observed_transaction_states: list[bool] = []

    async with get_sessionmaker()() as db:

        def record_enqueue(*, session_id: UUID, sequence: int) -> bool:
            del session_id, sequence
            observed_transaction_states.append(db.in_transaction())
            return True

        monkeypatch.setattr("recantor.transcript.enqueue_transcript_available", record_enqueue)
        segment, idempotent = await commit_transcript_segment(
            db,
            session_id=session_id,
            producer_key="post-commit",
            start_ms=0,
            end_ms=800,
            text="committed first",
        )

    assert idempotent is False
    assert segment.sequence == 1
    assert observed_transaction_states == [False]


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
        assert notice == transcript_notice(first_id, first.sequence)

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
async def test_real_websocket_route_forwards_only_valid_session_notice(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-transcript-ws-0001")
    other_id = await create_session(client, "writer-transcript-ws-0002")

    async with (
        live_api_server() as port,
        connect(transcript_ws_url(port, session_id)) as websocket,
    ):
        assert await recv_json(websocket) == {"type": "ready"}

        redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            await redis.publish(transcript_channel(session_id), "not-json")
            await redis.publish(
                transcript_channel(session_id),
                json.dumps(transcript_notice(other_id, 7)),
            )
            expected = transcript_notice(session_id, 3)
            await redis.publish(
                transcript_channel(session_id),
                json.dumps(expected),
            )
            assert await recv_json(websocket) == expected
        finally:
            await redis.aclose()


@pytest.mark.asyncio
async def test_real_websocket_route_rejects_missing_session(client: AsyncClient) -> None:
    del client
    missing_id = uuid4()
    async with (
        live_api_server() as port,
        connect(transcript_ws_url(port, missing_id)) as websocket,
    ):
        error = await recv_json(websocket)
        assert error["type"] == "error"
        assert error["code"] == "session_not_found"
        with pytest.raises(ConnectionClosed) as closed:
            await websocket.recv()
        assert closed.value.code == 1008


@pytest.mark.asyncio
async def test_real_websocket_route_degrades_and_cleans_up_subscription(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await create_session(client, "writer-transcript-ws-degraded-0001")

    class FailingPubSub:
        def __init__(self) -> None:
            self.subscribed: str | None = None
            self.unsubscribed: str | None = None
            self.closed = False

        async def subscribe(self, channel: str) -> None:
            self.subscribed = channel

        async def get_message(self, **kwargs):
            del kwargs
            raise RedisError("forced pubsub failure")

        async def unsubscribe(self, channel: str) -> None:
            self.unsubscribed = channel

        async def aclose(self) -> None:
            self.closed = True

    class FailingRedis:
        def __init__(self) -> None:
            self.pubsub_instance = FailingPubSub()
            self.closed = False

        def pubsub(self) -> FailingPubSub:
            return self.pubsub_instance

        async def aclose(self) -> None:
            self.closed = True

    fake_redis = FailingRedis()
    monkeypatch.setattr(
        "recantor.routes.transcript.create_transcript_redis",
        lambda: fake_redis,
    )

    async with (
        live_api_server() as port,
        connect(transcript_ws_url(port, session_id)) as websocket,
    ):
        assert await recv_json(websocket) == {"type": "ready"}
        degraded = await recv_json(websocket)
        assert degraded["type"] == "delivery_degraded"
        with pytest.raises(ConnectionClosed) as closed:
            await websocket.recv()
        assert closed.value.code == 1013

    assert fake_redis.pubsub_instance.subscribed == transcript_channel(session_id)
    assert fake_redis.pubsub_instance.unsubscribed == transcript_channel(session_id)
    assert fake_redis.pubsub_instance.closed is True
    assert fake_redis.closed is True


@pytest.mark.asyncio
async def test_delivery_failure_cannot_roll_back_transcript_or_recording(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await create_session(client, "writer-transcript-degraded-0001")

    def unavailable_delivery(*, session_id: UUID, sequence: int) -> bool:
        del session_id, sequence
        raise OSError("forced delivery outage")

    monkeypatch.setattr("recantor.transcript.enqueue_transcript_available", unavailable_delivery)

    segment, idempotent = await commit_segment(session_id, "degraded-1", 0, "tetap tersimpan")
    assert idempotent is False
    assert segment.sequence == 1

    transcript = await client.get(f"/api/v1/sessions/{session_id}/transcript")
    assert transcript.status_code == 200
    assert [item["text"] for item in transcript.json()["segments"]] == ["tetap tersimpan"]

    recording = await client.get(f"/api/v1/sessions/{session_id}/recording-state")
    assert recording.status_code == 200
    assert recording.json()["session"]["state"] == "recording"
