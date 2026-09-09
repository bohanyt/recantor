import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.models import RecordingSession
from recantor.recording import utcnow
from recantor.settings import get_settings


@pytest.fixture
async def client(clean_recording_state) -> AsyncClient:
    del clean_recording_state
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


async def create_session(client: AsyncClient, writer_id: str) -> tuple[str, str]:
    request_id = str(uuid4())
    response = await client.post(
        "/api/v1/sessions/live",
        json={"client_request_id": request_id, "writer_id": writer_id},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], request_id


async def put_chunk(
    client: AsyncClient,
    *,
    session_id: str,
    writer_id: str,
    epoch: int,
    sequence: int,
    payload: bytes,
    start_ms: int,
    end_ms: int,
):
    digest = hashlib.sha256(payload).hexdigest()
    return await client.put(
        f"/api/v1/sessions/{session_id}/chunks/{sequence}",
        params={
            "writer_id": writer_id,
            "capture_epoch": epoch,
            "monotonic_start_ms": start_ms,
            "monotonic_end_ms": end_ms,
            "sha256": digest,
            "content_type": "audio/webm;codecs=opus",
        },
        content=payload,
        headers={"content-type": "application/octet-stream"},
    )


@pytest.mark.asyncio
async def test_create_is_idempotent_and_competing_writer_is_rejected(client: AsyncClient) -> None:
    writer = "writer-primary-0001"
    session_id, request_id = await create_session(client, writer)

    retry = await client.post(
        "/api/v1/sessions/live",
        json={"client_request_id": request_id, "writer_id": writer},
    )
    assert retry.status_code == 201
    assert retry.json()["id"] == session_id
    assert retry.json()["capture_epoch"] == 1

    conflict = await client.post(
        f"/api/v1/sessions/{session_id}/capture/claim",
        json={"writer_id": "writer-secondary-0002", "expected_epoch": 1},
    )
    assert conflict.status_code == 409

    same_writer = await client.post(
        f"/api/v1/sessions/{session_id}/capture/claim",
        json={"writer_id": writer, "expected_epoch": 1},
    )
    assert same_writer.status_code == 200
    assert same_writer.json()["capture_epoch"] == 1


@pytest.mark.asyncio
async def test_chunk_ack_is_durable_idempotent_and_conflict_safe(client: AsyncClient) -> None:
    writer = "writer-durable-0001"
    session_id, _ = await create_session(client, writer)
    payload = b"phase-one-fragment"

    first = await put_chunk(
        client,
        session_id=session_id,
        writer_id=writer,
        epoch=1,
        sequence=1,
        payload=payload,
        start_ms=0,
        end_ms=2000,
    )
    assert first.status_code == 200, first.text
    assert first.json()["idempotent"] is False

    duplicate = await put_chunk(
        client,
        session_id=session_id,
        writer_id=writer,
        epoch=1,
        sequence=1,
        payload=payload,
        start_ms=0,
        end_ms=2000,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["idempotent"] is True

    conflict = await put_chunk(
        client,
        session_id=session_id,
        writer_id=writer,
        epoch=1,
        sequence=1,
        payload=b"different-content",
        start_ms=0,
        end_ms=2000,
    )
    assert conflict.status_code == 409

    state = await client.get(f"/api/v1/sessions/{session_id}/recording-state")
    assert state.status_code == 200
    assert state.json()["accepted_count"] == 1
    assert state.json()["highest_contiguous_sequence"] == 1
    assert state.json()["accepted_ranges"] == [{"start": 1, "end": 1}]


@pytest.mark.asyncio
async def test_finalize_waits_for_missing_chunk_then_completes(client: AsyncClient) -> None:
    writer = "writer-finalize-0001"
    session_id, _ = await create_session(client, writer)

    for sequence in (1, 3):
        response = await put_chunk(
            client,
            session_id=session_id,
            writer_id=writer,
            epoch=1,
            sequence=sequence,
            payload=f"chunk-{sequence}".encode(),
            start_ms=(sequence - 1) * 2000,
            end_ms=sequence * 2000,
        )
        assert response.status_code == 200

    incomplete = await client.post(
        f"/api/v1/sessions/{session_id}/finalize",
        json={
            "writer_id": writer,
            "capture_epoch": 1,
            "final_sequence": 3,
            "final_monotonic_end_ms": 6000,
            "gap_sequences": [],
        },
    )
    assert incomplete.status_code == 200
    assert incomplete.json()["complete"] is False
    assert incomplete.json()["missing_sequences"] == [2]
    assert incomplete.json()["session"]["state"] == "finalizing"

    recovered = await put_chunk(
        client,
        session_id=session_id,
        writer_id=writer,
        epoch=1,
        sequence=2,
        payload=b"chunk-2",
        start_ms=2000,
        end_ms=4000,
    )
    assert recovered.status_code == 200

    complete = await client.post(
        f"/api/v1/sessions/{session_id}/finalize",
        json={
            "writer_id": writer,
            "capture_epoch": 1,
            "final_sequence": 3,
            "final_monotonic_end_ms": 6000,
            "gap_sequences": [],
        },
    )
    assert complete.status_code == 200
    assert complete.json()["complete"] is True
    assert complete.json()["missing_sequences"] == []
    assert complete.json()["session"]["state"] == "complete"


@pytest.mark.asyncio
async def test_finalize_can_explicitly_record_unrecoverable_gap(client: AsyncClient) -> None:
    writer = "writer-gap-0000001"
    session_id, _ = await create_session(client, writer)
    response = await put_chunk(
        client,
        session_id=session_id,
        writer_id=writer,
        epoch=1,
        sequence=1,
        payload=b"only-first",
        start_ms=0,
        end_ms=2000,
    )
    assert response.status_code == 200

    complete = await client.post(
        f"/api/v1/sessions/{session_id}/finalize",
        json={
            "writer_id": writer,
            "capture_epoch": 1,
            "final_sequence": 3,
            "final_monotonic_end_ms": 6000,
            "gap_sequences": [2, 3],
        },
    )
    assert complete.status_code == 200
    body = complete.json()
    assert body["complete"] is True
    assert body["missing_sequences"] == []
    assert any(gap["sequence_start"] == 2 and gap["sequence_end"] == 3 for gap in body["gaps"])


@pytest.mark.asyncio
async def test_two_sessions_keep_sequences_and_audio_metadata_isolated(client: AsyncClient) -> None:
    first_id, _ = await create_session(client, "writer-isolated-0001")
    second_id, _ = await create_session(client, "writer-isolated-0002")

    first = await put_chunk(
        client,
        session_id=first_id,
        writer_id="writer-isolated-0001",
        epoch=1,
        sequence=1,
        payload=b"meeting-a",
        start_ms=0,
        end_ms=2000,
    )
    second = await put_chunk(
        client,
        session_id=second_id,
        writer_id="writer-isolated-0002",
        epoch=1,
        sequence=1,
        payload=b"meeting-b",
        start_ms=0,
        end_ms=2000,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["storage_key"] != second.json()["storage_key"]

    first_state = (await client.get(f"/api/v1/sessions/{first_id}/recording-state")).json()
    second_state = (await client.get(f"/api/v1/sessions/{second_id}/recording-state")).json()
    assert first_state["accepted_ranges"] == second_state["accepted_ranges"] == [
        {"start": 1, "end": 1}
    ]
    assert first_state["session"]["id"] != second_state["session"]["id"]


@pytest.mark.asyncio
async def test_stale_heartbeat_becomes_interrupted_without_becoming_complete(client: AsyncClient) -> None:
    writer = "writer-heartbeat-0001"
    session_id, _ = await create_session(client, writer)

    async with get_sessionmaker()() as db:
        session = await db.scalar(
            select(RecordingSession).where(RecordingSession.id == UUID(session_id))
        )
        assert session is not None
        session.last_heartbeat_at = utcnow() - timedelta(
            seconds=get_settings().recording_heartbeat_timeout_seconds + 5
        )
        await db.commit()

    refreshed = await client.get(f"/api/v1/sessions/{session_id}")
    assert refreshed.status_code == 200
    assert refreshed.json()["state"] == "interrupted"
    assert refreshed.json()["finalized_at"] is None

    resumed = await client.post(
        f"/api/v1/sessions/{session_id}/capture/claim",
        json={"writer_id": writer, "expected_epoch": 1},
    )
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "recording"
