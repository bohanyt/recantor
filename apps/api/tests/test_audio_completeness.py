import hashlib
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from recantor import recording as recording_module
from recantor.main import app


def recovery_token(writer_id: str) -> str:
    return f"recantor-recovery-capability::{writer_id}::audio-completeness"


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
    assert response.json()["audio_completeness"] is None
    return response.json()["id"]


async def put_chunk(
    client: AsyncClient,
    *,
    session_id: str,
    writer_id: str,
    sequence: int,
) -> None:
    payload = f"audio-completeness-{sequence}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    response = await client.put(
        f"/api/v1/sessions/{session_id}/chunks/{sequence}",
        params={
            "writer_id": writer_id,
            "capture_epoch": 1,
            "monotonic_start_ms": (sequence - 1) * 1000,
            "monotonic_end_ms": sequence * 1000,
            "sha256": digest,
            "content_type": "audio/webm;codecs=opus",
        },
        content=payload,
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text


async def finalize(
    client: AsyncClient,
    *,
    session_id: str,
    writer_id: str,
    final_sequence: int,
    gap_sequences: list[int] | None = None,
):
    return await client.post(
        f"/api/v1/sessions/{session_id}/finalize",
        json={
            "writer_id": writer_id,
            "capture_epoch": 1,
            "final_sequence": final_sequence,
            "final_monotonic_end_ms": final_sequence * 1000,
            "gap_sequences": gap_sequences or [],
        },
    )


@pytest.mark.asyncio
async def test_finalize_persists_full_partial_and_empty_audio_completeness(
    client: AsyncClient,
) -> None:
    full_writer = "writer-completeness-full-0001"
    full_id = await create_session(client, full_writer)
    await put_chunk(client, session_id=full_id, writer_id=full_writer, sequence=1)
    await put_chunk(client, session_id=full_id, writer_id=full_writer, sequence=2)
    full = await finalize(
        client,
        session_id=full_id,
        writer_id=full_writer,
        final_sequence=2,
    )
    assert full.status_code == 200, full.text
    assert full.json()["complete"] is True
    assert full.json()["audio_completeness"] == "full"
    assert full.json()["session"]["audio_completeness"] == "full"
    full_session = await client.get(f"/api/v1/sessions/{full_id}")
    assert full_session.json()["audio_completeness"] == "full"

    partial_writer = "writer-completeness-partial-0001"
    partial_id = await create_session(client, partial_writer)
    await put_chunk(client, session_id=partial_id, writer_id=partial_writer, sequence=1)
    partial = await finalize(
        client,
        session_id=partial_id,
        writer_id=partial_writer,
        final_sequence=3,
        gap_sequences=[2, 3],
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["complete"] is True
    assert partial.json()["audio_completeness"] == "partial"
    assert partial.json()["session"]["audio_completeness"] == "partial"
    partial_session = await client.get(f"/api/v1/sessions/{partial_id}")
    assert partial_session.json()["audio_completeness"] == "partial"

    empty_writer = "writer-completeness-empty-0001"
    empty_id = await create_session(client, empty_writer)
    empty = await finalize(
        client,
        session_id=empty_id,
        writer_id=empty_writer,
        final_sequence=0,
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["complete"] is True
    assert empty.json()["audio_completeness"] == "empty"
    assert empty.json()["session"]["audio_completeness"] == "empty"
    empty_session = await client.get(f"/api/v1/sessions/{empty_id}")
    assert empty_session.json()["audio_completeness"] == "empty"


@pytest.mark.asyncio
async def test_all_gap_boundary_is_partial_not_empty(client: AsyncClient) -> None:
    writer = "writer-completeness-all-gap-0001"
    session_id = await create_session(client, writer)
    response = await finalize(
        client,
        session_id=session_id,
        writer_id=writer,
        final_sequence=3,
        gap_sequences=[1, 2, 3],
    )
    assert response.status_code == 200, response.text
    assert response.json()["audio_completeness"] == "partial"


@pytest.mark.asyncio
async def test_wall_clock_only_gap_does_not_downgrade_full_audio(client: AsyncClient) -> None:
    writer = "writer-completeness-wall-gap-0001"
    session_id = await create_session(client, writer)
    gap = await client.post(
        f"/api/v1/sessions/{session_id}/gaps",
        json={
            "client_gap_id": str(uuid4()),
            "writer_id": writer,
            "capture_epoch": 1,
            "wall_started_at": "2026-09-10T00:00:00Z",
            "wall_ended_at": "2026-09-10T00:00:05Z",
            "reason": "liveness_observation_only",
        },
    )
    assert gap.status_code == 201, gap.text
    await put_chunk(client, session_id=session_id, writer_id=writer, sequence=1)
    response = await finalize(
        client,
        session_id=session_id,
        writer_id=writer,
        final_sequence=1,
    )
    assert response.status_code == 200, response.text
    assert response.json()["audio_completeness"] == "full"


@pytest.mark.asyncio
async def test_complete_finalize_retry_preserves_persisted_classification(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = "writer-completeness-retry-0001"
    session_id = await create_session(client, writer)
    await put_chunk(client, session_id=session_id, writer_id=writer, sequence=1)
    first = await finalize(
        client,
        session_id=session_id,
        writer_id=writer,
        final_sequence=1,
    )
    assert first.status_code == 200, first.text
    assert first.json()["audio_completeness"] == "full"

    def fail_if_recomputed(**_kwargs):
        raise AssertionError("COMPLETE finalize retry must not recompute audio completeness")

    monkeypatch.setattr(recording_module, "_classify_audio_completeness", fail_if_recomputed)
    retry = await finalize(
        client,
        session_id=session_id,
        writer_id=writer,
        final_sequence=1,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["complete"] is True
    assert retry.json()["audio_completeness"] == "full"
