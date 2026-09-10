import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.transcript import TranscriptConflict, commit_transcript_segment


def recovery_token(writer_id: str) -> str:
    return f"recantor-recovery-capability::{writer_id}::transcript"


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


async def commit_segment(
    *,
    session_id: UUID,
    producer_key: str,
    start_ms: int,
    end_ms: int,
    text: str,
    language: str | None = None,
):
    async with get_sessionmaker()() as db:
        return await commit_transcript_segment(
            db,
            session_id=session_id,
            producer_key=producer_key,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            language=language,
        )


@pytest.mark.asyncio
async def test_segments_receive_session_sequence_and_support_reconnect_cursor(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-transcript-order-0001")

    first, first_idempotent = await commit_segment(
        session_id=session_id,
        producer_key="utterance-1",
        start_ms=0,
        end_ms=1200,
        text="Selamat pagi.",
        language="id",
    )
    second, second_idempotent = await commit_segment(
        session_id=session_id,
        producer_key="utterance-2",
        start_ms=1200,
        end_ms=2600,
        text="Kita mulai rapatnya.",
        language="id",
    )

    assert first_idempotent is False
    assert second_idempotent is False
    assert first.sequence == 1
    assert second.sequence == 2

    first_page = await client.get(
        f"/api/v1/sessions/{session_id}/transcript",
        params={"after_sequence": 0, "limit": 1},
    )
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["has_more"] is True
    assert first_page.json()["next_after_sequence"] == 1
    assert [item["sequence"] for item in first_page.json()["segments"]] == [1]

    resumed = await client.get(
        f"/api/v1/sessions/{session_id}/transcript",
        params={"after_sequence": 1, "limit": 20},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["has_more"] is False
    assert resumed.json()["next_after_sequence"] == 2
    assert [item["sequence"] for item in resumed.json()["segments"]] == [2]
    assert resumed.json()["segments"][0]["text"] == "Kita mulai rapatnya."

    exhausted = await client.get(
        f"/api/v1/sessions/{session_id}/transcript",
        params={"after_sequence": 2},
    )
    assert exhausted.status_code == 200
    assert exhausted.json()["segments"] == []
    assert exhausted.json()["next_after_sequence"] == 2


@pytest.mark.asyncio
async def test_producer_key_retry_is_idempotent_but_conflicting_retry_is_rejected(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-transcript-idem-0001")

    first, first_idempotent = await commit_segment(
        session_id=session_id,
        producer_key=" utterance-idempotent ",
        start_ms=100,
        end_ms=900,
        text=" Halo dunia. ",
        language="ID",
    )
    retry, retry_idempotent = await commit_segment(
        session_id=session_id,
        producer_key="utterance-idempotent",
        start_ms=100,
        end_ms=900,
        text="Halo dunia.",
        language="id",
    )

    assert first_idempotent is False
    assert retry_idempotent is True
    assert retry.id == first.id
    assert retry.sequence == first.sequence == 1
    assert retry.text == "Halo dunia."
    assert retry.language == "id"

    with pytest.raises(TranscriptConflict, match="retry conflicts"):
        await commit_segment(
            session_id=session_id,
            producer_key="utterance-idempotent",
            start_ms=100,
            end_ms=900,
            text="Teks yang berbeda.",
            language="id",
        )


@pytest.mark.asyncio
async def test_concurrent_commits_serialize_sequence_allocation_per_session(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-transcript-concurrent-0001")

    async def commit_one(key: str, start_ms: int) -> int:
        segment, _ = await commit_segment(
            session_id=session_id,
            producer_key=key,
            start_ms=start_ms,
            end_ms=start_ms + 800,
            text=f"segment {key}",
        )
        return segment.sequence

    sequences = await asyncio.gather(
        commit_one("parallel-a", 0),
        commit_one("parallel-b", 1000),
    )
    assert sorted(sequences) == [1, 2]

    response = await client.get(f"/api/v1/sessions/{session_id}/transcript")
    assert response.status_code == 200
    assert [item["sequence"] for item in response.json()["segments"]] == [1, 2]


@pytest.mark.asyncio
async def test_independent_sessions_keep_transcript_sequences_and_text_isolated(
    client: AsyncClient,
) -> None:
    first_id = await create_session(client, "writer-transcript-isolated-0001")
    second_id = await create_session(client, "writer-transcript-isolated-0002")

    first, _ = await commit_segment(
        session_id=first_id,
        producer_key="utterance-1",
        start_ms=0,
        end_ms=1000,
        text="meeting A",
    )
    second, _ = await commit_segment(
        session_id=second_id,
        producer_key="utterance-1",
        start_ms=0,
        end_ms=1000,
        text="meeting B",
    )

    assert first.sequence == second.sequence == 1

    first_response = await client.get(f"/api/v1/sessions/{first_id}/transcript")
    second_response = await client.get(f"/api/v1/sessions/{second_id}/transcript")
    assert [item["text"] for item in first_response.json()["segments"]] == ["meeting A"]
    assert [item["text"] for item in second_response.json()["segments"]] == ["meeting B"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer_key", "start_ms", "end_ms", "text", "match"),
    [
        ("", 0, 1000, "valid", "producer key"),
        ("valid", -1, 1000, "valid", "timing"),
        ("valid", 1000, 1000, "valid", "timing"),
        ("valid", 0, 1000, "   ", "text"),
    ],
)
async def test_invalid_canonical_segment_payload_is_rejected(
    client: AsyncClient,
    producer_key: str,
    start_ms: int,
    end_ms: int,
    text: str,
    match: str,
) -> None:
    session_id = await create_session(client, f"writer-transcript-invalid-{uuid4().hex[:8]}")
    with pytest.raises(TranscriptConflict, match=match):
        await commit_segment(
            session_id=session_id,
            producer_key=producer_key,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
        )


@pytest.mark.asyncio
async def test_missing_session_transcript_read_returns_not_found(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/sessions/{uuid4()}/transcript")
    assert response.status_code == 404
    assert response.json()["detail"] == "recording session not found"
