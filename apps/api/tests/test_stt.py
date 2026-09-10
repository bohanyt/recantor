import urllib.error
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.models import RecordingSession, TranscriptSegment
from recantor.settings import get_settings
from recantor.stt import (
    GroqSTTProvider,
    STTErrorCategory,
    STTProviderError,
    STTRequest,
    STTResult,
    STTSourceError,
    transcribe_utterance,
)
from recantor.utterance import commit_utterance_work, transcript_producer_key_for_utterance


class FakeProvider:
    def __init__(self, result: STTResult):
        self.result = result
        self.requests: list[STTRequest] = []

    async def transcribe(self, request: STTRequest) -> STTResult:
        self.requests.append(request)
        return self.result


class FailingProvider:
    def __init__(self, error: STTProviderError):
        self.error = error
        self.calls = 0

    async def transcribe(self, request: STTRequest) -> STTResult:
        del request
        self.calls += 1
        raise self.error


def recovery_token(writer_id: str) -> str:
    return f"recantor-recovery-capability::{writer_id}::stt"


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


async def create_work(
    *,
    session_id: UUID,
    producer_key: str = "live:1:0:0:48000",
    start_ms: int = 100,
    end_ms: int = 1100,
    payload: bytes = b"durable utterance bytes",
):
    async with get_sessionmaker()() as db:
        work, _ = await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key=producer_key,
            start_ms=start_ms,
            end_ms=end_ms,
            content_type="audio/wav",
            payload=payload,
        )
    return work


@pytest.mark.asyncio
async def test_executor_reads_durable_work_and_commits_one_canonical_segment(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-stt-success-0001")
    payload = b"exact durable utterance payload"
    work = await create_work(session_id=session_id, payload=payload)
    provider = FakeProvider(STTResult(text="  Halo dunia  ", language="ID"))

    segment, idempotent = await transcribe_utterance(
        session_id=session_id,
        work_id=work.id,
        provider=provider,
        language="id",
        prompt="nama produk Recantor",
    )

    assert idempotent is False
    assert segment.producer_key == transcript_producer_key_for_utterance(work.id)
    assert segment.start_ms == work.start_ms
    assert segment.end_ms == work.end_ms
    assert segment.text == "Halo dunia"
    assert segment.language == "id"
    assert len(provider.requests) == 1
    assert provider.requests[0].audio == payload
    assert provider.requests[0].filename == "utterance.wav"
    assert provider.requests[0].content_type == "audio/wav"
    assert provider.requests[0].language == "id"

    retry_provider = FailingProvider(
        STTProviderError(
            STTErrorCategory.TRANSIENT,
            "provider must not be called on committed retry",
        )
    )
    retry, retry_idempotent = await transcribe_utterance(
        session_id=session_id,
        work_id=work.id,
        provider=retry_provider,
    )
    assert retry_idempotent is True
    assert retry.id == segment.id
    assert retry_provider.calls == 0

    async with get_sessionmaker()() as db:
        count = await db.scalar(
            select(func.count(TranscriptSegment.id)).where(
                TranscriptSegment.session_id == session_id
            )
        )
    assert count == 1


@pytest.mark.asyncio
async def test_blank_provider_text_is_not_committed(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-stt-blank-0001")
    work = await create_work(session_id=session_id)
    provider = FakeProvider(STTResult(text="   "))

    with pytest.raises(STTProviderError) as exc_info:
        await transcribe_utterance(session_id=session_id, work_id=work.id, provider=provider)
    assert exc_info.value.category == STTErrorCategory.MALFORMED_RESPONSE

    async with get_sessionmaker()() as db:
        count = await db.scalar(
            select(func.count(TranscriptSegment.id)).where(
                TranscriptSegment.session_id == session_id
            )
        )
    assert count == 0


@pytest.mark.asyncio
async def test_corrupt_durable_media_fails_before_provider_call(client: AsyncClient) -> None:
    session_id = await create_session(client, "writer-stt-corrupt-0001")
    work = await create_work(session_id=session_id, payload=b"original bytes")
    media_path = Path(get_settings().audio_storage_path) / work.storage_key
    media_path.write_bytes(b"tampered bytes")
    provider = FakeProvider(STTResult(text="should never run"))

    with pytest.raises(STTSourceError, match="failed verification"):
        await transcribe_utterance(session_id=session_id, work_id=work.id, provider=provider)
    assert provider.requests == []


@pytest.mark.asyncio
async def test_provider_failure_leaves_session_and_canonical_transcript_untouched(
    client: AsyncClient,
) -> None:
    session_id = await create_session(client, "writer-stt-provider-fail-0001")
    work = await create_work(session_id=session_id)
    provider = FailingProvider(STTProviderError(STTErrorCategory.RATE_LIMIT, "rate limited"))

    with pytest.raises(STTProviderError) as exc_info:
        await transcribe_utterance(session_id=session_id, work_id=work.id, provider=provider)
    assert exc_info.value.category == STTErrorCategory.RATE_LIMIT

    async with get_sessionmaker()() as db:
        session = await db.scalar(select(RecordingSession).where(RecordingSession.id == session_id))
        count = await db.scalar(
            select(func.count(TranscriptSegment.id)).where(
                TranscriptSegment.session_id == session_id
            )
        )
    assert session is not None
    assert session.state == "recording"
    assert count == 0


class FakeHTTPResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def read(self, amount: int) -> bytes:
        return self.payload[:amount]


@pytest.mark.asyncio
async def test_groq_adapter_builds_expected_multipart_request(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeHTTPResponse(b'{"text":" hasil groq ","language":"id"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = GroqSTTProvider(
        api_key="secret-test-key",
        endpoint="https://api.groq.com/openai/v1/audio/transcriptions",
        model="whisper-large-v3-turbo",
        timeout_seconds=12.5,
    )
    result = await provider.transcribe(
        STTRequest(
            audio=b"RIFF-test-audio",
            filename="utterance.wav",
            content_type="audio/wav",
            language="ID",
            prompt="Recantor",
        )
    )

    request = captured["request"]
    body = request.data
    assert request.full_url == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert request.get_header("Authorization") == "Bearer secret-test-key"
    assert request.get_header("Content-type").startswith("multipart/form-data; boundary=")
    assert request.get_header("User-agent") == "Recantor/0.1 (+https://github.com/bohanyt/recantor)"
    assert captured["timeout"] == 12.5
    assert b'name="model"' in body and b"whisper-large-v3-turbo" in body
    assert b'name="response_format"' in body and b"json" in body
    assert b'name="temperature"' in body and b"0" in body
    assert b'name="language"' in body and b"id" in body
    assert b'name="prompt"' in body and b"Recantor" in body
    assert b'name="file"; filename="utterance.wav"' in body
    assert b"RIFF-test-audio" in body
    assert result == STTResult(text="hasil groq", language="id")


@pytest.mark.asyncio
async def test_groq_adapter_classifies_rate_limit_and_malformed_response(monkeypatch) -> None:
    provider = GroqSTTProvider(
        api_key="secret-test-key",
        endpoint="https://api.groq.com/openai/v1/audio/transcriptions",
        model="whisper-large-v3-turbo",
        timeout_seconds=10,
    )
    request = STTRequest(audio=b"audio", filename="utterance.wav", content_type="audio/wav")

    def rate_limited(*args, **kwargs):
        del args, kwargs
        raise urllib.error.HTTPError(provider.endpoint, 429, "rate limit", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", rate_limited)
    with pytest.raises(STTProviderError) as rate_exc:
        await provider.transcribe(request)
    assert rate_exc.value.category == STTErrorCategory.RATE_LIMIT

    monkeypatch.setattr(provider, "_request_sync", lambda value: b'{"x_groq":{"id":"req"}}')
    with pytest.raises(STTProviderError) as malformed_exc:
        await provider.transcribe(request)
    assert malformed_exc.value.category == STTErrorCategory.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_groq_adapter_requires_server_side_key() -> None:
    provider = GroqSTTProvider(
        api_key=None,
        endpoint="https://api.groq.com/openai/v1/audio/transcriptions",
        model="whisper-large-v3-turbo",
        timeout_seconds=10,
    )
    with pytest.raises(STTProviderError) as exc_info:
        await provider.transcribe(
            STTRequest(audio=b"audio", filename="utterance.wav", content_type="audio/wav")
        )
    assert exc_info.value.category == STTErrorCategory.CONFIGURATION
