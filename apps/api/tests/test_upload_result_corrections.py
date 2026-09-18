from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
)
from recantor.models import (
    RecordingSession,
    SessionState,
    TranscriptSegment,
    UploadMediaProcessing,
    UploadProcessingState,
)
from recantor.stt import STTErrorCategory, STTProviderError, STTRequest, STTResult
from recantor.stt_jobs import STTExecutionStatus, execute_stt_job
from recantor.uploads import create_upload_session
from recantor.utterance import commit_utterance_work

TOKEN = "Q" * 43
EARLIER_AUDIO = b"durable-earlier-upload-audio"
LATER_AUDIO = b"durable-later-upload-audio"


class RetryEarlierProvider:
    def __init__(self) -> None:
        self.earlier_calls = 0
        self.later_calls = 0

    async def transcribe(self, request: STTRequest) -> STTResult:
        if request.audio == EARLIER_AUDIO:
            self.earlier_calls += 1
            if self.earlier_calls == 1:
                raise STTProviderError(STTErrorCategory.TRANSIENT, "retry earlier fixture")
            return STTResult(text="earlier A", language="id")
        if request.audio == LATER_AUDIO:
            self.later_calls += 1
            return STTResult(text="later B", language="id")
        raise AssertionError("unexpected correction fixture audio")


async def _waiting_upload() -> UUID:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as db:
        session, record = await create_upload_session(
            db,
            client_request_id=uuid4(),
            capability_token=TOKEN,
            original_filename="timeline.wav",
            content_type="audio/wav",
            byte_length=4096,
            duration_ms=2000,
        )
        session.state = SessionState.UPLOADED.value
        record.received_bytes = record.declared_byte_length
        record.storage_key = f"uploads/tus/{session.id.hex}"
        record.sha256 = "a" * 64
        record.byte_length = record.declared_byte_length
        record.completed_at = now
        db.add(
            UploadMediaProcessing(
                session_id=session.id,
                source_storage_key=record.storage_key,
                source_sha256=record.sha256,
                source_byte_length=record.byte_length,
                source_completed_at=now,
                state=UploadProcessingState.WAITING_STT.value,
                normalization_spec_id=NORMALIZATION_SPEC_ID,
                segmentation_spec_id=SEGMENTATION_SPEC_ID,
                segmentation_params_json=SEGMENTATION_PARAMS_JSON,
                expected_utterance_count=2,
            )
        )
        await db.commit()
        return session.id


def _headers() -> dict[str, str]:
    return {"X-Recantor-Upload-Token": TOKEN}


@pytest.mark.asyncio
async def test_upload_timeline_page_and_exports_survive_retry_out_of_order_completion(
    clean_recording_state,
):
    del clean_recording_state
    session_id = await _waiting_upload()

    async with get_sessionmaker()() as db:
        earlier, _ = await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key=upload_utterance_producer_key(1),
            start_ms=100,
            end_ms=500,
            content_type="audio/wav",
            payload=EARLIER_AUDIO,
        )
        later, _ = await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key=upload_utterance_producer_key(2),
            start_ms=1000,
            end_ms=1400,
            content_type="audio/wav",
            payload=LATER_AUDIO,
        )

    provider = RetryEarlierProvider()
    start = datetime.now(UTC)
    first_earlier = await execute_stt_job(
        utterance_id=earlier.id,
        provider=provider,
        now=start,
    )
    assert first_earlier.status == STTExecutionStatus.RETRY_SCHEDULED
    assert first_earlier.next_attempt_at is not None

    later_result = await execute_stt_job(
        utterance_id=later.id,
        provider=provider,
        now=start + timedelta(milliseconds=1),
    )
    assert later_result.status == STTExecutionStatus.SUCCEEDED

    # Upload pagination is deliberately unavailable while the canonical set can still gain an
    # earlier timeline segment; callers keep polling result status instead of receiving a cursor
    # that could skip A when its retry succeeds.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        in_progress = await client.get(
            f"/api/v1/uploads/{session_id}/transcript?limit=1",
            headers=_headers(),
        )
        assert in_progress.status_code == 409

    earlier_result = await execute_stt_job(
        utterance_id=earlier.id,
        provider=provider,
        now=first_earlier.next_attempt_at + timedelta(milliseconds=1),
    )
    assert earlier_result.status == STTExecutionStatus.SUCCEEDED
    assert provider.earlier_calls == 2
    assert provider.later_calls == 1

    async with get_sessionmaker()() as db:
        publication_order = list(
            (
                await db.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.session_id == session_id)
                    .order_by(TranscriptSegment.sequence)
                )
            ).all()
        )
    assert [(segment.sequence, segment.text) for segment in publication_order] == [
        (1, "later B"),
        (2, "earlier A"),
    ]

    reconciled = await reconcile_upload_processing()
    assert reconciled.stt_succeeded == 1

    async with get_sessionmaker()() as db:
        processing = await db.get(UploadMediaProcessing, session_id)
        session = await db.get(RecordingSession, session_id)
        assert processing is not None
        assert processing.state == UploadProcessingState.SUCCEEDED.value
        assert session is not None and session.state == SessionState.UPLOADED.value

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first_page_response = await client.get(
            f"/api/v1/uploads/{session_id}/transcript?limit=1",
            headers=_headers(),
        )
        assert first_page_response.status_code == 200
        first_page = first_page_response.json()
        assert [segment["text"] for segment in first_page["segments"]] == ["earlier A"]
        assert first_page["has_more"] is True
        assert first_page["next_after_sequence"] == 2

        second_page_response = await client.get(
            (
                f"/api/v1/uploads/{session_id}/transcript"
                f"?limit=1&after_sequence={first_page['next_after_sequence']}"
            ),
            headers=_headers(),
        )
        assert second_page_response.status_code == 200
        second_page = second_page_response.json()
        assert [segment["text"] for segment in second_page["segments"]] == ["later B"]
        assert second_page["has_more"] is False
        assert second_page["next_after_sequence"] == 1

        repeated_first = await client.get(
            f"/api/v1/uploads/{session_id}/transcript?limit=1",
            headers=_headers(),
        )
        assert repeated_first.content == first_page_response.content

        exports: dict[str, bytes] = {}
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
            exports[export_format] = first.content

    assert exports["txt"] == b"earlier A\nlater B\n"
    structured = json.loads(exports["json"])
    assert [(segment["sequence"], segment["text"]) for segment in structured["segments"]] == [
        (2, "earlier A"),
        (1, "later B"),
    ]
    assert [(segment["start_ms"], segment["end_ms"]) for segment in structured["segments"]] == [
        (100, 500),
        (1000, 1400),
    ]
    vtt = exports["vtt"].decode()
    assert vtt.index("00:00:00.100 --> 00:00:00.500") < vtt.index(
        "00:00:01.000 --> 00:00:01.400"
    )
    srt = exports["srt"].decode()
    assert srt.startswith("1\n00:00:00,100 --> 00:00:00,500\nearlier A")
    assert "2\n00:00:01,000 --> 00:00:01,400\nlater B" in srt
