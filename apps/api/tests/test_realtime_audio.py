import struct
import wave
from io import BytesIO
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.realtime_audio import (
    EnergyEndpointDetector,
    PcmFrame,
    RealtimeAudioProtocolError,
    VadConfig,
    encode_pcm_wav,
    parse_pcm_packet,
    sample_to_timeline_ms,
)
from recantor.utterance import UtteranceWorkConflict, commit_utterance_work


SAMPLE_RATE = 8_000
FRAME_SAMPLES = 160


def pcm_frame(sample_offset: int, amplitude: int) -> PcmFrame:
    pcm = struct.pack(f"<{FRAME_SAMPLES}h", *([amplitude] * FRAME_SAMPLES))
    return PcmFrame(sample_offset=sample_offset, pcm=pcm)


def packet(sample_offset: int, amplitude: int) -> bytes:
    frame = pcm_frame(sample_offset, amplitude)
    return struct.pack("<Q", sample_offset) + frame.pcm


def recovery_token(writer_id: str) -> str:
    return f"recantor-recovery-capability::{writer_id}::realtime"


@pytest_asyncio.fixture
async def client(clean_recording_state) -> AsyncClient:
    del clean_recording_state
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


async def create_session(client: AsyncClient, writer_id: str) -> tuple[UUID, str]:
    token = recovery_token(writer_id)
    response = await client.post(
        "/api/v1/sessions/live",
        json={
            "client_request_id": str(uuid4()),
            "writer_id": writer_id,
            "recovery_token": token,
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"]), token


def test_pcm_packet_preserves_uint64_offset_and_s16_samples() -> None:
    sample_offset = 2**32 + 123
    parsed = parse_pcm_packet(packet(sample_offset, 1234))
    assert parsed.sample_offset == sample_offset
    assert parsed.sample_count == FRAME_SAMPLES
    assert struct.unpack_from("<h", parsed.pcm, 0)[0] == 1234

    with pytest.raises(RealtimeAudioProtocolError, match="too short"):
        parse_pcm_packet(b"tiny")
    with pytest.raises(RealtimeAudioProtocolError, match="whole samples"):
        parse_pcm_packet(struct.pack("<Q", 0) + b"\x00")


def test_vad_commits_voiced_interval_after_silence_with_preroll() -> None:
    detector = EnergyEndpointDetector(
        SAMPLE_RATE,
        VadConfig(
            pre_roll_ms=40,
            min_voiced_ms=40,
            silence_ms=60,
            hard_max_ms=500,
            absolute_threshold_dbfs=-45,
        ),
    )
    candidates = []
    offset = 0
    for amplitude in [0, 0, 12_000, 12_000, 12_000, 0, 0, 0]:
        candidates.extend(detector.feed(pcm_frame(offset, amplitude)))
        offset += FRAME_SAMPLES

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.start_sample == 0
    assert candidate.end_sample == offset
    assert candidate.voiced_samples == FRAME_SAMPLES * 3
    assert len(candidate.pcm) == (candidate.end_sample - candidate.start_sample) * 2


def test_vad_discards_too_short_speech() -> None:
    detector = EnergyEndpointDetector(
        SAMPLE_RATE,
        VadConfig(
            pre_roll_ms=20,
            min_voiced_ms=80,
            silence_ms=40,
            hard_max_ms=500,
            absolute_threshold_dbfs=-45,
        ),
    )
    candidates = []
    offset = 0
    for amplitude in [0, 12_000, 0, 0]:
        candidates.extend(detector.feed(pcm_frame(offset, amplitude)))
        offset += FRAME_SAMPLES
    assert candidates == []
    assert detector.active is False


def test_vad_hard_max_splits_long_speech_into_bounded_work() -> None:
    detector = EnergyEndpointDetector(
        SAMPLE_RATE,
        VadConfig(
            pre_roll_ms=0,
            min_voiced_ms=20,
            silence_ms=500,
            hard_max_ms=100,
            absolute_threshold_dbfs=-45,
        ),
    )
    candidates = []
    offset = 0
    for _ in range(10):
        candidates.extend(detector.feed(pcm_frame(offset, 12_000)))
        offset += FRAME_SAMPLES

    assert len(candidates) == 2
    assert [(item.start_sample, item.end_sample) for item in candidates] == [(0, 800), (800, 1600)]


def test_detector_reset_prevents_utterance_across_sample_discontinuity() -> None:
    detector = EnergyEndpointDetector(
        SAMPLE_RATE,
        VadConfig(
            pre_roll_ms=0,
            min_voiced_ms=20,
            silence_ms=40,
            hard_max_ms=500,
            absolute_threshold_dbfs=-45,
        ),
    )
    assert detector.feed(pcm_frame(0, 12_000)) == []
    assert detector.active is True
    detector.reset()
    assert detector.feed(pcm_frame(10_000, 0)) == []
    assert detector.feed(pcm_frame(10_160, 0)) == []
    assert detector.active is False


def test_pcm_wav_is_independently_decodable_and_timeline_math_is_sample_based() -> None:
    raw_pcm = pcm_frame(0, 7_000).pcm + pcm_frame(FRAME_SAMPLES, -7_000).pcm
    encoded = encode_pcm_wav(raw_pcm, sample_rate=SAMPLE_RATE)
    with wave.open(BytesIO(encoded), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == SAMPLE_RATE
        assert audio.getnframes() == FRAME_SAMPLES * 2
        assert audio.readframes(audio.getnframes()) == raw_pcm

    assert sample_to_timeline_ms(2_000, SAMPLE_RATE, SAMPLE_RATE) == 3_000


@pytest.mark.asyncio
async def test_live_utterance_commit_is_fenced_after_capture_takeover(client: AsyncClient) -> None:
    writer_id = "writer-realtime-fence-0001"
    session_id, token = await create_session(client, writer_id)
    wav = encode_pcm_wav(pcm_frame(0, 8_000).pcm, sample_rate=SAMPLE_RATE)

    async with get_sessionmaker()() as db:
        first, _ = await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key="live:1:0:0:160",
            start_ms=0,
            end_ms=20,
            content_type="audio/wav",
            payload=wav,
            expected_writer_id=writer_id,
            expected_capture_epoch=1,
        )
    assert first.sequence == 1

    new_writer_id = "writer-realtime-fence-0002"
    claim = await client.post(
        f"/api/v1/sessions/{session_id}/capture/claim",
        json={
            "writer_id": new_writer_id,
            "expected_epoch": 1,
            "recovery_token": token,
        },
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["capture_epoch"] == 2

    async with get_sessionmaker()() as db:
        with pytest.raises(UtteranceWorkConflict, match="no longer active"):
            await commit_utterance_work(
                db,
                session_id=session_id,
                producer_key="live:1:0:160:320",
                start_ms=20,
                end_ms=40,
                content_type="audio/wav",
                payload=wav,
                expected_writer_id=writer_id,
                expected_capture_epoch=1,
            )
