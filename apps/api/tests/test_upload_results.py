from __future__ import annotations

import hashlib
import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.media_processing import reconcile_upload_processing, upload_utterance_producer_key
from recantor.media_spec import (
    NORMALIZATION_SPEC_ID,
    SEGMENTATION_PARAMS_JSON,
    SEGMENTATION_SPEC_ID,
    UPLOAD_SAMPLE_RATE,
)
from recantor.models import (
    RecordingSession,
    SessionState,
    TranscriptSegment,
    UploadMediaProcessing,
    UploadProcessingState,
    UploadRecord,
)
from recantor.realtime_audio import encode_pcm_wav
from recantor.settings import get_settings
from recantor.stt import STTRequest, STTResult
from recantor.stt_jobs import STTExecutionStatus, execute_stt_job
from recantor.uploads import create_upload_session
from recantor.utterance import commit_utterance_work

TOKEN_A = "Q" * 43
TOKEN_B = "R" * 43


class DeterministicUploadProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, request: STTRequest) -> STTResult:
        assert request.audio
        self.calls += 1
        return STTResult(text=f"injected upload transcript {self.calls}", language="id")


def _tone_wav(milliseconds: int = 400) -> bytes:
    samples = round(UPLOAD_SAMPLE_RATE * milliseconds / 1000)
    pcm = b"".join(struct.pack("<h", 9000 if index % 2 == 0 else -9000) for index in range(samples))
    return encode_pcm_wav(pcm, sample_rate=UPLOAD_SAMPLE_RATE)


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
            outcome_code=(
                "transcribed"
                if processing_state == UploadProcessingState.SUCCEEDED.value and segment_count
                else None
            ),
            completed_at=(
                now
                if processing_state
                in {
                    UploadProcessingState.SUCCEEDED.value,
                    UploadProcessingState.FAILED.value,
                }
                else None
            ),
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


async def _physical_completed_upload(payload: bytes) -> UUID:
    now = datetime.now(UTC)
    settings = get_settings()
    async with get_sessionmaker()() as db:
        session, record = await create_upload_session(
            db,
            client_request_id=uuid4(),
            capability_token=TOKEN_A,
            original_filename="provider-fixture.wav",
            content_type="audio/wav",
            byte_length=len(payload),
            duration_ms=400,
        )
        root = Path(settings.audio_storage_path)
        source_path = root / settings.upload_tus_storage_prefix / f"issue46-{session.id.hex}"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()

        session.state = SessionState.UPLOADED.value
        record.received_bytes = len(payload)
        record.storage_key = source_path.relative_to(root).as_posix()
        record.sha256 = digest
        record.byte_length = len(payload)
        record.completed_at = now
        db.add(
            UploadMediaProcessing(
                session_id=session.id,
                source_storage_key=record.storage_key,
                source_sha256=digest,
                source_byte_length=len(payload),
                source_completed_at=now,
                state=UploadProcessingState.PENDING.value,
                normalization_spec_id=NORMALIZATION_SPEC_ID,
                segmentation_spec_id=SEGMENTATION_SPEC_ID,
                segmentation_params_json=SEGMENTATION_PARAMS_JSON,
            )
        )
        await db.commit()
        return session.id


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _headers(token: str = TOKEN_A) -> dict[str, str]:
    return {"X-Recantor-Upload-Token": token}


@pytest.mark.asyncio
async def test_result_surfaces_enforce_capability_expiry_and_cross_session_isolation(
    clean_recording_state,
):
    del clean_recording_state
    session_a = await _completed_upload(
        processing_state=UploadProcessingState.SUCCEEDED.value,
        segment_count=1,
    )
    session_b = await _completed_upload(
        token=TOKEN_B,
        processing_state=UploadProcessingState.SUCCEEDED.value,
        segment_count=1,
    )

    async with _client() as client:
        for suffix in ("result", "transcript", "exports/txt"):
            wrong = await client.get(
                f"/api/v1/uploads/{session_a}/{suffix}",
                headers=_headers(TOKEN_B),
            )
            assert wrong.status_code == 403
            isolated = await client.get(
                f"/api/v1/uploads/{session_b}/{suffix}",
                headers=_headers(TOKEN_A),
            )
            assert isolated.status_code == 403

    async with get_sessionmaker()() as db:
        record = await db.get(UploadRecord, session_a)
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    async with _client() as client:
        for suffix in ("result", "transcript", "exports/json"):
            expired = await client.get(
                f"/api/v1/uploads/{session_a}/{suffix}",
                headers=_headers(),
            )
            assert expired.status_code == 410

    async with get_sessionmaker()() as db:
        assert await db.get(RecordingSession, session_a) is not None
        assert await db.get(UploadRecord, session_a) is not None
        assert await db.get(UploadMediaProcessing, session_a) is not None
        segment_id = await db.scalar(
            select(TranscriptSegment.id).where(TranscriptSegment.session_id == session_a)
        )
        assert segment_id is not None


@pytest.mark.asyncio
async def test_result_status_is_postgres_derived_and_failure_is_safe(clean_recording_state):
    del clean_recording_state
    session_id = await _completed_upload()

    async with _client() as client:
        preparing = await client.get(
            f"/api/v1/uploads/{session_id}/result",
            headers=_headers(),
        )
        assert preparing.status_code == 200
        assert preparing.json()["state"] == "preparing"

        async with get_sessionmaker()() as db:
            processing = await db.get(UploadMediaProcessing, session_id)
            assert processing is not None
            processing.state = UploadProcessingState.WAITING_STT.value
            processing.expected_utterance_count = 1
            await db.commit()
        transcribing = await client.get(
            f"/api/v1/uploads/{session_id}/result",
            headers=_headers(),
        )
        assert transcribing.json()["state"] == "transcribing"

        async with get_sessionmaker()() as db:
            processing = await db.get(UploadMediaProcessing, session_id)
            assert processing is not None
            processing.state = UploadProcessingState.SUCCEEDED.value
            processing.outcome_code = "no_speech"
            processing.completed_at = datetime.now(UTC)
            await db.commit()
        empty = await client.get(
            f"/api/v1/uploads/{session_id}/result",
            headers=_headers(),
        )
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
        failed = await client.get(
            f"/api/v1/uploads/{session_id}/result",
            headers=_headers(),
        )
        payload = failed.json()
        assert payload["state"] == "failed"
        assert payload["exports_available"] is False
        assert "secret" not in payload["failure_message"]
        blocked = await client.get(
            f"/api/v1/uploads/{session_id}/exports/txt",
            headers=_headers(),
        )
        assert blocked.status_code == 409


@pytest.mark.asyncio
async def test_uploaded_fixture_reaches_canonical_result_via_injected_provider(
    clean_recording_state,
):
    del clean_recording_state
    payload = _tone_wav()
    session_id = await _physical_completed_upload(payload)

    async with get_sessionmaker()() as db, db.begin():
        processing = await db.get(UploadMediaProcessing, session_id)
        assert processing is not None
        processing.state = UploadProcessingState.WAITING_STT.value
        processing.expected_utterance_count = 1

    async with get_sessionmaker()() as db:
        utterance, _ = await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key=upload_utterance_producer_key(1),
            start_ms=0,
            end_ms=400,
            content_type="audio/wav",
            payload=payload,
        )

    provider = DeterministicUploadProvider()
    execution = await execute_stt_job(utterance_id=utterance.id, provider=provider)
    assert execution.status == STTExecutionStatus.SUCCEEDED
    assert provider.calls == 1
    reconciled = await reconcile_upload_processing()
    assert reconciled.stt_succeeded == 1

    async with _client() as client:
        result = await client.get(
            f"/api/v1/uploads/{session_id}/result",
            headers=_headers(),
        )
        assert result.status_code == 200
        assert result.json()["state"] == "complete"
        assert result.json()["transcript_segment_count"] == 1

        transcript = await client.get(
            f"/api/v1/uploads/{session_id}/transcript",
            headers=_headers(),
        )
        assert transcript.status_code == 200
        segments = transcript.json()["segments"]
        assert [segment["text"] for segment in segments] == ["injected upload transcript 1"]


@pytest.mark.asyncio
async def test_transcript_page_and_all_exports_are_deterministic_and_structurally_valid(
    clean_recording_state,
):
    del clean_recording_state
    session_id = await _completed_upload(
        processing_state=UploadProcessingState.SUCCEEDED.value,
        segment_count=2,
    )

    async with _client() as client:
        status_response = await client.get(
            f"/api/v1/uploads/{session_id}/result",
            headers=_headers(),
        )
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
    assert set(structured["segments"][0]) == {
        "sequence",
        "start_ms",
        "end_ms",
        "text",
        "language",
    }
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

    async with _client() as client:
        result = await client.get(
            f"/api/v1/uploads/{session_id}/result",
            headers=_headers(),
        )
        assert result.json()["state"] == "no_speech"
        transcript = await client.get(
            f"/api/v1/uploads/{session_id}/transcript",
            headers=_headers(),
        )
        assert transcript.json()["segments"] == []
        expected = {
            "txt": b"",
            "json": json.dumps(
                {"session_id": str(session_id), "segments": []},
                separators=(",", ":"),
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

    async with _client() as client:
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

        exported = await client.get(
            f"/api/v1/uploads/{session_id}/exports/txt",
            headers=_headers(),
        )
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
