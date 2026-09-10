import asyncio
import hashlib
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.settings import get_settings
from recantor.storage import FilesystemAudioStorage
from recantor.utterance import (
    UtteranceWorkConflict,
    commit_utterance_work,
    transcript_producer_key_for_utterance,
    utterance_work_id,
)


def recovery_token(writer_id: str) -> str:
    return f"recantor-recovery-capability::{writer_id}::utterance"


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


async def commit_work(
    *,
    session_id: UUID,
    producer_key: str,
    start_ms: int,
    end_ms: int,
    content_type: str = "audio/wav",
    payload: bytes = b"utterance-audio",
):
    async with get_sessionmaker()() as db:
        return await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key=producer_key,
            start_ms=start_ms,
            end_ms=end_ms,
            content_type=content_type,
            payload=payload,
        )


@pytest.mark.asyncio
async def test_utterances_get_deterministic_id_sequence_and_inspection_cursor(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-utterance-order-0001")

    first, first_idempotent = await commit_work(
        session_id=session_id,
        producer_key="speech-a",
        start_ms=0,
        end_ms=1200,
        payload=b"first utterance",
    )
    second, second_idempotent = await commit_work(
        session_id=session_id,
        producer_key="speech-b",
        start_ms=1300,
        end_ms=2500,
        payload=b"second utterance",
    )

    assert first_idempotent is False
    assert second_idempotent is False
    assert first.id == utterance_work_id(session_id, "speech-a")
    assert first.sequence == 1
    assert second.sequence == 2
    assert transcript_producer_key_for_utterance(first.id) == f"utterance:{first.id}"

    first_page = await client.get(
        f"/api/v1/sessions/{session_id}/utterances",
        params={"after_sequence": 0, "limit": 1},
    )
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["has_more"] is True
    assert first_page.json()["next_after_sequence"] == 1
    assert [item["sequence"] for item in first_page.json()["utterances"]] == [1]
    assert "storage_key" not in first_page.json()["utterances"][0]

    resumed = await client.get(
        f"/api/v1/sessions/{session_id}/utterances",
        params={"after_sequence": 1, "limit": 20},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["has_more"] is False
    assert resumed.json()["next_after_sequence"] == 2
    assert [item["sequence"] for item in resumed.json()["utterances"]] == [2]


@pytest.mark.asyncio
async def test_identical_retry_is_idempotent_and_conflicting_retries_fail(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-utterance-idem-0001")

    first, first_idempotent = await commit_work(
        session_id=session_id,
        producer_key=" speech-idempotent ",
        start_ms=100,
        end_ms=900,
        content_type=" Audio/WAV ",
        payload=b"same encoded audio",
    )
    retry, retry_idempotent = await commit_work(
        session_id=session_id,
        producer_key="speech-idempotent",
        start_ms=100,
        end_ms=900,
        content_type="audio/wav",
        payload=b"same encoded audio",
    )

    assert first_idempotent is False
    assert retry_idempotent is True
    assert retry.id == first.id
    assert retry.sequence == first.sequence == 1
    assert retry.content_type == "audio/wav"

    conflicts = [
        {
            "start_ms": 101,
            "end_ms": 900,
            "content_type": "audio/wav",
            "payload": b"same encoded audio",
        },
        {
            "start_ms": 100,
            "end_ms": 900,
            "content_type": "audio/ogg",
            "payload": b"same encoded audio",
        },
        {
            "start_ms": 100,
            "end_ms": 900,
            "content_type": "audio/wav",
            "payload": b"different audio",
        },
    ]
    for conflict in conflicts:
        with pytest.raises(UtteranceWorkConflict, match="retry conflicts"):
            await commit_work(
                session_id=session_id,
                producer_key="speech-idempotent",
                **conflict,
            )


@pytest.mark.asyncio
async def test_retry_reuses_matching_storage_object_left_before_database_commit(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-utterance-crash-0001")
    producer_key = "crash-retry"
    payload = b"durable orphan candidate"
    expected_id = utterance_work_id(session_id, producer_key)
    digest = hashlib.sha256(payload).hexdigest()
    storage = FilesystemAudioStorage(get_settings().audio_storage_path)

    preexisting = storage.commit_utterance_bytes(
        session_id=session_id,
        work_id=expected_id,
        producer_key=producer_key,
        start_ms=2000,
        end_ms=3200,
        content_type="audio/wav",
        payload=payload,
        sha256=digest,
    )

    work, idempotent = await commit_work(
        session_id=session_id,
        producer_key=producer_key,
        start_ms=2000,
        end_ms=3200,
        payload=payload,
    )

    assert idempotent is False
    assert work.id == expected_id
    assert work.storage_key == preexisting.key
    assert work.sha256 == digest
    assert work.byte_length == len(payload)
    assert storage.verify(work.storage_key, sha256=digest, byte_length=len(payload))
    assert storage.verify_utterance(
        session_id=session_id,
        work_id=work.id,
        producer_key=producer_key,
        start_ms=2000,
        end_ms=3200,
        content_type="audio/wav",
        storage_key=work.storage_key,
        sha256=digest,
        byte_length=len(payload),
    )

    retry, retry_idempotent = await commit_work(
        session_id=session_id,
        producer_key=producer_key,
        start_ms=2000,
        end_ms=3200,
        payload=payload,
    )
    assert retry_idempotent is True
    assert retry.id == work.id


@pytest.mark.asyncio
async def test_conflicting_preexisting_storage_object_is_rejected(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-utterance-orphan-conflict-0001")
    producer_key = "orphan-conflict"
    work_id = utterance_work_id(session_id, producer_key)
    existing_payload = b"existing crash bytes"
    storage = FilesystemAudioStorage(get_settings().audio_storage_path)
    storage.commit_utterance_bytes(
        session_id=session_id,
        work_id=work_id,
        producer_key=producer_key,
        start_ms=0,
        end_ms=1000,
        content_type="audio/wav",
        payload=existing_payload,
        sha256=hashlib.sha256(existing_payload).hexdigest(),
    )

    with pytest.raises(UtteranceWorkConflict, match="durable storage evidence"):
        await commit_work(
            session_id=session_id,
            producer_key=producer_key,
            start_ms=0,
            end_ms=1000,
            payload=b"different retry bytes",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_start_ms", "retry_content_type"),
    [
        (1, "audio/wav"),
        (0, "audio/ogg"),
    ],
)
async def test_crash_residue_rejects_changed_metadata_even_when_bytes_match(
    client: AsyncClient,
    retry_start_ms: int,
    retry_content_type: str,
) -> None:
    session_id = await create_session(client, f"writer-utterance-meta-{uuid4().hex[:8]}")
    producer_key = "metadata-conflict"
    work_id = utterance_work_id(session_id, producer_key)
    payload = b"same durable bytes"
    storage = FilesystemAudioStorage(get_settings().audio_storage_path)
    storage.commit_utterance_bytes(
        session_id=session_id,
        work_id=work_id,
        producer_key=producer_key,
        start_ms=0,
        end_ms=1000,
        content_type="audio/wav",
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    with pytest.raises(UtteranceWorkConflict, match="durable storage evidence"):
        await commit_work(
            session_id=session_id,
            producer_key=producer_key,
            start_ms=retry_start_ms,
            end_ms=1000,
            content_type=retry_content_type,
            payload=payload,
        )


@pytest.mark.asyncio
async def test_concurrent_same_session_commits_allocate_unique_sequences(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-utterance-concurrent-0001")

    async def commit_one(key: str, start_ms: int) -> int:
        work, _ = await commit_work(
            session_id=session_id,
            producer_key=key,
            start_ms=start_ms,
            end_ms=start_ms + 800,
            payload=f"audio-{key}".encode(),
        )
        return work.sequence

    sequences = await asyncio.gather(
        commit_one("parallel-a", 0),
        commit_one("parallel-b", 1000),
    )
    assert sorted(sequences) == [1, 2]


@pytest.mark.asyncio
async def test_same_producer_key_is_isolated_across_sessions(client: AsyncClient) -> None:
    first_id = await create_session(client, "writer-utterance-isolated-0001")
    second_id = await create_session(client, "writer-utterance-isolated-0002")

    first, _ = await commit_work(
        session_id=first_id,
        producer_key="same-key",
        start_ms=0,
        end_ms=1000,
        payload=b"meeting-a",
    )
    second, _ = await commit_work(
        session_id=second_id,
        producer_key="same-key",
        start_ms=0,
        end_ms=1000,
        payload=b"meeting-b",
    )

    assert first.sequence == second.sequence == 1
    assert first.id != second.id
    assert first.storage_key != second.storage_key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer_key", "start_ms", "end_ms", "content_type", "payload", "match"),
    [
        ("", 0, 1000, "audio/wav", b"audio", "producer key"),
        ("valid", -1, 1000, "audio/wav", b"audio", "timing"),
        ("valid", 1000, 1000, "audio/wav", b"audio", "timing"),
        ("valid", 0, 1000, "   ", b"audio", "content type"),
        ("valid", 0, 1000, "audio/wav", b"", "payload"),
    ],
)
async def test_invalid_utterance_payload_is_rejected(
    client: AsyncClient,
    producer_key: str,
    start_ms: int,
    end_ms: int,
    content_type: str,
    payload: bytes,
    match: str,
) -> None:
    session_id = await create_session(client, f"writer-utterance-invalid-{uuid4().hex[:8]}")
    with pytest.raises(UtteranceWorkConflict, match=match):
        await commit_work(
            session_id=session_id,
            producer_key=producer_key,
            start_ms=start_ms,
            end_ms=end_ms,
            content_type=content_type,
            payload=payload,
        )


@pytest.mark.asyncio
async def test_missing_session_utterance_read_returns_not_found(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/sessions/{uuid4()}/utterances")
    assert response.status_code == 404
    assert response.json()["detail"] == "recording session not found"
