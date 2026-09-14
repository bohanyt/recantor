from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.media_spec import NORMALIZATION_SPEC_ID, SEGMENTATION_PARAMS_JSON, SEGMENTATION_SPEC_ID
from recantor.models import (
    RecordingSession,
    SessionState,
    TranscriptSegment,
    UploadMediaProcessing,
    UploadProcessingState,
    UploadRecord,
)
from recantor.uploads import create_upload_session

TOKEN_A = "Q" * 43
TOKEN_B = "R" * 43


async def _completed_upload(
    *,
    token: str = TOKEN_A,
    processing_state: str = UploadProcessingState.PENDING.value,
    segment_count: int = 0,
) -> UUID:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as db:
        session, record = await create_upload_session(
            db,
            client_request_id=uuid4(),
            capability_token=token,
            original_filename="result.wav",
            content_type="audio/wav",
            byte_length=4096,
            duration_ms=3000,
        )
        session.state = SessionState.UPLOADED.value
        record.received_bytes = record.declared_byte_length
        record.storage_key = f"uploads/tus/{session.id.hex}"
        record.sha256 = "a" * 64
        record.byte_length = record.declared_byte_length
        record.completed_at = now
        processing = UploadMediaProcessing(
            session_id=session.id,
            source_storage_key=record.storage_key,
            source_sha256=record.sha256,
            source_byte_length=record.byte_length,
            source_completed_at=now,
            state=processing_state,
            normalization_spec_id=NORMALIZATION_SPEC_ID,
            segmentation_spec_id=SEGMENTATION_SPEC_ID,
            segmentation_params_json=SEGMENTATION_PARAMS_JSON,
            expected_utterance_count=segment_count,
            outcome_code="transcribed"
            if processing_state == UploadProcessingState.SUCCEEDED.value and segment_count
            else None,
            completed_at=now
            if processing_state in {
                UploadProcessingState.SUCCEEDED.value,
                UploadProcessingState.FAILED.value,
            }
            else None,
        )
        db.add(processing)
        for index in range(segment_count):
            sequence = index + 1
            db.add(
                TranscriptSegment(
                    session_id=session.id,
                    sequence=sequence,
                    producer_key=f"fixture:{sequence}",
                    start_ms=index * 1000 + 5,
                    end_ms=(index + 1) * 1000 + 6,
                    text=f"segment {sequence}",
                    language="id",
                )
            )
        await db.commit()
        return session.id


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _headers(token: str = TOKEN_A) -> dict[str, str]:
    return {"X-Recantor-Upload-Token": token}


@pytest.mark.asyncio
async def test_result_surfaces_enforce_capability_expiry_and_cross_session_isolation(
    clean_recording_state,
):
    del clean_recording_state
    session_a = await _completed_upload(processing_state=UploadProcessingState.SUCCEEDED.value, segment_count=1)
    session_b = await _completed_upload(
        token=TOKEN_B,
        processing_state=UploadProcessingState.SUCCEEDED.value,
        segment_count=1,
    )

    async with await _client() as client:
        for suffix in ("result", "transcript", "exports/txt"):
            wrong = await client.get(f"/api/v1/uploads/{session_a}/{suffix}", headers=_headers(TOKEN_B))
            assert wrong.status_code == 403
            isolated = await client.get(f"/api/v1/uploads/{session_b}/{suffix}", headers=_headers(TOKEN_A))
            assert isolated.status_code == 403

    async with get_sessionmaker()() as db:
        record = await db.get(UploadRecord, session_a)
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    async with await _client() as client:
        for suffix in ("result", "transcript", "exports/json"):
            expired = await client.get(f"/api/v1/uploads/{session_a}/{suffix}", headers=_headers())
            assert expired.status_code == 410

    async with get_sessionmaker()() as db:
        assert await db.get(RecordingSession, session_a) is not None
        assert await db.get(UploadRecord, session_a) is not None
        assert await db.get(UploadMediaProcessing, session_a) is not None
        segment = await db.get(TranscriptSegment, (await db.scalar(
            __import__("sqlalchemy").select(TranscriptSegment.id).where(
                TranscriptSegment.session_id == session_a
            )
        )))
        assert segment is not None


@pytest.mark.asyncio
async def test_result_status_is_postgres_derived_and_failure_is_safe(clean_recording_state):
    del clean_recording_state
    session_id = await _completed_upload()

    async with await _client() as client:
        preparing = await client.get(f"/api/v1/uploads/{session_id}/result", headers=_headers())
        assert preparing.status_code == 200
        assert preparing.json()["state"] == "preparing"

        async with get_sessionmaker()() as db:
            processing = await db.get(UploadMediaProcessing, session_id)
            assert processing is not None
            processing.state = UploadProcessingState.WAITING_STT.value
            processing.expected_utterance_count = 1
            await db.commit()
        transcribing = await client.get(f"/api/v1/uploads/{session_id}/result", headers=_headers())
        assert transcribing.json()["state"] == "transcribing"

        async with get_sessionmaker()() as db:
            processing = await db.get(UploadMediaProcessing, session_id)
            assert processing is not None
            processing.state = UploadProcessingState.SUCCEEDED.value
            processing.outcome_code = "no_speech"
            processing.completed_at = datetime.now(UTC)
            await db.commit()
        empty = await client.get(f"/api/v1/uploads/{session_id}/result", headers=_headers())
        assert empty.json()["state"] == "no_speech"
        assert empty.json()["exports_available"] is True
        assert empty.json()["transcript_segment_count"] == 0

        async with get_sessionmaker()() as db:
            processing = await db.get(UploadMediaProcessing, session_id)
            assert processing is not None
            processing.state = UploadProcessingState.FAILED.value
            processing.outcome_code = None
            processing.last_error_message = "secret /srv/storage/key provider dump"
            processing.completed_at = datetime.now(UTC)
            await db.commit()
        failed = await client.get(f"/api/v1/uploads/{session_id}/result", headers=_headers())
        payload = failed.json()
        assert payload["state"] == "failed"
        assert payload["exports_available"] is False
        assert "secret" not in payload["failure_message"]
        blocked = await client.get(f"/api/v1/uploads/{session_id}/exports/txt", headers=_headers())
        assert blocked.status_code == 409


@pytest.mark.asyncio
async def test_transcript_page_and_all_exports_are_deterministic_and_structurally_valid(
    clean_recording_state,
):
    del clean_recording_state
    session_id = await _completed_upload(
        processing_state=UploadProcessingState.SUCCEEDED.value,
        segment_count=2,
    )

    async with await _client() as client:
        status_response = await client.get(f"/api/v1/uploads/{session_id}/result", headers=_headers())
        assert status_response.json()["state"] == "complete"
        assert status_response.json()["transcript_segment_count"] == 2

        page = await client.get(
            f"/api/v1/uploads/{session_id}/transcript?limit=1",
            headers=_headers(),
        )
        assert page.status_code == 200
        first_page = page.json()
        assert [segment["sequence"] for segment in first_page["segments"]] == [1]
        assert first_page["has_more"] is True
        assert first_page["next_after_sequence"] == 1

        bodies: dict[str, bytes] = {}
        for export_format in ("txt", "json", "vtt", "srt"):
            first = await client.get(
                f"/api/v1/uploads/{session_id}/exports/{export_format}",
                headers=_headers(),
            )
            second = await client.get(
                f"/api/v1/uploads/{session_id}/exports/{export_format}",
                headers=_headers(),
            )
            assert first.status_code == 200
            assert first.content == second.content
            bodies[export_format] = first.content

    assert bodies["txt"] == b"segment 1\nsegment 2\n"
    structured = json.loads(bodies["json"])
    assert structured["session_id"] == str(session_id)
    assert [segment["sequence"] for segment in structured["segments"]] == [1, 2]
    assert set(structured["segments"][0]) == {"sequence", "start_ms", "end_ms", "text", "language"}
    vtt = bodies["vtt"].decode()
    assert vtt.startswith("WEBVTT\n\n00:00:00.005 --> 00:00:01.006")
    assert "00:00:01.005 --> 00:00:02.006" in vtt
    srt = bodies["srt"].decode()
    assert srt.startswith("1\n00:00:00,005 --> 00:00:01,006")
    assert "2\n00:00:01,005 --> 00:00:02,006" in srt


@pytest.mark.asyncio
async def test_no_speech_has_no_fake_segment_and_empty_exports(clean_recording_state):
    del clean_recording_state
    session_id = await _completed_upload(processing_state=UploadProcessingState.SUCCEEDED.value)
    async with get_sessionmaker()() as db:
        processing = await db.get(UploadMediaProcessing, session_id)
        assert processing is not None
        processing.outcome_code = "no_speech"
        await db.commit()

    async with await _client() as client:
        result = await client.get(f"/api/v1/uploads/{session_id}/result", headers=_headers())
        assert result.json()["state"] == "no_speech"
        transcript = await client.get(f"/api/v1/uploads/{session_id}/transcript", headers=_headers())
        assert transcript.json()["segments"] == []
        expected = {
            "txt": b"",
            "json": json.dumps(
                {"session_id": str(session_id), "segments": []}, separators=(",", ":")
            ).encode()
            + b"\n",
            "vtt": b"WEBVTT\n\n",
            "srt": b"",
        }
        for export_format, body in expected.items():
            response = await client.get(
                f"/api/v1/uploads/{session_id}/exports/{export_format}",
                headers=_headers(),
            )
            assert response.content == body


@pytest.mark.asyncio
async def test_long_transcript_reads_and_exports_cross_bounded_pages(clean_recording_state):
    del clean_recording_state
    session_id = await _completed_upload(
        processing_state=UploadProcessingState.SUCCEEDED.value,
        segment_count=505,
    )

    async with await _client() as client:
        page = await client.get(
            f"/api/v1/uploads/{session_id}/transcript?limit=200",
            headers=_headers(),
        )
        payload = page.json()
        assert len(payload["segments"]) == 200
        assert payload["has_more"] is True
        too_large = await client.get(
            f"/api/v1/uploads/{session_id}/transcript?limit=1001",
            headers=_headers(),
        )
        assert too_large.status_code == 422

        exported = await client.get(f"/api/v1/uploads/{session_id}/exports/txt", headers=_headers())
        assert exported.status_code == 200
        assert len(exported.text.splitlines()) == 505
        assert exported.text.splitlines()[-1] == "segment 505"


def test_openapi_exposes_capability_protected_upload_result_contracts():
    schema = app.openapi()
    for path in (
        "/api/v1/uploads/{session_id}/result",
        "/api/v1/uploads/{session_id}/transcript",
        "/api/v1/uploads/{session_id}/exports/{export_format}",
    ):
        operation = schema["paths"][path]["get"]
        headers = [
            parameter
            for parameter in operation["parameters"]
            if parameter["in"] == "header" and parameter["name"] == "X-Recantor-Upload-Token"
        ]
        assert len(headers) == 1
