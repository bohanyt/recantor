from __future__ import annotations

import asyncio
import hashlib
import math
import shutil
import struct
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from recantor.db import get_sessionmaker
from recantor.media_processing import (
    MediaPermanentError,
    MediaRetryableError,
    _run_process,
    _upload_vad,
    claim_next_upload_processing,
    execute_upload_processing_claim,
    probe_media,
    reconcile_upload_processing,
    upload_utterance_producer_key,
)
from recantor.media_spec import (
    NORMALIZATION_SPEC_ID,
    SEGMENTATION_PARAMS_JSON,
    SEGMENTATION_SPEC_ID,
    UPLOAD_FRAME_SAMPLES,
    UPLOAD_SAMPLE_RATE,
    UPLOAD_SEGMENTATION_SPEC,
)
from recantor.media_storage import FilesystemNormalizedMediaStorage, NormalizedMediaError
from recantor.models import (
    RecordingSession,
    SessionKind,
    SessionState,
    STTJob,
    STTJobState,
    TranscriptionUtterance,
    TranscriptSegment,
    UploadMediaProcessing,
    UploadProcessingState,
    UploadRecord,
)
from recantor.realtime_audio import PcmFrame, encode_pcm_wav
from recantor.settings import get_settings
from recantor.stt import GroqSTTProvider, STTRequest, STTResult
from recantor.stt_jobs import (
    STTExecutionStatus,
    STTWorkloadClass,
    execute_next_reserved_stt_job,
    execute_stt_job,
    reconcile_stt_jobs,
)
from recantor.upload_contracts import TusHookRequest
from recantor.uploads import create_upload_session, get_upload_storage, process_tusd_hook
from recantor.utterance import commit_utterance_work

TOKEN = "N" * 43


class SequenceProvider:
    def __init__(self, results: list[STTResult]):
        self.results = list(results)
        self.calls = 0

    async def transcribe(self, request: STTRequest) -> STTResult:
        assert request.audio
        self.calls += 1
        return self.results.pop(0)


def _tone_pcm(milliseconds: int, *, amplitude: int = 9000) -> bytes:
    samples = round(UPLOAD_SAMPLE_RATE * milliseconds / 1000)
    return b"".join(
        struct.pack("<h", amplitude if index % 2 == 0 else -amplitude) for index in range(samples)
    )


def _silence_pcm(milliseconds: int) -> bytes:
    samples = round(UPLOAD_SAMPLE_RATE * milliseconds / 1000)
    return b"\x00\x00" * samples


def _wav_bytes(*parts: bytes) -> bytes:
    return encode_pcm_wav(b"".join(parts), sample_rate=UPLOAD_SAMPLE_RATE)


async def _create_completed_upload(
    tmp_path: Path,
    payload: bytes,
    *,
    original_filename: str = "fixture.wav",
    content_type: str = "audio/wav",
    add_processing: bool = True,
):
    session_id = uuid4()
    upload_id = uuid4().hex
    settings = get_settings()
    root = Path(settings.audio_storage_path)
    source_path = root / settings.upload_tus_storage_prefix / upload_id
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    now = datetime.now(UTC)
    session = RecordingSession(
        id=session_id,
        client_request_id=uuid4(),
        kind=SessionKind.UPLOAD.value,
        state=SessionState.UPLOADED.value,
        capture_epoch=0,
        created_at=now,
        updated_at=now,
    )
    record = UploadRecord(
        session_id=session_id,
        original_filename=original_filename,
        content_type=content_type,
        declared_byte_length=len(payload),
        received_bytes=len(payload),
        tus_upload_id=upload_id,
        storage_key=source_path.relative_to(root).as_posix(),
        sha256=digest,
        byte_length=len(payload),
        expires_at=now + timedelta(hours=1),
        completed_at=now,
        created_at=now,
        updated_at=now,
    )
    processing = UploadMediaProcessing(
        session_id=session_id,
        source_storage_key=record.storage_key,
        source_sha256=digest,
        source_byte_length=len(payload),
        source_completed_at=now,
        state=UploadProcessingState.PENDING.value,
        normalization_spec_id=NORMALIZATION_SPEC_ID,
        segmentation_spec_id=SEGMENTATION_SPEC_ID,
        segmentation_params_json=SEGMENTATION_PARAMS_JSON,
    )
    async with get_sessionmaker()() as db:
        db.add_all([session, record])
        if add_processing:
            db.add(processing)
        await db.commit()
    return session_id, upload_id, source_path


def _hook(
    hook_type: str,
    *,
    session_id,
    size: int,
    offset: int,
    upload_id: str | None,
) -> TusHookRequest:
    return TusHookRequest.model_validate(
        {
            "Type": hook_type,
            "Event": {
                "Upload": {
                    "ID": upload_id,
                    "Size": size,
                    "SizeIsDeferred": False,
                    "Offset": offset,
                    "MetaData": {
                        "recantor_session_id": str(session_id),
                        "filename": "slow.wav",
                        "filetype": "audio/wav",
                    },
                    "Storage": {},
                },
                "HTTPRequest": {
                    "Method": "POST" if hook_type == "pre-create" else "PATCH",
                    "URI": "/files/" if upload_id is None else f"/files/{upload_id}",
                    "Header": {"X-Recantor-Upload-Token": [TOKEN]},
                },
            },
        }
    )


@pytest.fixture(autouse=True)
def require_ffmpeg():
    assert shutil.which("ffmpeg"), "#45 tests require ffmpeg runtime"
    assert shutil.which("ffprobe"), "#45 tests require ffprobe runtime"


@pytest.mark.asyncio
async def test_d3_a_snapshot_is_exact_and_retry_does_not_read_realtime_tuning(
    clean_recording_state,
):
    del clean_recording_state
    settings = get_settings()
    old = settings.realtime_vad_hard_max_ms
    settings.realtime_vad_hard_max_ms = 1234
    try:
        spec = UPLOAD_SEGMENTATION_SPEC
        assert spec.frame_ms == 20
        assert spec.frame_samples == 320
        assert spec.pre_roll_ms == 200
        assert spec.min_voiced_ms == 160
        assert spec.trailing_silence_ms == 600
        assert spec.hard_max_ms == 180_000
        assert spec.absolute_threshold_dbfs == -50.0
        assert spec.noise_margin_db == 12.0
        assert spec.initial_noise_dbfs == -65.0
        assert spec.noise_alpha == 0.95
        assert "180000" in SEGMENTATION_PARAMS_JSON
        assert _upload_vad().hard_max_samples == 180 * UPLOAD_SAMPLE_RATE
    finally:
        settings.realtime_vad_hard_max_ms = old


def _segment_boundaries(pcm: bytes) -> list[tuple[int, int]]:
    detector = _upload_vad()
    output: list[tuple[int, int]] = []
    sample_offset = 0
    frame_bytes = UPLOAD_FRAME_SAMPLES * 2
    for offset in range(0, len(pcm), frame_bytes):
        chunk = pcm[offset : offset + frame_bytes]
        if not chunk:
            break
        frame = PcmFrame(sample_offset=sample_offset, pcm=chunk)
        sample_offset += frame.sample_count
        output.extend(
            (candidate.start_sample, candidate.end_sample) for candidate in detector.feed(frame)
        )
    candidate = detector.flush()
    if candidate is not None:
        output.append((candidate.start_sample, candidate.end_sample))
    return output


def test_d3_a_segmentation_restarts_from_zero_is_exact_and_silence_safe():
    pcm = b"".join(
        [
            _silence_pcm(400),
            _tone_pcm(500),
            _silence_pcm(800),
            _tone_pcm(700),
            _silence_pcm(700),
        ]
    )
    first = _segment_boundaries(pcm)
    second = _segment_boundaries(pcm)
    assert first == second
    assert len(first) == 2
    assert _segment_boundaries(_silence_pcm(10_000)) == []
    assert _segment_boundaries(_tone_pcm(120)) == []  # EOF obeys the same 160 ms minimum.
    assert len(_segment_boundaries(_tone_pcm(160))) == 1


def test_one_hour_d3_projection_materially_reduces_requests_vs_realtime_8s():
    one_hour_ms = 60 * 60 * 1000
    d3_requests = math.ceil(one_hour_ms / UPLOAD_SEGMENTATION_SPEC.hard_max_ms)
    realtime_requests = math.ceil(one_hour_ms / 8000)
    assert d3_requests == 20
    assert realtime_requests == 450
    assert d3_requests * 20 < realtime_requests


@pytest.mark.asyncio
async def test_probe_supported_formats_and_probe_wins_over_filename_mime(
    clean_recording_state,
    tmp_path: Path,
):
    del clean_recording_state
    seed = tmp_path / "seed.wav"
    seed.write_bytes(_wav_bytes(_tone_pcm(300)))
    formats = {
        "wav": [],
        "mp3": ["-c:a", "libmp3lame"],
        "m4a": ["-c:a", "aac"],
        "ogg": ["-c:a", "libopus"],
        "webm": ["-c:a", "libopus"],
        "mp4": ["-c:a", "aac"],
    }
    for suffix, codec_args in formats.items():
        path = tmp_path / f"fixture.{suffix}"
        if suffix == "wav":
            path.write_bytes(seed.read_bytes())
        else:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(seed),
                    *codec_args,
                    str(path),
                ],
                check=True,
            )
        probe = await probe_media(path)
        assert probe.audio_stream_index >= 0

    # Durable #44 metadata can say MP3 while bytes are WAV. Processing trusts ffprobe bytes.
    session_id, _, _ = await _create_completed_upload(
        tmp_path,
        seed.read_bytes(),
        original_filename="claimed.mp3",
        content_type="audio/mpeg",
    )
    claim = await claim_next_upload_processing()
    assert claim is not None and claim.session_id == session_id
    assert await execute_upload_processing_claim(claim) is True


@pytest.mark.asyncio
async def test_probe_rejects_corrupt_no_audio_multiple_audio_and_unsupported(tmp_path: Path):
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"not-media" * 100)
    with pytest.raises(MediaPermanentError, match="ffprobe") as corrupt_exc:
        await probe_media(corrupt)
    assert corrupt_exc.value.code == "corrupt_media"

    no_audio = tmp_path / "no-audio.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:d=0.2",
            "-an",
            "-c:v",
            "mpeg4",
            str(no_audio),
        ],
        check=True,
    )
    with pytest.raises(MediaPermanentError) as no_audio_exc:
        await probe_media(no_audio)
    assert no_audio_exc.value.code == "no_audio"

    multi = tmp_path / "multi.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=0.2",
            "-map",
            "0:a",
            "-map",
            "1:a",
            "-c:a",
            "aac",
            str(multi),
        ],
        check=True,
    )
    with pytest.raises(MediaPermanentError) as multi_exc:
        await probe_media(multi)
    assert multi_exc.value.code == "multiple_audio_streams"

    unsupported = tmp_path / "unsupported.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.2",
            "-c:a",
            "adpcm_ima_wav",
            str(unsupported),
        ],
        check=True,
    )
    with pytest.raises(MediaPermanentError) as unsupported_exc:
        await probe_media(unsupported)
    assert unsupported_exc.value.code == "unsupported_codec"


@pytest.mark.asyncio
async def test_subprocess_timeout_output_cap_and_hostile_filename_are_bounded(tmp_path: Path):
    settings = get_settings()
    with pytest.raises(MediaRetryableError) as timeout_exc:
        await _run_process(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            timeout=0.05,
            output_limit=4096,
        )
    assert timeout_exc.value.code == "subprocess_timeout"

    with pytest.raises(MediaPermanentError) as output_exc:
        await _run_process(
            [sys.executable, "-c", "print('x' * 10000)"],
            timeout=2,
            output_limit=4096,
        )
    assert output_exc.value.code == "subprocess_output_limit"

    marker = tmp_path / "pwned"
    hostile = tmp_path / f"evil;touch {marker.name}.wav"
    hostile.write_bytes(_wav_bytes(_tone_pcm(200)))
    probe = await probe_media(hostile)
    assert probe.codec_name.startswith("pcm_")
    assert not marker.exists()
    assert settings.media_subprocess_output_limit_bytes >= 4096


@pytest.mark.asyncio
async def test_normalized_first_durable_evidence_crash_rules(clean_recording_state, tmp_path: Path):
    del clean_recording_state
    settings = get_settings()
    storage = FilesystemNormalizedMediaStorage(settings.audio_storage_path)
    session_id = uuid4()
    source_key = "uploads/tus/source"
    source_hash = "a" * 64
    temp = storage.private_temp_path(session_id)
    temp.write_bytes(_wav_bytes(_tone_pcm(200)))
    media = storage.publish_temp(
        session_id=session_id,
        source_storage_key=source_key,
        source_sha256=source_hash,
        source_byte_length=123,
        selected_audio_stream=0,
        temp_path=temp,
    )
    assert (
        storage.verify_committed(
            session_id=session_id,
            source_storage_key=source_key,
            source_sha256=source_hash,
            source_byte_length=123,
            selected_audio_stream=0,
        )
        == media
    )

    # Once manifest evidence is durable, deleting/mutating the WAV is a loud integrity error;
    # it is never silently regenerated in a new identity.
    storage.final_path_for(session_id).write_bytes(b"mutated")
    with pytest.raises(NormalizedMediaError):
        storage.verify_committed(
            session_id=session_id,
            source_storage_key=source_key,
            source_sha256=source_hash,
            source_byte_length=123,
        )

    # Payload-before-manifest is not authoritative and deterministic retry may replace it.
    orphan_session = uuid4()
    orphan_final = storage.final_path_for(orphan_session)
    orphan_final.parent.mkdir(parents=True, exist_ok=True)
    orphan_final.write_bytes(b"orphan")
    replacement = storage.private_temp_path(orphan_session)
    replacement.write_bytes(_wav_bytes(_tone_pcm(180)))
    recovered = storage.publish_temp(
        session_id=orphan_session,
        source_storage_key=source_key,
        source_sha256=source_hash,
        source_byte_length=123,
        selected_audio_stream=0,
        temp_path=replacement,
    )
    assert recovered.total_samples == round(UPLOAD_SAMPLE_RATE * 0.18)


@pytest.mark.asyncio
async def test_instrumented_slow_completion_hash_does_not_block_event_loop_or_row_lock(
    clean_recording_state,
    monkeypatch,
):
    del clean_recording_state
    get_upload_storage.cache_clear()
    payload = _wav_bytes(_tone_pcm(200))
    upload_id = uuid4().hex
    async with get_sessionmaker()() as db:
        session, _ = await create_upload_session(
            db,
            client_request_id=uuid4(),
            capability_token=TOKEN,
            original_filename="slow.wav",
            content_type="audio/wav",
            byte_length=len(payload),
            duration_ms=None,
        )
        session_id = session.id
        await process_tusd_hook(
            db,
            _hook(
                "post-create",
                session_id=session_id,
                size=len(payload),
                offset=0,
                upload_id=upload_id,
            ),
        )

    storage = get_upload_storage()
    storage.upload_root.mkdir(parents=True, exist_ok=True)
    (storage.upload_root / upload_id).write_bytes(payload)
    original = storage.inspect_completed
    started = threading.Event()
    release = threading.Event()

    def slow_hash(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "inspect_completed", slow_hash)
    async with get_sessionmaker()() as hook_db:
        task = asyncio.create_task(
            process_tusd_hook(
                hook_db,
                _hook(
                    "pre-finish",
                    session_id=session_id,
                    size=len(payload),
                    offset=len(payload),
                    upload_id=upload_id,
                ),
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
        async with get_sessionmaker()() as probe_db, probe_db.begin():
            locked = await probe_db.scalar(
                select(UploadRecord)
                .where(UploadRecord.session_id == session_id)
                .with_for_update(nowait=True)
            )
            assert locked is not None
        release.set()
        await asyncio.wait_for(task, timeout=3)

    async with get_sessionmaker()() as db:
        record = await db.get(UploadRecord, session_id)
        processing = await db.get(UploadMediaProcessing, session_id)
    assert record is not None and record.completed_at is not None
    assert processing is not None and processing.state == UploadProcessingState.PENDING.value


@pytest.mark.asyncio
async def test_media_claim_restart_and_lost_wake_recover_from_postgres(
    clean_recording_state, tmp_path: Path
):
    del clean_recording_state
    session_id, _, _ = await _create_completed_upload(
        tmp_path,
        _wav_bytes(_tone_pcm(300)),
    )
    t0 = datetime.now(UTC)
    first = await claim_next_upload_processing(now=t0)
    assert first is not None and first.session_id == session_id
    assert await claim_next_upload_processing(now=t0 + timedelta(seconds=1)) is None

    # Simulated worker death: no broker message survives. PostgreSQL claim expiry recreates
    # runnable authority and the media reconciler requests a generic wake again.
    result = await reconcile_upload_processing()
    assert result.wake_target == 0
    recovered = await claim_next_upload_processing(
        now=t0 + timedelta(seconds=get_settings().media_claim_lease_seconds + 1)
    )
    assert recovered is not None and recovered.token != first.token


@pytest.mark.asyncio
async def test_blank_stt_is_terminal_no_speech_and_upload_still_succeeds(
    clean_recording_state,
    tmp_path: Path,
):
    del clean_recording_state
    session_id, _, _ = await _create_completed_upload(
        tmp_path,
        _wav_bytes(_tone_pcm(300)),
    )
    async with get_sessionmaker()() as db, db.begin():
        processing = await db.get(UploadMediaProcessing, session_id)
        assert processing is not None
        processing.state = UploadProcessingState.WAITING_STT.value
        processing.expected_utterance_count = 2

    async with get_sessionmaker()() as db:
        first, _ = await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key=upload_utterance_producer_key(1),
            start_ms=0,
            end_ms=500,
            content_type="audio/wav",
            payload=_wav_bytes(_tone_pcm(500)),
        )
    async with get_sessionmaker()() as db:
        second, _ = await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key=upload_utterance_producer_key(2),
            start_ms=500,
            end_ms=1000,
            content_type="audio/wav",
            payload=_wav_bytes(_tone_pcm(500)),
        )

    provider = SequenceProvider(
        [STTResult(text="spoken", language="id"), STTResult(text="   ", language="id")]
    )
    assert (
        await execute_stt_job(utterance_id=first.id, provider=provider)
    ).status == STTExecutionStatus.SUCCEEDED
    blank = await execute_stt_job(utterance_id=second.id, provider=provider)
    assert blank.status == STTExecutionStatus.NO_SPEECH

    result = await reconcile_upload_processing()
    assert result.stt_succeeded == 1
    async with get_sessionmaker()() as db:
        blank_job = await db.get(STTJob, second.id)
        processing = await db.get(UploadMediaProcessing, session_id)
        transcript_count = int(
            await db.scalar(
                select(func.count(TranscriptSegment.id)).where(
                    TranscriptSegment.session_id == session_id
                )
            )
            or 0
        )
    assert blank_job is not None and blank_job.state == STTJobState.NO_SPEECH.value
    assert processing is not None and processing.state == UploadProcessingState.SUCCEEDED.value
    assert transcript_count == 1

    # Reconciliation must not treat no_speech as false success just because no row exists.
    await reconcile_stt_jobs(
        workload_class=STTWorkloadClass.UPLOAD,
        ensure_wake_capacity=lambda target: target,
    )
    async with get_sessionmaker()() as db:
        blank_job = await db.get(STTJob, second.id)
    assert blank_job is not None and blank_job.state == STTJobState.NO_SPEECH.value


@pytest.mark.asyncio
async def test_upload_backlog_cannot_consume_live_reserved_headroom(clean_recording_state):
    del clean_recording_state
    now = datetime.now(UTC)
    upload_session = RecordingSession(
        id=uuid4(),
        client_request_id=uuid4(),
        kind=SessionKind.UPLOAD.value,
        state=SessionState.UPLOADED.value,
        capture_epoch=0,
        created_at=now,
        updated_at=now,
    )
    async with get_sessionmaker()() as db:
        db.add(upload_session)
        for index in range(200):
            utterance_id = uuid4()
            db.add(
                TranscriptionUtterance(
                    id=utterance_id,
                    session_id=upload_session.id,
                    sequence=index + 1,
                    producer_key=f"bulk:{index:04d}",
                    start_ms=index * 1000,
                    end_ms=index * 1000 + 500,
                    content_type="audio/wav",
                    sha256="a" * 64,
                    byte_length=1,
                    storage_key=f"bulk/{index}",
                )
            )
            db.add(
                STTJob(
                    utterance_id=utterance_id,
                    session_id=upload_session.id,
                    state=STTJobState.PENDING.value,
                )
            )
        await db.commit()

    live_session = RecordingSession(
        id=uuid4(),
        client_request_id=uuid4(),
        kind=SessionKind.LIVE.value,
        state=SessionState.RECORDING.value,
        active_writer_id="writer-live-headroom",
        capture_epoch=1,
        created_at=now,
        updated_at=now,
    )
    async with get_sessionmaker()() as db:
        db.add(live_session)
        await db.commit()
    async with get_sessionmaker()() as db:
        live_work, _ = await commit_utterance_work(
            db,
            session_id=live_session.id,
            producer_key="live:late",
            start_ms=0,
            end_ms=500,
            content_type="audio/wav",
            payload=_wav_bytes(_tone_pcm(500)),
        )

    upload_wakes: list[int] = []
    live_wakes: list[int] = []
    upload_result = await reconcile_stt_jobs(
        workload_class=STTWorkloadClass.UPLOAD,
        limit=32,
        per_session_limit=2,
        ensure_wake_capacity=lambda target: upload_wakes.append(target) or target,
    )
    live_result = await reconcile_stt_jobs(
        workload_class=STTWorkloadClass.LIVE,
        limit=4,
        per_session_limit=2,
        ensure_wake_capacity=lambda target: live_wakes.append(target) or target,
    )
    assert upload_result.wake_target == 2
    assert live_result.wake_target == 1
    assert upload_wakes == [2]
    assert live_wakes == [1]

    provider = SequenceProvider([STTResult(text="live wins", language="id")])
    execution = await execute_next_reserved_stt_job(
        workload_class=STTWorkloadClass.LIVE,
        provider=provider,
    )
    assert execution.status == STTExecutionStatus.SUCCEEDED
    assert provider.calls == 1
    async with get_sessionmaker()() as db:
        live_count = int(
            await db.scalar(
                select(func.count(TranscriptSegment.id)).where(
                    TranscriptSegment.session_id == live_session.id
                )
            )
            or 0
        )
        upload_succeeded = int(
            await db.scalar(
                select(func.count(STTJob.utterance_id)).where(
                    STTJob.session_id == upload_session.id,
                    STTJob.state == STTJobState.SUCCEEDED.value,
                )
            )
            or 0
        )
    assert live_count == 1
    assert upload_succeeded == 0


class _GroqHandler(BaseHTTPRequestHandler):
    requests: list[bytes] = []
    response_text = "adapter success"

    def do_POST(self):  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.__class__.requests.append(body)
        payload = ('{"text":"%s","language":"id"}' % self.__class__.response_text).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):  # noqa: A002
        del format, args


@pytest.mark.asyncio
async def test_secretless_real_groq_adapter_full_upload_path_and_wake_loss_recovery(
    clean_recording_state,
    tmp_path: Path,
):
    del clean_recording_state
    _GroqHandler.requests = []
    payload = _wav_bytes(_silence_pcm(200), _tone_pcm(500), _silence_pcm(700))
    session_id, _, _ = await _create_completed_upload(tmp_path, payload)

    claim = await claim_next_upload_processing()
    assert claim is not None and claim.session_id == session_id
    assert await execute_upload_processing_claim(claim) is True
    async with get_sessionmaker()() as db:
        processing = await db.get(UploadMediaProcessing, session_id)
        assert processing is not None
        assert processing.state == UploadProcessingState.WAITING_STT.value
        assert processing.expected_utterance_count == 1

    # First publication is lost. PostgreSQL reservation remains authoritative and next
    # reconciler pass replenishes the generic upload wake without changing work identity.
    def lost_publish(target: int) -> int:
        assert target == 1
        raise OSError("redis unavailable")

    first = await reconcile_stt_jobs(
        workload_class=STTWorkloadClass.UPLOAD,
        ensure_wake_capacity=lost_publish,
    )
    assert first.enqueue_failures == 1
    second_wakes: list[int] = []
    second = await reconcile_stt_jobs(
        workload_class=STTWorkloadClass.UPLOAD,
        ensure_wake_capacity=lambda target: second_wakes.append(target) or target,
    )
    assert second.wake_target == 1
    assert second_wakes == [1]

    server = ThreadingHTTPServer(("127.0.0.1", 0), _GroqHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = GroqSTTProvider(
            api_key="ci-placeholder-not-a-real-secret",
            endpoint=f"http://127.0.0.1:{server.server_port}/openai/v1/audio/transcriptions",
            model="whisper-large-v3-turbo",
            timeout_seconds=5,
        )
        execution = await execute_next_reserved_stt_job(
            workload_class=STTWorkloadClass.UPLOAD,
            provider=provider,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert execution.status == STTExecutionStatus.SUCCEEDED
    assert len(_GroqHandler.requests) == 1
    assert b'name="file"; filename="utterance.wav"' in _GroqHandler.requests[0]

    convergence = await reconcile_upload_processing()
    assert convergence.stt_succeeded == 1
    async with get_sessionmaker()() as db:
        processing = await db.get(UploadMediaProcessing, session_id)
        transcript_count = int(
            await db.scalar(
                select(func.count(TranscriptSegment.id)).where(
                    TranscriptSegment.session_id == session_id
                )
            )
            or 0
        )
    assert processing is not None and processing.state == UploadProcessingState.SUCCEEDED.value
    assert transcript_count == 1
