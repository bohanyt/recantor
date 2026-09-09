from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.models import RecordingSession
from recantor.recording import utcnow
from recantor.settings import get_settings


def recovery_token(writer_id: str) -> str:
    return f"recantor-recovery-capability::{writer_id}::issue10"


@pytest_asyncio.fixture
async def client(clean_recording_state) -> AsyncClient:
    del clean_recording_state
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


async def create_session(client: AsyncClient, writer_id: str) -> str:
    response = await client.post(
        "/api/v1/sessions/live",
        json={
            "client_request_id": str(uuid4()),
            "writer_id": writer_id,
            "recovery_token": recovery_token(writer_id),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def make_heartbeat_stale(session_id: str) -> None:
    async with get_sessionmaker()() as db:
        session = await db.scalar(
            select(RecordingSession).where(RecordingSession.id == UUID(session_id))
        )
        assert session is not None
        session.last_heartbeat_at = utcnow() - timedelta(
            seconds=get_settings().recording_heartbeat_timeout_seconds + 5
        )
        await db.commit()


@pytest.mark.asyncio
async def test_stall_read_then_heartbeat_restores_current_state_and_keeps_history(
    client: AsyncClient,
) -> None:
    writer = "writer-liveness-read-0001"
    session_id = await create_session(client, writer)
    await make_heartbeat_stale(session_id)

    interrupted = await client.get(f"/api/v1/sessions/{session_id}")
    assert interrupted.status_code == 200
    assert interrupted.json()["state"] == "interrupted"
    historical_interruption = interrupted.json()["interrupted_at"]
    assert historical_interruption is not None

    restored = await client.post(
        f"/api/v1/sessions/{session_id}/heartbeat",
        json={"writer_id": writer, "capture_epoch": 1},
    )
    assert restored.status_code == 200
    assert restored.json()["state"] == "recording"
    assert restored.json()["interrupted_at"] == historical_interruption

    current = await client.get(f"/api/v1/sessions/{session_id}")
    assert current.status_code == 200
    assert current.json()["state"] == "recording"
    assert current.json()["interrupted_at"] == historical_interruption


@pytest.mark.asyncio
async def test_stall_then_heartbeat_without_read_records_history_before_refresh(
    client: AsyncClient,
) -> None:
    writer = "writer-liveness-heartbeat-0001"
    session_id = await create_session(client, writer)
    await make_heartbeat_stale(session_id)

    restored = await client.post(
        f"/api/v1/sessions/{session_id}/heartbeat",
        json={"writer_id": writer, "capture_epoch": 1},
    )
    assert restored.status_code == 200
    assert restored.json()["state"] == "recording"
    assert restored.json()["interrupted_at"] is not None

    stable_heartbeat = await client.post(
        f"/api/v1/sessions/{session_id}/heartbeat",
        json={"writer_id": writer, "capture_epoch": 1},
    )
    assert stable_heartbeat.status_code == 200
    assert stable_heartbeat.json()["state"] == "recording"
    assert stable_heartbeat.json()["interrupted_at"] == restored.json()["interrupted_at"]

    state = await client.get(f"/api/v1/sessions/{session_id}/recording-state")
    assert state.status_code == 200
    assert state.json()["session"]["state"] == "recording"
    assert state.json()["session"]["interrupted_at"] == restored.json()["interrupted_at"]
    assert state.json()["gaps"] == []


@pytest.mark.asyncio
async def test_stall_then_claim_preserves_history_while_new_generation_is_current(
    client: AsyncClient,
) -> None:
    writer = "writer-liveness-claim-0001"
    session_id = await create_session(client, writer)
    await make_heartbeat_stale(session_id)

    claimed = await client.post(
        f"/api/v1/sessions/{session_id}/capture/claim",
        json={
            "writer_id": "writer-liveness-claim-0002",
            "expected_epoch": 1,
            "recovery_token": recovery_token(writer),
        },
    )
    assert claimed.status_code == 200
    assert claimed.json()["state"] == "recording"
    assert claimed.json()["capture_epoch"] == 2
    assert claimed.json()["active_writer_id"] == "writer-liveness-claim-0002"
    assert claimed.json()["interrupted_at"] is not None

    state = await client.get(f"/api/v1/sessions/{session_id}/recording-state")
    assert state.status_code == 200
    assert state.json()["session"]["state"] == "recording"
    assert state.json()["session"]["interrupted_at"] == claimed.json()["interrupted_at"]
    assert state.json()["gaps"] == []
