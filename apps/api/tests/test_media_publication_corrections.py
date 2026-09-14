from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import recantor.media_storage as media_storage_module
from recantor.db import get_sessionmaker
from recantor.media_processing import (
    MediaProcessingStale,
    _publish_prepared_normalized,
    _sample_bounds_to_milliseconds,
    claim_next_upload_processing,
)
from recantor.media_spec import (
    NORMALIZATION_SPEC_ID,
    SEGMENTATION_PARAMS_JSON,
    SEGMENTATION_SPEC_ID,
    UPLOAD_FRAME_SAMPLES,
    UPLOAD_SAMPLE_RATE,
)
from recantor.media_storage import FilesystemNormalizedMediaStorage, NormalizedMedia
from recantor.models import (
    RecordingSession,
    SessionKind,
    SessionState,
    UploadMediaProcessing,
    UploadProcessingState,
    UploadRecord,
)
from recantor.realtime_audio import encode_pcm_wav
from recantor.settings import get_settings


def _candidate_wav(sample_count: int, amplitude: int) -> bytes:
    pcm = b"".join(
        struct.pack("<h", amplitude if index % 2 == 0 else -amplitude)
        for index in range(sample_count)
    )
    return encode_pcm_wav(pcm, sample_rate=UPLOAD_SAMPLE_RATE)


async def _create_claimable_processing() -> tuple[object, str, str, int]:
    session_id = uuid4()
    now = datetime.now(UTC)
    source_key = f"uploads/tus/{uuid4().hex}"
    source_bytes = b"immutable source identity"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    source_byte_length = len(source_bytes)
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
        original_filename="race.wav",
        content_type="audio/wav",
        declared_byte_length=source_byte_length,
        received_bytes=source_byte_length,
        tus_upload_id=uuid4().hex,
        storage_key=source_key,
        sha256=source_sha256,
        byte_length=source_byte_length,
        expires_at=now + timedelta(hours=1),
        completed_at=now,
        created_at=now,
        updated_at=now,
    )
    processing = UploadMediaProcessing(
        session_id=session_id,
        source_storage_key=source_key,
        source_sha256=source_sha256,
        source_byte_length=source_byte_length,
        source_completed_at=now,
        state=UploadProcessingState.PENDING.value,
        normalization_spec_id=NORMALIZATION_SPEC_ID,
        segmentation_spec_id=SEGMENTATION_SPEC_ID,
        segmentation_params_json=SEGMENTATION_PARAMS_JSON,
    )
    async with get_sessionmaker()() as db:
        db.add(session)
        await db.flush()
        db.add(record)
        await db.flush()
        db.add(processing)
        await db.commit()
    return session_id, source_key, source_sha256, source_byte_length


@pytest.mark.asyncio
async def test_normalized_publication_is_claim_fenced_atomic_first_wins_under_reclaim_race(
    clean_recording_state,
    monkeypatch,
):
    del clean_recording_state
    session_id, source_key, source_sha256, source_byte_length = await _create_claimable_processing()
    storage = FilesystemNormalizedMediaStorage(get_settings().audio_storage_path)

    t0 = datetime.now(UTC)
    old_claim = await claim_next_upload_processing(now=t0)
    assert old_claim is not None and old_claim.session_id == session_id

    old_bytes = _candidate_wav(UPLOAD_SAMPLE_RATE // 4, 4000)
    new_a_bytes = _candidate_wav(UPLOAD_SAMPLE_RATE // 4, 7000)
    new_b_bytes = _candidate_wav(UPLOAD_SAMPLE_RATE // 4, 10000)
    old_temp = storage.private_temp_path(session_id)
    new_a_temp = storage.private_temp_path(session_id)
    new_b_temp = storage.private_temp_path(session_id)
    old_temp.write_bytes(old_bytes)
    new_a_temp.write_bytes(new_a_bytes)
    new_b_temp.write_bytes(new_b_bytes)

    digest_started = threading.Event()
    release_old_digest = threading.Event()
    original_digest = media_storage_module._digest_file

    def gated_digest(path: Path):
        if path == old_temp:
            digest_started.set()
            assert release_old_digest.wait(timeout=5)
        return original_digest(path)

    monkeypatch.setattr(media_storage_module, "_digest_file", gated_digest)
    old_prepare_task = asyncio.create_task(
        asyncio.to_thread(
            storage.prepare_temp,
            session_id=session_id,
            source_storage_key=source_key,
            source_sha256=source_sha256,
            source_byte_length=source_byte_length,
            selected_audio_stream=0,
            temp_path=old_temp,
        )
    )
    assert await asyncio.to_thread(digest_started.wait, 2)

    # Reclaim while the old worker is still digesting/fsyncing private bytes. The new owner is
    # durable before the old candidate is allowed to reach the final publication primitive.
    new_claim = await claim_next_upload_processing(now=old_claim.expires_at + timedelta(seconds=1))
    assert new_claim is not None
    assert new_claim.session_id == session_id
    assert new_claim.token != old_claim.token

    new_a_prepared = await asyncio.to_thread(
        storage.prepare_temp,
        session_id=session_id,
        source_storage_key=source_key,
        source_sha256=source_sha256,
        source_byte_length=source_byte_length,
        selected_audio_stream=0,
        temp_path=new_a_temp,
    )
    new_b_prepared = await asyncio.to_thread(
        storage.prepare_temp,
        session_id=session_id,
        source_storage_key=source_key,
        source_sha256=source_sha256,
        source_byte_length=source_byte_length,
        selected_audio_stream=0,
        temp_path=new_b_temp,
    )
    release_old_digest.set()
    old_prepared = await asyncio.wait_for(old_prepare_task, timeout=3)

    old_result, new_a_result, new_b_result = await asyncio.gather(
        _publish_prepared_normalized(old_claim, storage, old_prepared),
        _publish_prepared_normalized(new_claim, storage, new_a_prepared),
        _publish_prepared_normalized(new_claim, storage, new_b_prepared),
        return_exceptions=True,
    )

    assert isinstance(old_result, MediaProcessingStale)
    assert isinstance(new_a_result, NormalizedMedia)
    assert isinstance(new_b_result, NormalizedMedia)
    assert new_a_result == new_b_result

    committed = storage.verify_committed(
        session_id=session_id,
        source_storage_key=source_key,
        source_sha256=source_sha256,
        source_byte_length=source_byte_length,
        selected_audio_stream=0,
    )
    assert committed == new_a_result
    assert committed.sha256 != hashlib.sha256(old_bytes).hexdigest()
    assert committed.sha256 in {
        hashlib.sha256(new_a_bytes).hexdigest(),
        hashlib.sha256(new_b_bytes).hexdigest(),
    }

    manifest_bytes = storage.manifest_path_for(session_id).read_bytes()
    manifest = json.loads(manifest_bytes)
    winner_path = storage.normalized_path(session_id, committed.key)
    assert manifest["normalized_sha256"] == committed.sha256
    assert manifest["normalized_byte_length"] == committed.byte_length
    assert manifest["total_samples"] == committed.total_samples
    assert hashlib.sha256(winner_path.read_bytes()).hexdigest() == committed.sha256
    assert winner_path.stat().st_size == committed.byte_length

    async with get_sessionmaker()() as db:
        processing = await db.get(UploadMediaProcessing, session_id)
    assert processing is not None
    assert processing.claim_token == new_claim.token
    assert processing.normalized_storage_key == committed.key
    assert processing.normalized_sha256 == committed.sha256
    assert processing.normalized_byte_length == committed.byte_length
    assert processing.normalized_total_samples == committed.total_samples

    # Re-publishing a different candidate with the same current owner cannot replace the winner.
    late_temp = storage.private_temp_path(session_id)
    late_bytes = _candidate_wav(UPLOAD_SAMPLE_RATE // 4, 12000)
    late_temp.write_bytes(late_bytes)
    late_prepared = await asyncio.to_thread(
        storage.prepare_temp,
        session_id=session_id,
        source_storage_key=source_key,
        source_sha256=source_sha256,
        source_byte_length=source_byte_length,
        selected_audio_stream=0,
        temp_path=late_temp,
    )
    late_result = await _publish_prepared_normalized(new_claim, storage, late_prepared)
    assert late_result == committed
    assert storage.manifest_path_for(session_id).read_bytes() == manifest_bytes
    assert hashlib.sha256(winner_path.read_bytes()).hexdigest() == committed.sha256


def test_partial_final_frame_timeline_uses_floor_start_and_ceil_end():
    start_sample = UPLOAD_FRAME_SAMPLES * 2
    end_sample = start_sample + UPLOAD_FRAME_SAMPLES + 1

    start_ms, end_ms = _sample_bounds_to_milliseconds(start_sample, end_sample)

    assert start_ms == 40
    assert end_ms == 61  # 60.0625 ms EOF must not be truncated to 60 ms.
    assert start_ms * UPLOAD_SAMPLE_RATE <= start_sample * 1000
    assert end_ms * UPLOAD_SAMPLE_RATE >= end_sample * 1000
