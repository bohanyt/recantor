"""Daemon-free checks of the public Live transcript route boundary."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from recantor.db import get_db_session
from recantor.main import app
from recantor.routes import transcript as transcript_routes


class FakeSession:
    def __init__(self, session_id=None):
        self.session_id = session_id

    async def scalar(self, statement):
        assert "recording_sessions.kind" in str(statement)
        return self.session_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.close_code = None

    async def accept(self):
        return None

    async def send_json(self, payload):
        self.messages.append(payload)

    async def close(self, code):
        self.close_code = code


@pytest.mark.asyncio
async def test_generic_http_route_hides_upload_session_uuid(monkeypatch):
    upload_id = uuid4()

    async def fake_db():
        yield FakeSession()

    async def reader(*_args, **_kwargs):
        raise AssertionError("Upload transcript reader must not run through Live route")

    monkeypatch.setattr(transcript_routes, "read_transcript_segments", reader)
    app.dependency_overrides[get_db_session] = fake_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/sessions/{upload_id}/transcript")
        assert response.status_code == 404
        assert response.json()["detail"] == "recording session not found"
    finally:
        app.dependency_overrides.pop(get_db_session, None)


@pytest.mark.asyncio
async def test_generic_http_route_still_reads_live_session(monkeypatch):
    live_id = uuid4()

    async def fake_db():
        yield FakeSession(live_id)

    async def reader(_db, *, session_id, after_sequence, limit):
        assert session_id == live_id
        assert after_sequence == 0
        assert limit == 200
        return [], False

    monkeypatch.setattr(transcript_routes, "read_transcript_segments", reader)
    app.dependency_overrides[get_db_session] = fake_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/sessions/{live_id}/transcript")
        assert response.status_code == 200
        assert response.json()["segments"] == []
    finally:
        app.dependency_overrides.pop(get_db_session, None)


@pytest.mark.asyncio
async def test_generic_websocket_hides_upload_before_subscription(monkeypatch):
    upload_id = uuid4()
    websocket = FakeWebSocket()
    monkeypatch.setattr(transcript_routes, "get_sessionmaker", lambda: lambda: FakeSession())

    def forbidden_redis():
        raise AssertionError("Upload session must not subscribe to Live transcript notices")

    monkeypatch.setattr(transcript_routes, "create_transcript_redis", forbidden_redis)
    await transcript_routes.session_transcript_live(websocket, upload_id)
    assert websocket.messages == [
        {
            "type": "error",
            "code": "session_not_found",
            "detail": "recording session not found",
        }
    ]
    assert websocket.close_code == 1008


@pytest.mark.asyncio
async def test_generic_websocket_still_admits_live_session(monkeypatch):
    live_id = uuid4()
    websocket = FakeWebSocket()
    monkeypatch.setattr(transcript_routes, "get_sessionmaker", lambda: lambda: FakeSession(live_id))

    class FakePubSub:
        channel = None

        async def subscribe(self, channel):
            self.channel = channel

        async def get_message(self, **_kwargs):
            raise RedisError("synthetic delivery loss")

        async def unsubscribe(self, _channel):
            return None

        async def aclose(self):
            return None

    class FakeRedis:
        def __init__(self):
            self.subscription = FakePubSub()

        def pubsub(self):
            return self.subscription

        async def aclose(self):
            return None

    redis = FakeRedis()
    monkeypatch.setattr(transcript_routes, "create_transcript_redis", lambda: redis)
    await transcript_routes.session_transcript_live(websocket, live_id)
    assert redis.subscription.channel == f"recantor:transcript:{live_id}"
    assert websocket.messages[0] == {"type": "ready"}
    assert websocket.messages[1]["type"] == "delivery_degraded"
    assert websocket.close_code == 1013
