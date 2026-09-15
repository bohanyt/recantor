from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_sessionmaker
from recantor.models import TranscriptSegment, UploadMediaProcessing, UploadProcessingState
from recantor.transcript import read_transcript_segments
from recantor.upload_result_contracts import (
    UploadExportFormat,
    UploadResultState,
    UploadResultStatusResponse,
)
from recantor.uploads import get_upload_session


class UploadResultError(RuntimeError):
    pass


class UploadResultNotReady(UploadResultError):
    pass


@dataclass(frozen=True)
class UploadExportSpec:
    media_type: str
    extension: str


_EXPORT_SPECS: dict[UploadExportFormat, UploadExportSpec] = {
    "txt": UploadExportSpec("text/plain; charset=utf-8", "txt"),
    "json": UploadExportSpec("application/json; charset=utf-8", "json"),
    "vtt": UploadExportSpec("text/vtt; charset=utf-8", "vtt"),
    "srt": UploadExportSpec("application/x-subrip; charset=utf-8", "srt"),
}
_EXPORT_PAGE_SIZE = 500


def export_spec(export_format: UploadExportFormat) -> UploadExportSpec:
    return _EXPORT_SPECS[export_format]


def _safe_failure_message() -> str:
    return "Transcription could not be completed. Start a fresh upload to try again."


async def _segment_count(db: AsyncSession, session_id: UUID) -> int:
    return int(
        await db.scalar(
            select(func.count(TranscriptSegment.id)).where(
                TranscriptSegment.session_id == session_id
            )
        )
        or 0
    )


async def read_upload_result_status(
    db: AsyncSession,
    *,
    session_id: UUID,
    capability_token: str,
) -> UploadResultStatusResponse:
    session, record = await get_upload_session(
        db,
        session_id=session_id,
        capability_token=capability_token,
    )
    count = await _segment_count(db, session_id)

    if record.completed_at is None:
        failed = session.state == "failed" or record.failure_code is not None
        return UploadResultStatusResponse(
            session_id=session_id,
            state="failed" if failed else "uploading",
            transcript_segment_count=count,
            exports_available=False,
            completed_at=None,
            expires_at=record.expires_at,
            failure_message=_safe_failure_message() if failed else None,
        )

    processing = await db.get(UploadMediaProcessing, session_id)
    if processing is None:
        state: UploadResultState = "preparing"
        completed_at = None
        failure_message = None
    elif processing.state in {
        UploadProcessingState.PENDING.value,
        UploadProcessingState.CLAIMED.value,
        UploadProcessingState.RETRY_WAIT.value,
    }:
        state = "preparing"
        completed_at = None
        failure_message = None
    elif processing.state == UploadProcessingState.WAITING_STT.value:
        state = "transcribing"
        completed_at = None
        failure_message = None
    elif processing.state == UploadProcessingState.FAILED.value:
        state = "failed"
        completed_at = processing.completed_at
        failure_message = _safe_failure_message()
    elif processing.state == UploadProcessingState.SUCCEEDED.value:
        state = "complete" if count > 0 else "no_speech"
        completed_at = processing.completed_at
        failure_message = None
    else:
        state = "failed"
        completed_at = processing.completed_at
        failure_message = _safe_failure_message()

    return UploadResultStatusResponse(
        session_id=session_id,
        state=state,
        transcript_segment_count=count,
        exports_available=state in {"complete", "no_speech"},
        completed_at=completed_at,
        expires_at=record.expires_at,
        failure_message=failure_message,
    )


def _timestamp(milliseconds: int, *, separator: str) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def _normalized_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


async def iter_upload_export(
    *,
    session_id: UUID,
    export_format: UploadExportFormat,
) -> AsyncIterator[bytes]:
    if export_format == "vtt":
        yield b"WEBVTT\n\n"
    elif export_format == "json":
        prefix = json.dumps({"session_id": str(session_id)}, separators=(",", ":"))[:-1]
        yield f'{prefix},"segments":['.encode()

    after_sequence = 0
    cue_number = 0
    json_first = True
    async with get_sessionmaker()() as db:
        while True:
            segments, has_more = await read_transcript_segments(
                db,
                session_id=session_id,
                after_sequence=after_sequence,
                limit=_EXPORT_PAGE_SIZE,
            )
            for segment in segments:
                text = _normalized_text(segment.text)
                if export_format == "txt":
                    yield f"{text}\n".encode()
                elif export_format == "json":
                    payload = {
                        "sequence": segment.sequence,
                        "start_ms": segment.start_ms,
                        "end_ms": segment.end_ms,
                        "text": text,
                        "language": segment.language,
                    }
                    encoded = json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                    if not json_first:
                        yield b","
                    yield encoded
                    json_first = False
                elif export_format == "vtt":
                    start = _timestamp(segment.start_ms, separator=".")
                    end = _timestamp(segment.end_ms, separator=".")
                    yield f"{start} --> {end}\n{text}\n\n".encode()
                else:
                    cue_number += 1
                    start = _timestamp(segment.start_ms, separator=",")
                    end = _timestamp(segment.end_ms, separator=",")
                    yield f"{cue_number}\n{start} --> {end}\n{text}\n\n".encode()
            if not segments or not has_more:
                break
            after_sequence = segments[-1].sequence

    if export_format == "json":
        yield b"]}\n"


async def ensure_upload_export_ready(
    db: AsyncSession,
    *,
    session_id: UUID,
    capability_token: str,
) -> UploadResultStatusResponse:
    result = await read_upload_result_status(
        db,
        session_id=session_id,
        capability_token=capability_token,
    )
    if not result.exports_available:
        raise UploadResultNotReady("upload transcript result is not complete")
    return result
