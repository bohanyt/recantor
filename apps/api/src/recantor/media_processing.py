from __future__ import annotations

import asyncio
import json
import math
import os
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from recantor.db import get_sessionmaker
from recantor.media_spec import (
    NORMALIZATION_SPEC_ID,
    SEGMENTATION_PARAMS_JSON,
    SEGMENTATION_SPEC_ID,
    UPLOAD_FRAME_SAMPLES,
    UPLOAD_SAMPLE_RATE,
    UPLOAD_SEGMENTATION_SPEC,
)
from recantor.media_storage import (
    FilesystemNormalizedMediaStorage,
    NormalizedMedia,
    NormalizedMediaConflict,
    NormalizedMediaError,
)
from recantor.models import (
    RecordingSession,
    SessionKind,
    STTJob,
    STTJobState,
    TranscriptionUtterance,
    TranscriptSegment,
    UploadMediaProcessing,
    UploadProcessingState,
    UploadRecord,
)
from recantor.realtime_audio import EnergyEndpointDetector, PcmFrame, VadConfig, encode_pcm_wav
from recantor.settings import get_settings
from recantor.upload_storage import FilesystemUploadStorage, UploadStorageError
from recantor.utterance import (
    UtteranceWorkConflict,
    UtteranceWorkStorageError,
    commit_utterance_work,
)


class MediaProcessingError(RuntimeError):
    pass


class MediaProcessingStale(MediaProcessingError):
    pass


class MediaPermanentError(MediaProcessingError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MediaRetryableError(MediaProcessingError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MediaSubprocessLimit(MediaProcessingError):
    pass


@dataclass(frozen=True)
class MediaClaim:
    session_id: UUID
    token: str
    attempt_count: int
    expires_at: datetime


@dataclass(frozen=True)
class MediaProbe:
    audio_stream_index: int
    format_names: tuple[str, ...]
    codec_name: str
    duration_seconds: float | None


@dataclass(frozen=True)
class MediaReconcileResult:
    created: int = 0
    stt_succeeded: int = 0
    stt_failed: int = 0
    wake_target: int = 0


@dataclass(frozen=True)
class _SourceIdentity:
    session_id: UUID
    tus_upload_id: str
    storage_key: str
    sha256: str
    byte_length: int
    completed_at: datetime


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


_SUPPORTED_FORMAT_NAMES = {
    "wav",
    "mp3",
    "mov",
    "mp4",
    "m4a",
    "3gp",
    "3g2",
    "mj2",
    "ogg",
    "matroska",
    "webm",
    "flac",
}
_SUPPORTED_CODECS = {"mp3", "aac", "alac", "opus", "vorbis", "flac"}
_UPLOAD_PRODUCER_PREFIX = f"{SEGMENTATION_SPEC_ID}:"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_message(message: str, fallback: str) -> str:
    normalized = " ".join(message.split()).strip() or fallback
    return normalized[:512]


def _upload_storage() -> FilesystemUploadStorage:
    settings = get_settings()
    return FilesystemUploadStorage(
        settings.audio_storage_path,
        settings.upload_tus_storage_prefix,
    )


def _normalized_storage() -> FilesystemNormalizedMediaStorage:
    return FilesystemNormalizedMediaStorage(get_settings().audio_storage_path)


def _processing_identity_values(record: UploadRecord) -> dict[str, object]:
    if (
        record.completed_at is None
        or record.storage_key is None
        or record.sha256 is None
        or record.byte_length is None
    ):
        raise MediaProcessingError("upload is not durably complete")
    return {
        "session_id": record.session_id,
        "source_storage_key": record.storage_key,
        "source_sha256": record.sha256,
        "source_byte_length": record.byte_length,
        "source_completed_at": record.completed_at,
        "state": UploadProcessingState.PENDING.value,
        "normalization_spec_id": NORMALIZATION_SPEC_ID,
        "segmentation_spec_id": SEGMENTATION_SPEC_ID,
        "segmentation_params_json": SEGMENTATION_PARAMS_JSON,
    }


def _assert_processing_identity(processing: UploadMediaProcessing, record: UploadRecord) -> None:
    expected = _processing_identity_values(record)
    for field in (
        "source_storage_key",
        "source_sha256",
        "source_byte_length",
        "source_completed_at",
        "normalization_spec_id",
        "segmentation_spec_id",
        "segmentation_params_json",
    ):
        if getattr(processing, field) != expected[field]:
            raise MediaPermanentError(
                "processing_identity_drift",
                f"durable upload processing identity drifted at {field}",
            )


async def _database_now(db) -> datetime:
    value = await db.scalar(select(func.clock_timestamp()))
    if value is None:
        raise MediaProcessingError("database did not return a processing clock")
    return value


async def ensure_upload_processing_rows(batch_size: int = 100) -> int:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    async with get_sessionmaker()() as db:
        completed = list(
            (
                await db.scalars(
                    select(UploadRecord)
                    .outerjoin(
                        UploadMediaProcessing,
                        UploadMediaProcessing.session_id == UploadRecord.session_id,
                    )
                    .where(
                        UploadRecord.completed_at.is_not(None),
                        UploadMediaProcessing.session_id.is_(None),
                    )
                    .order_by(UploadRecord.completed_at, UploadRecord.session_id)
                    .limit(batch_size)
                )
            ).all()
        )
        if not completed:
            return 0
        statement = (
            insert(UploadMediaProcessing)
            .values([_processing_identity_values(record) for record in completed])
            .on_conflict_do_nothing(index_elements=[UploadMediaProcessing.session_id])
            .returning(UploadMediaProcessing.session_id)
        )
        created = list((await db.scalars(statement)).all())
        await db.commit()
        return len(created)


def _claimable_expression(current: datetime):
    return or_(
        UploadMediaProcessing.state == UploadProcessingState.PENDING.value,
        and_(
            UploadMediaProcessing.state == UploadProcessingState.RETRY_WAIT.value,
            UploadMediaProcessing.next_attempt_at.is_not(None),
            UploadMediaProcessing.next_attempt_at <= current,
        ),
        and_(
            UploadMediaProcessing.state == UploadProcessingState.CLAIMED.value,
            UploadMediaProcessing.claim_expires_at.is_not(None),
            UploadMediaProcessing.claim_expires_at <= current,
        ),
    )


async def claim_next_upload_processing(now: datetime | None = None) -> MediaClaim | None:
    settings = get_settings()
    async with get_sessionmaker()() as db, db.begin():
        current = now or await _database_now(db)
        for _ in range(8):
            processing = await db.scalar(
                select(UploadMediaProcessing)
                .where(_claimable_expression(current))
                .order_by(UploadMediaProcessing.created_at, UploadMediaProcessing.session_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if processing is None:
                return None
            record = await db.get(UploadRecord, processing.session_id)
            session = await db.get(RecordingSession, processing.session_id)
            if (
                record is None
                or session is None
                or session.kind != SessionKind.UPLOAD.value
                or record.completed_at is None
            ):
                processing.state = UploadProcessingState.FAILED.value
                processing.next_attempt_at = None
                processing.claim_token = None
                processing.claim_expires_at = None
                processing.last_error_category = "identity"
                processing.last_error_code = "completed_upload_missing"
                processing.last_error_message = "durable completed upload identity is missing"
                processing.completed_at = current
                continue
            try:
                _assert_processing_identity(processing, record)
            except MediaPermanentError as exc:
                processing.state = UploadProcessingState.FAILED.value
                processing.next_attempt_at = None
                processing.claim_token = None
                processing.claim_expires_at = None
                processing.last_error_category = "identity"
                processing.last_error_code = exc.code
                processing.last_error_message = _safe_message(str(exc), exc.code)
                processing.completed_at = current
                continue
            if processing.attempt_count >= settings.media_max_attempts:
                processing.state = UploadProcessingState.FAILED.value
                processing.next_attempt_at = None
                processing.claim_token = None
                processing.claim_expires_at = None
                processing.last_error_category = "scheduler"
                processing.last_error_code = "retry_budget_exhausted"
                processing.last_error_message = "automatic media processing attempt budget exhausted"
                processing.completed_at = current
                continue

            token = uuid4().hex
            expires_at = current + timedelta(seconds=float(settings.media_claim_lease_seconds))
            processing.state = UploadProcessingState.CLAIMED.value
            processing.attempt_count += 1
            processing.next_attempt_at = None
            processing.claim_token = token
            processing.claim_expires_at = expires_at
            processing.last_error_category = None
            processing.last_error_code = None
            processing.last_error_message = None
            await db.flush()
            return MediaClaim(
                session_id=processing.session_id,
                token=token,
                attempt_count=processing.attempt_count,
                expires_at=expires_at,
            )
    return None


async def _renew_claim(claim: MediaClaim) -> None:
    settings = get_settings()
    async with get_sessionmaker()() as db, db.begin():
        processing = await db.scalar(
            select(UploadMediaProcessing)
            .where(UploadMediaProcessing.session_id == claim.session_id)
            .with_for_update()
        )
        current = await _database_now(db)
        if (
            processing is None
            or processing.state != UploadProcessingState.CLAIMED.value
            or processing.claim_token != claim.token
            or processing.claim_expires_at is None
            or processing.claim_expires_at <= current
        ):
            raise MediaProcessingStale("media processing claim is no longer current")
        processing.claim_expires_at = current + timedelta(
            seconds=float(settings.media_claim_lease_seconds)
        )


async def _load_claim_source(claim: MediaClaim) -> _SourceIdentity:
    async with get_sessionmaker()() as db:
        processing = await db.get(UploadMediaProcessing, claim.session_id)
        record = await db.get(UploadRecord, claim.session_id)
        session = await db.get(RecordingSession, claim.session_id)
    if processing is None or record is None or session is None:
        raise MediaPermanentError("completed_upload_missing", "completed upload state is missing")
    if session.kind != SessionKind.UPLOAD.value:
        raise MediaPermanentError("session_kind_mismatch", "processing session is not an upload")
    _assert_processing_identity(processing, record)
    if record.tus_upload_id is None:
        raise MediaPermanentError("upload_binding_missing", "completed upload has no tus binding")
    if record.storage_key is None or record.sha256 is None or record.byte_length is None:
        raise MediaPermanentError("completed_upload_missing", "completed upload evidence is incomplete")
    if record.completed_at is None:
        raise MediaPermanentError("completed_upload_missing", "completed upload timestamp is missing")
    return _SourceIdentity(
        session_id=claim.session_id,
        tus_upload_id=record.tus_upload_id,
        storage_key=record.storage_key,
        sha256=record.sha256,
        byte_length=record.byte_length,
        completed_at=record.completed_at,
    )


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise MediaSubprocessLimit("media subprocess output exceeded configured limit")
        chunks.append(chunk)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def _run_process(argv: list[str], *, timeout: float, output_limit: int) -> _ProcessResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LC_ALL": "C"},
    )
    assert process.stdout is not None and process.stderr is not None
    output_tasks = [
        asyncio.create_task(_read_bounded(process.stdout, output_limit)),
        asyncio.create_task(_read_bounded(process.stderr, output_limit)),
    ]
    combined = asyncio.gather(process.wait(), *output_tasks)
    try:
        returncode, stdout, stderr = await asyncio.wait_for(combined, timeout=timeout)
    except TimeoutError as exc:
        await _stop_process(process)
        raise MediaRetryableError("subprocess_timeout", "media subprocess timed out") from exc
    except MediaSubprocessLimit as exc:
        await _stop_process(process)
        raise MediaPermanentError("subprocess_output_limit", str(exc)) from exc
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    finally:
        if not combined.done():
            combined.cancel()
        for task in output_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*output_tasks, return_exceptions=True)
    return _ProcessResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _parse_duration(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        duration = float(value)
    except ValueError:
        return None
    if not math.isfinite(duration) or duration < 0:
        return None
    return duration


async def probe_media(path: Path) -> MediaProbe:
    settings = get_settings()
    result = await _run_process(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration:stream=index,codec_type,codec_name,duration",
            "-of",
            "json",
            str(path),
        ],
        timeout=float(settings.media_probe_timeout_seconds),
        output_limit=settings.media_subprocess_output_limit_bytes,
    )
    if result.returncode != 0:
        raise MediaPermanentError("corrupt_media", "ffprobe rejected uploaded media")
    try:
        parsed = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaPermanentError("probe_malformed", "ffprobe returned malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise MediaPermanentError("probe_malformed", "ffprobe response is not an object")
    raw_streams = parsed.get("streams")
    if not isinstance(raw_streams, list):
        raise MediaPermanentError("probe_malformed", "ffprobe response has no stream list")
    audio_streams = [
        stream
        for stream in raw_streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise MediaPermanentError("no_audio", "uploaded media contains no audio stream")
    if len(audio_streams) != 1:
        raise MediaPermanentError(
            "multiple_audio_streams",
            "uploaded media must contain exactly one audio stream",
        )
    stream = audio_streams[0]
    index = stream.get("index")
    codec = stream.get("codec_name")
    if not isinstance(index, int) or index < 0 or not isinstance(codec, str):
        raise MediaPermanentError("probe_malformed", "ffprobe audio stream identity is malformed")
    codec = codec.strip().lower()
    if not (codec.startswith("pcm_") or codec in _SUPPORTED_CODECS):
        raise MediaPermanentError("unsupported_codec", f"unsupported uploaded audio codec: {codec}")

    raw_format = parsed.get("format")
    if not isinstance(raw_format, dict) or not isinstance(raw_format.get("format_name"), str):
        raise MediaPermanentError("probe_malformed", "ffprobe container identity is missing")
    format_names = tuple(
        part.strip().lower() for part in raw_format["format_name"].split(",") if part.strip()
    )
    if not set(format_names).intersection(_SUPPORTED_FORMAT_NAMES):
        raise MediaPermanentError("unsupported_container", "uploaded media container is unsupported")

    durations = [
        duration
        for duration in (
            _parse_duration(raw_format.get("duration")),
            _parse_duration(stream.get("duration")),
        )
        if duration is not None
    ]
    duration = max(durations) if durations else None
    if duration is not None and duration > float(settings.upload_max_duration_seconds):
        raise MediaPermanentError("duration_limit", "uploaded media exceeds duration limit")
    return MediaProbe(
        audio_stream_index=index,
        format_names=format_names,
        codec_name=codec,
        duration_seconds=duration,
    )


async def _project_normalized(claim: MediaClaim, media: NormalizedMedia) -> None:
    async with get_sessionmaker()() as db, db.begin():
        processing = await db.scalar(
            select(UploadMediaProcessing)
            .where(UploadMediaProcessing.session_id == claim.session_id)
            .with_for_update()
        )
        current = await _database_now(db)
        if (
            processing is None
            or processing.state != UploadProcessingState.CLAIMED.value
            or processing.claim_token != claim.token
            or processing.claim_expires_at is None
            or processing.claim_expires_at <= current
        ):
            raise MediaProcessingStale("media processing claim changed before normalized publish")
        if processing.normalization_spec_id != NORMALIZATION_SPEC_ID:
            raise MediaPermanentError(
                "processing_identity_drift",
                "normalization specification identity drifted",
            )
        existing = (
            processing.selected_audio_stream,
            processing.normalized_storage_key,
            processing.normalized_sha256,
            processing.normalized_byte_length,
            processing.normalized_total_samples,
        )
        expected = (
            media.selected_audio_stream,
            media.key,
            media.sha256,
            media.byte_length,
            media.total_samples,
        )
        if processing.normalized_storage_key is not None and existing != expected:
            raise MediaPermanentError(
                "normalized_identity_drift",
                "normalized artifact projection conflicts with durable evidence",
            )
        processing.selected_audio_stream = media.selected_audio_stream
        processing.normalized_storage_key = media.key
        processing.normalized_sha256 = media.sha256
        processing.normalized_byte_length = media.byte_length
        processing.normalized_total_samples = media.total_samples


async def _load_or_create_normalized(
    claim: MediaClaim,
    source: _SourceIdentity,
    source_path: Path,
) -> NormalizedMedia:
    storage = _normalized_storage()
    async with get_sessionmaker()() as db:
        processing = await db.get(UploadMediaProcessing, claim.session_id)
    if processing is None:
        raise MediaProcessingStale("media processing row disappeared")
    if processing.normalized_storage_key is not None:
        try:
            media = await asyncio.to_thread(
                storage.verify_committed,
                session_id=claim.session_id,
                source_storage_key=source.storage_key,
                source_sha256=source.sha256,
                source_byte_length=source.byte_length,
                selected_audio_stream=processing.selected_audio_stream,
            )
        except (NormalizedMediaError, NormalizedMediaConflict) as exc:
            raise MediaPermanentError(
                "normalized_storage_integrity",
                "committed normalized media failed verification",
            ) from exc
        expected = (
            processing.selected_audio_stream,
            processing.normalized_storage_key,
            processing.normalized_sha256,
            processing.normalized_byte_length,
            processing.normalized_total_samples,
        )
        actual = (
            media.selected_audio_stream,
            media.key,
            media.sha256,
            media.byte_length,
            media.total_samples,
        )
        if expected != actual:
            raise MediaPermanentError(
                "normalized_identity_drift",
                "database normalized identity conflicts with manifest",
            )
        return media

    if storage.manifest_path_for(claim.session_id).exists():
        try:
            media = await asyncio.to_thread(
                storage.verify_committed,
                session_id=claim.session_id,
                source_storage_key=source.storage_key,
                source_sha256=source.sha256,
                source_byte_length=source.byte_length,
            )
        except (NormalizedMediaError, NormalizedMediaConflict) as exc:
            raise MediaPermanentError(
                "normalized_storage_integrity",
                "orphaned normalized manifest failed verification",
            ) from exc
        await _project_normalized(claim, media)
        return media

    probe = await probe_media(source_path)
    await _renew_claim(claim)
    settings = get_settings()
    normalized_temp = storage.private_temp_path(claim.session_id)
    try:
        max_decode_seconds = float(settings.upload_max_duration_seconds) + 1.0
        result = await _run_process(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-threads",
                "1",
                "-copyts",
                "-i",
                str(source_path),
                "-map",
                f"0:{probe.audio_stream_index}",
                "-vn",
                "-sn",
                "-dn",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-af",
                f"aresample={UPLOAD_SAMPLE_RATE}:async=1:first_pts=0",
                "-ac",
                "1",
                "-ar",
                str(UPLOAD_SAMPLE_RATE),
                "-sample_fmt",
                "s16",
                "-c:a",
                "pcm_s16le",
                "-threads",
                "1",
                "-t",
                f"{max_decode_seconds:.3f}",
                "-f",
                "wav",
                str(normalized_temp),
            ],
            timeout=float(settings.media_normalize_timeout_seconds),
            output_limit=settings.media_subprocess_output_limit_bytes,
        )
        if result.returncode != 0:
            raise MediaPermanentError("decode_failed", "FFmpeg could not normalize uploaded media")
        samples = await asyncio.to_thread(storage.inspect_wav, normalized_temp)
        if samples > settings.upload_max_duration_seconds * UPLOAD_SAMPLE_RATE:
            raise MediaPermanentError(
                "duration_limit",
                "decoded uploaded media exceeds duration limit",
            )
        # Fence/renew immediately before the first durable normalized evidence is published.
        await _renew_claim(claim)
        media = await asyncio.to_thread(
            storage.publish_temp,
            session_id=claim.session_id,
            source_storage_key=source.storage_key,
            source_sha256=source.sha256,
            source_byte_length=source.byte_length,
            selected_audio_stream=probe.audio_stream_index,
            temp_path=normalized_temp,
        )
    finally:
        try:
            normalized_temp.unlink(missing_ok=True)
        except OSError:
            pass
    await _project_normalized(claim, media)
    return media


def _upload_vad() -> EnergyEndpointDetector:
    spec = UPLOAD_SEGMENTATION_SPEC
    return EnergyEndpointDetector(
        UPLOAD_SAMPLE_RATE,
        VadConfig(
            pre_roll_ms=spec.pre_roll_ms,
            min_voiced_ms=spec.min_voiced_ms,
            silence_ms=spec.trailing_silence_ms,
            hard_max_ms=spec.hard_max_ms,
            absolute_threshold_dbfs=spec.absolute_threshold_dbfs,
            noise_margin_db=spec.noise_margin_db,
            initial_noise_dbfs=spec.initial_noise_dbfs,
            noise_alpha=spec.noise_alpha,
        ),
    )


def upload_utterance_producer_key(ordinal: int, start_sample: int, end_sample: int) -> str:
    return f"{SEGMENTATION_SPEC_ID}:{ordinal:08d}:{start_sample}:{end_sample}"


async def _commit_candidate(
    *,
    claim: MediaClaim,
    ordinal: int,
    start_sample: int,
    end_sample: int,
    pcm: bytes,
) -> None:
    await _renew_claim(claim)
    start_ms = round(start_sample * 1000 / UPLOAD_SAMPLE_RATE)
    end_ms = round(end_sample * 1000 / UPLOAD_SAMPLE_RATE)
    if end_ms <= start_ms:
        raise MediaPermanentError(
            "segmentation_identity_drift",
            "deterministic upload segment has invalid timeline bounds",
        )
    payload = encode_pcm_wav(pcm, sample_rate=UPLOAD_SAMPLE_RATE)
    try:
        async with get_sessionmaker()() as db:
            await commit_utterance_work(
                db,
                session_id=claim.session_id,
                producer_key=upload_utterance_producer_key(ordinal, start_sample, end_sample),
                start_ms=start_ms,
                end_ms=end_ms,
                content_type="audio/wav",
                payload=payload,
            )
    except (UtteranceWorkConflict, UtteranceWorkStorageError) as exc:
        raise MediaPermanentError(
            "segmentation_identity_drift",
            "deterministic upload utterance retry conflicts with durable evidence",
        ) from exc


async def segment_normalized(claim: MediaClaim, media: NormalizedMedia) -> int:
    async with get_sessionmaker()() as db:
        processing = await db.get(UploadMediaProcessing, claim.session_id)
    if processing is None:
        raise MediaProcessingStale("media processing row disappeared")
    if (
        processing.segmentation_spec_id != SEGMENTATION_SPEC_ID
        or processing.segmentation_params_json != SEGMENTATION_PARAMS_JSON
    ):
        raise MediaPermanentError(
            "segmentation_identity_drift",
            "durable segmentation parameter snapshot drifted",
        )

    path = _normalized_storage().normalized_path(claim.session_id, media.key)
    detector = _upload_vad()
    sample_offset = 0
    ordinal = 0
    try:
        source = wave.open(str(path), "rb")
    except (OSError, wave.Error) as exc:
        raise MediaPermanentError(
            "normalized_storage_integrity",
            "normalized media could not be opened for deterministic segmentation",
        ) from exc
    with source:
        while True:
            pcm = source.readframes(UPLOAD_FRAME_SAMPLES)
            if not pcm:
                break
            frame = PcmFrame(sample_offset=sample_offset, pcm=pcm)
            sample_offset += frame.sample_count
            for candidate in detector.feed(frame):
                ordinal += 1
                await _commit_candidate(
                    claim=claim,
                    ordinal=ordinal,
                    start_sample=candidate.start_sample,
                    end_sample=candidate.end_sample,
                    pcm=candidate.pcm,
                )
        eof_candidate = detector.flush()
        if eof_candidate is not None:
            ordinal += 1
            await _commit_candidate(
                claim=claim,
                ordinal=ordinal,
                start_sample=eof_candidate.start_sample,
                end_sample=eof_candidate.end_sample,
                pcm=eof_candidate.pcm,
            )

    if sample_offset != media.total_samples:
        raise MediaPermanentError(
            "normalized_storage_integrity",
            "normalized sample count changed during segmentation",
        )
    async with get_sessionmaker()() as db:
        durable_count = int(
            await db.scalar(
                select(func.count(TranscriptionUtterance.id)).where(
                    TranscriptionUtterance.session_id == claim.session_id,
                    TranscriptionUtterance.producer_key.like(f"{_UPLOAD_PRODUCER_PREFIX}%"),
                )
            )
            or 0
        )
    if durable_count != ordinal:
        raise MediaPermanentError(
            "segmentation_identity_drift",
            "durable upload utterance set differs from deterministic replay",
        )
    return ordinal


async def _publish_segmentation_result(claim: MediaClaim, expected_count: int) -> None:
    async with get_sessionmaker()() as db, db.begin():
        processing = await db.scalar(
            select(UploadMediaProcessing)
            .where(UploadMediaProcessing.session_id == claim.session_id)
            .with_for_update()
        )
        current = await _database_now(db)
        if (
            processing is None
            or processing.state != UploadProcessingState.CLAIMED.value
            or processing.claim_token != claim.token
            or processing.claim_expires_at is None
            or processing.claim_expires_at <= current
        ):
            raise MediaProcessingStale("media processing claim changed before segmentation publish")
        if (
            processing.segmentation_spec_id != SEGMENTATION_SPEC_ID
            or processing.segmentation_params_json != SEGMENTATION_PARAMS_JSON
        ):
            raise MediaPermanentError(
                "segmentation_identity_drift",
                "durable segmentation parameter snapshot drifted before publish",
            )
        if (
            processing.expected_utterance_count is not None
            and processing.expected_utterance_count != expected_count
        ):
            raise MediaPermanentError(
                "segmentation_identity_drift",
                "deterministic upload utterance count drifted",
            )
        processing.expected_utterance_count = expected_count
        processing.claim_token = None
        processing.claim_expires_at = None
        processing.next_attempt_at = None
        processing.last_error_category = None
        processing.last_error_code = None
        processing.last_error_message = None
        if expected_count == 0:
            processing.state = UploadProcessingState.SUCCEEDED.value
            processing.outcome_code = "no_speech"
            processing.completed_at = current
        else:
            processing.state = UploadProcessingState.WAITING_STT.value
            processing.outcome_code = None
            processing.completed_at = None


async def _record_processing_error(claim: MediaClaim, exc: MediaProcessingError) -> None:
    settings = get_settings()
    async with get_sessionmaker()() as db, db.begin():
        processing = await db.scalar(
            select(UploadMediaProcessing)
            .where(UploadMediaProcessing.session_id == claim.session_id)
            .with_for_update()
        )
        if (
            processing is None
            or processing.state != UploadProcessingState.CLAIMED.value
            or processing.claim_token != claim.token
        ):
            return
        current = await _database_now(db)
        processing.claim_token = None
        processing.claim_expires_at = None
        code = exc.code if isinstance(exc, (MediaPermanentError, MediaRetryableError)) else "worker_error"
        processing.last_error_category = (
            "transient" if isinstance(exc, MediaRetryableError) else "media"
        )
        processing.last_error_code = code[:96]
        processing.last_error_message = _safe_message(str(exc), code)
        if isinstance(exc, MediaRetryableError) and processing.attempt_count < settings.media_max_attempts:
            delay = float(settings.media_retry_base_seconds) * (2 ** max(0, processing.attempt_count - 1))
            processing.state = UploadProcessingState.RETRY_WAIT.value
            processing.next_attempt_at = current + timedelta(seconds=delay)
            processing.completed_at = None
            return
        processing.state = UploadProcessingState.FAILED.value
        processing.next_attempt_at = None
        processing.completed_at = current


async def execute_upload_processing_claim(claim: MediaClaim) -> bool:
    try:
        source = await _load_claim_source(claim)
        storage = _upload_storage()
        source_path = storage.completed_path(
            source.tus_upload_id,
            expected_key=source.storage_key,
            expected_bytes=source.byte_length,
        )
        inspected = await asyncio.to_thread(
            storage.inspect_completed,
            source.tus_upload_id,
            expected_bytes=source.byte_length,
        )
        if inspected.sha256 != source.sha256 or inspected.key != source.storage_key:
            raise MediaPermanentError(
                "source_integrity",
                "completed upload source no longer matches immutable completion evidence",
            )
        await _renew_claim(claim)
        media = await _load_or_create_normalized(claim, source, source_path)
        expected_count = await segment_normalized(claim, media)
        await _publish_segmentation_result(claim, expected_count)
        return True
    except MediaProcessingStale:
        return False
    except UploadStorageError as exc:
        await _record_processing_error(
            claim,
            MediaPermanentError("source_integrity", "completed upload source failed verification"),
        )
        return False
    except NormalizedMediaError as exc:
        await _record_processing_error(
            claim,
            MediaPermanentError("normalized_storage_integrity", str(exc)),
        )
        return False
    except MediaProcessingError as exc:
        await _record_processing_error(claim, exc)
        return False
    except Exception as exc:
        await _record_processing_error(
            claim,
            MediaRetryableError(
                "unexpected_worker_error",
                f"unexpected media worker error: {type(exc).__name__}",
            ),
        )
        return False


async def process_next_upload() -> bool:
    claim = await claim_next_upload_processing()
    if claim is None:
        return False
    return await execute_upload_processing_claim(claim)


async def _converge_waiting_stt(batch_size: int = 100) -> tuple[int, int]:
    succeeded = 0
    failed = 0
    async with get_sessionmaker()() as db:
        session_ids = list(
            (
                await db.scalars(
                    select(UploadMediaProcessing.session_id)
                    .where(UploadMediaProcessing.state == UploadProcessingState.WAITING_STT.value)
                    .order_by(UploadMediaProcessing.updated_at, UploadMediaProcessing.session_id)
                    .limit(batch_size)
                )
            ).all()
        )

    for session_id in session_ids:
        async with get_sessionmaker()() as db, db.begin():
            processing = await db.scalar(
                select(UploadMediaProcessing)
                .where(UploadMediaProcessing.session_id == session_id)
                .with_for_update(skip_locked=True)
            )
            if processing is None or processing.state != UploadProcessingState.WAITING_STT.value:
                continue
            expected = processing.expected_utterance_count
            if expected is None or expected <= 0:
                processing.state = UploadProcessingState.FAILED.value
                processing.last_error_category = "identity"
                processing.last_error_code = "expected_utterance_identity_missing"
                processing.last_error_message = "waiting upload processing has no expected utterance set"
                processing.completed_at = await _database_now(db)
                failed += 1
                continue

            rows = (
                await db.execute(
                    select(
                        TranscriptionUtterance.id,
                        STTJob.state,
                        TranscriptSegment.id,
                    )
                    .join(STTJob, STTJob.utterance_id == TranscriptionUtterance.id)
                    .outerjoin(
                        TranscriptSegment,
                        and_(
                            TranscriptSegment.session_id == TranscriptionUtterance.session_id,
                            TranscriptSegment.producer_key
                            == func.concat(
                                "utterance:",
                                cast(TranscriptionUtterance.id, String),
                            ),
                        ),
                    )
                    .where(
                        TranscriptionUtterance.session_id == session_id,
                        TranscriptionUtterance.producer_key.like(f"{_UPLOAD_PRODUCER_PREFIX}%"),
                    )
                )
            ).all()
            if len(rows) != expected:
                if len(rows) > expected:
                    processing.state = UploadProcessingState.FAILED.value
                    processing.last_error_category = "identity"
                    processing.last_error_code = "segmentation_identity_drift"
                    processing.last_error_message = "upload utterance set exceeds durable expected count"
                    processing.completed_at = await _database_now(db)
                    failed += 1
                continue
            if any(state == STTJobState.FAILED.value for _, state, _ in rows):
                processing.state = UploadProcessingState.FAILED.value
                processing.last_error_category = "stt"
                processing.last_error_code = "stt_terminal_failure"
                processing.last_error_message = "one or more upload STT jobs reached terminal failure"
                processing.completed_at = await _database_now(db)
                failed += 1
                continue
            all_satisfied = all(
                state == STTJobState.NO_SPEECH.value
                or (state == STTJobState.SUCCEEDED.value and transcript_id is not None)
                for _, state, transcript_id in rows
            )
            if all_satisfied:
                processing.state = UploadProcessingState.SUCCEEDED.value
                processing.outcome_code = "transcribed"
                processing.last_error_category = None
                processing.last_error_code = None
                processing.last_error_message = None
                processing.completed_at = await _database_now(db)
                succeeded += 1
    return succeeded, failed


async def reconcile_upload_processing(batch_size: int = 100) -> MediaReconcileResult:
    created = await ensure_upload_processing_rows(batch_size)
    stt_succeeded, stt_failed = await _converge_waiting_stt(batch_size)
    async with get_sessionmaker()() as db:
        current = await _database_now(db)
        runnable = int(
            await db.scalar(
                select(func.count(UploadMediaProcessing.session_id)).where(
                    _claimable_expression(current)
                )
            )
            or 0
        )
    # Media worker concurrency is intentionally one for alpha. Broker capacity is only a wake;
    # PostgreSQL still selects the current authoritative processing row.
    return MediaReconcileResult(
        created=created,
        stt_succeeded=stt_succeeded,
        stt_failed=stt_failed,
        wake_target=1 if runnable else 0,
    )
