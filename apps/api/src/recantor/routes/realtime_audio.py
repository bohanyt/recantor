from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select

from recantor.db import get_sessionmaker
from recantor.models import RecordingSession, SessionState
from recantor.realtime_audio import (
    EnergyEndpointDetector,
    RealtimeAudioProtocolError,
    RealtimeAudioStart,
    UtteranceCandidate,
    VadConfig,
    encode_pcm_wav,
    parse_pcm_packet,
    sample_to_timeline_ms,
)
from recantor.settings import get_settings
from recantor.utterance import (
    UtteranceWorkConflict,
    UtteranceWorkNotFound,
    UtteranceWorkStorageError,
    commit_utterance_work,
)

router = APIRouter(prefix="/sessions", tags=["realtime-audio"])
_ALLOWED_LIVE_STATES = {
    SessionState.RECORDING.value,
    SessionState.RECOVERING.value,
    SessionState.INTERRUPTED.value,
}
_MAX_HANDSHAKE_CHARS = 4_096
_MAX_CONTROL_CHARS = 256


async def _owner_is_current(session_id: UUID, start: RealtimeAudioStart) -> bool:
    async with get_sessionmaker()() as db:
        session = await db.scalar(select(RecordingSession).where(RecordingSession.id == session_id))
    return bool(
        session is not None
        and session.active_writer_id == start.writer_id
        and session.capture_epoch == start.capture_epoch
        and session.state in _ALLOWED_LIVE_STATES
    )


async def _send_error(websocket: WebSocket, code: str, detail: str) -> None:
    try:
        await websocket.send_json({"type": "error", "code": code, "detail": detail})
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _safe_close(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except (RuntimeError, WebSocketDisconnect):
        pass


def _is_stop_message(text: str) -> bool:
    if len(text) > _MAX_CONTROL_CHARS:
        raise RealtimeAudioProtocolError("realtime control message exceeds size limit")
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RealtimeAudioProtocolError("invalid realtime control JSON") from exc
    return isinstance(message, dict) and message == {"type": "stop"}


async def _commit_candidate(
    websocket: WebSocket,
    *,
    session_id: UUID,
    start: RealtimeAudioStart,
    candidate: UtteranceCandidate,
    notify: bool = True,
) -> None:
    start_ms = sample_to_timeline_ms(
        start.timeline_base_ms,
        candidate.start_sample,
        start.sample_rate,
    )
    end_ms = max(
        start_ms + 1,
        sample_to_timeline_ms(
            start.timeline_base_ms,
            candidate.end_sample,
            start.sample_rate,
        ),
    )
    producer_key = (
        f"live:{start.capture_epoch}:{start.timeline_base_ms}:"
        f"{candidate.start_sample}:{candidate.end_sample}"
    )
    wav_payload = encode_pcm_wav(candidate.pcm, sample_rate=start.sample_rate)

    async with get_sessionmaker()() as db:
        work, idempotent = await commit_utterance_work(
            db,
            session_id=session_id,
            producer_key=producer_key,
            start_ms=start_ms,
            end_ms=end_ms,
            content_type="audio/wav",
            payload=wav_payload,
            expected_writer_id=start.writer_id,
            expected_capture_epoch=start.capture_epoch,
        )

    if notify:
        await websocket.send_json(
            {
                "type": "utterance_committed",
                "id": str(work.id),
                "sequence": work.sequence,
                "start_ms": work.start_ms,
                "end_ms": work.end_ms,
                "idempotent": idempotent,
            }
        )


def _vad_config() -> VadConfig:
    settings = get_settings()
    return VadConfig(
        pre_roll_ms=settings.realtime_vad_pre_roll_ms,
        min_voiced_ms=settings.realtime_vad_min_voiced_ms,
        silence_ms=settings.realtime_vad_silence_ms,
        hard_max_ms=settings.realtime_vad_hard_max_ms,
        absolute_threshold_dbfs=settings.realtime_vad_absolute_threshold_dbfs,
        noise_margin_db=settings.realtime_vad_noise_margin_db,
    )


@router.websocket("/{session_id}/realtime-audio", name="sessionRealtimeAudio")
async def session_realtime_audio(websocket: WebSocket, session_id: UUID) -> None:
    await websocket.accept()
    start: RealtimeAudioStart | None = None
    detector: EnergyEndpointDetector | None = None
    expected_offset: int | None = None
    clean_stop = False
    disconnect_code: int | None = None

    try:
        first = await websocket.receive()
        if first.get("type") == "websocket.disconnect":
            disconnect_code = first.get("code")
            return
        first_text = first.get("text")
        if first_text is None:
            raise RealtimeAudioProtocolError("first realtime message must be a JSON start handshake")
        if len(first_text) > _MAX_HANDSHAKE_CHARS:
            raise RealtimeAudioProtocolError("realtime start handshake exceeds size limit")
        try:
            start = RealtimeAudioStart.model_validate_json(first_text)
        except ValidationError as exc:
            raise RealtimeAudioProtocolError("invalid realtime start handshake") from exc
        if not await _owner_is_current(session_id, start):
            raise UtteranceWorkConflict("capture writer or epoch is no longer active")

        settings = get_settings()
        detector = EnergyEndpointDetector(start.sample_rate, _vad_config())
        await websocket.send_json(
            {
                "type": "ready",
                "stream_id": str(start.stream_id),
                "sample_rate": start.sample_rate,
                "pre_roll_ms": settings.realtime_vad_pre_roll_ms,
                "silence_ms": settings.realtime_vad_silence_ms,
                "hard_max_ms": settings.realtime_vad_hard_max_ms,
            }
        )

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                disconnect_code = message.get("code")
                break

            text = message.get("text")
            if text is not None:
                if not _is_stop_message(text):
                    raise RealtimeAudioProtocolError("unexpected realtime control message")
                clean_stop = True
                tail = detector.flush()
                if tail is not None:
                    await _commit_candidate(
                        websocket,
                        session_id=session_id,
                        start=start,
                        candidate=tail,
                    )
                await websocket.send_json({"type": "stopped"})
                await _safe_close(websocket, 1000)
                break

            binary = message.get("bytes")
            if binary is None:
                raise RealtimeAudioProtocolError("realtime message must contain PCM bytes")
            frame = parse_pcm_packet(binary, max_bytes=settings.realtime_max_pcm_packet_bytes)
            max_frame_samples = max(1, start.sample_rate // 5)
            if frame.sample_count > max_frame_samples:
                raise RealtimeAudioProtocolError("PCM frame exceeds 200 ms transport bound")

            if expected_offset is not None and frame.sample_offset != expected_offset:
                detector.reset()
                await websocket.send_json(
                    {
                        "type": "discontinuity",
                        "expected_sample_offset": expected_offset,
                        "received_sample_offset": frame.sample_offset,
                    }
                )
            expected_offset = frame.end_sample

            for candidate in detector.feed(frame):
                await _commit_candidate(
                    websocket,
                    session_id=session_id,
                    start=start,
                    candidate=candidate,
                )

    except WebSocketDisconnect as exc:
        disconnect_code = exc.code
    except UtteranceWorkNotFound as exc:
        await _send_error(websocket, "session_not_found", str(exc))
        await _safe_close(websocket, 1008)
    except UtteranceWorkConflict as exc:
        await _send_error(websocket, "stale_or_conflicting_capture", str(exc))
        await _safe_close(websocket, 4009)
    except UtteranceWorkStorageError as exc:
        await _send_error(websocket, "utterance_storage_failed", str(exc))
        await _safe_close(websocket, 1011)
    except RealtimeAudioProtocolError as exc:
        await _send_error(websocket, "protocol_error", str(exc))
        await _safe_close(websocket, 1003)
    finally:
        if (
            not clean_stop
            and disconnect_code in {1000, 1001}
            and start is not None
            and detector is not None
        ):
            tail = detector.flush()
            if tail is not None:
                try:
                    await _commit_candidate(
                        websocket,
                        session_id=session_id,
                        start=start,
                        candidate=tail,
                        notify=False,
                    )
                except (UtteranceWorkConflict, UtteranceWorkNotFound, UtteranceWorkStorageError):
                    pass
