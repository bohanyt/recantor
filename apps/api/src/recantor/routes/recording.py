from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_db_session
from recantor.recording import (
    RecordingConflict,
    RecordingNotFound,
    StaleWriter,
    accept_chunk,
    claim_capture,
    compress_sequences,
    create_live_session,
    declare_gap,
    finalize_session,
    get_recording_session,
    heartbeat,
    highest_contiguous,
    recording_state,
)
from recantor.schemas import (
    CaptureClaimRequest,
    ChunkAckResponse,
    CreateLiveSessionRequest,
    FinalizeSessionRequest,
    FinalizeSessionResponse,
    GapDeclarationRequest,
    HeartbeatRequest,
    RecordingGapResponse,
    RecordingSessionResponse,
    RecordingStateResponse,
)

router = APIRouter(prefix="/sessions", tags=["recording"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RecordingNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, StaleWriter):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, RecordingConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="recording failure",
    )


@router.post(
    "/live",
    response_model=RecordingSessionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createLiveSession",
)
async def create_live(body: CreateLiveSessionRequest, db: DbSession) -> RecordingSessionResponse:
    try:
        session = await create_live_session(
            db,
            client_request_id=body.client_request_id,
            writer_id=body.writer_id,
            recovery_token=body.recovery_token,
        )
    except (RecordingNotFound, RecordingConflict) as exc:
        raise _http_error(exc) from exc
    return RecordingSessionResponse.model_validate(session)


@router.get(
    "/{session_id}", response_model=RecordingSessionResponse, operation_id="getRecordingSession"
)
async def get_session(session_id: UUID, db: DbSession) -> RecordingSessionResponse:
    try:
        session = await get_recording_session(db, session_id)
    except (RecordingNotFound, RecordingConflict) as exc:
        raise _http_error(exc) from exc
    return RecordingSessionResponse.model_validate(session)


@router.post(
    "/{session_id}/capture/claim",
    response_model=RecordingSessionResponse,
    operation_id="claimRecordingSession",
)
async def claim_session(
    session_id: UUID, body: CaptureClaimRequest, db: DbSession
) -> RecordingSessionResponse:
    try:
        session = await claim_capture(
            db,
            session_id=session_id,
            writer_id=body.writer_id,
            expected_epoch=body.expected_epoch,
            recovery_token=body.recovery_token,
        )
    except (RecordingNotFound, RecordingConflict) as exc:
        raise _http_error(exc) from exc
    return RecordingSessionResponse.model_validate(session)


@router.post(
    "/{session_id}/heartbeat",
    response_model=RecordingSessionResponse,
    operation_id="heartbeatRecordingSession",
)
async def session_heartbeat(
    session_id: UUID, body: HeartbeatRequest, db: DbSession
) -> RecordingSessionResponse:
    try:
        session = await heartbeat(
            db,
            session_id=session_id,
            writer_id=body.writer_id,
            capture_epoch=body.capture_epoch,
        )
    except (RecordingNotFound, RecordingConflict) as exc:
        raise _http_error(exc) from exc
    return RecordingSessionResponse.model_validate(session)


@router.get(
    "/{session_id}/recording-state",
    response_model=RecordingStateResponse,
    operation_id="getRecordingState",
)
async def get_session_recording_state(session_id: UUID, db: DbSession) -> RecordingStateResponse:
    try:
        session, sequences, gaps = await recording_state(db, session_id)
    except (RecordingNotFound, RecordingConflict) as exc:
        raise _http_error(exc) from exc
    return RecordingStateResponse(
        session=RecordingSessionResponse.model_validate(session),
        accepted_ranges=compress_sequences(sequences),
        accepted_count=len(sequences),
        highest_contiguous_sequence=highest_contiguous(sequences),
        gaps=[RecordingGapResponse.model_validate(gap) for gap in gaps],
    )


@router.put(
    "/{session_id}/chunks/{sequence}",
    response_model=ChunkAckResponse,
    operation_id="putRecordingChunk",
)
async def put_chunk(
    session_id: UUID,
    sequence: int,
    db: DbSession,
    payload: Annotated[bytes, Body(media_type="application/octet-stream")],
    writer_id: Annotated[str, Query(min_length=8, max_length=128)],
    capture_epoch: Annotated[int, Query(ge=1)],
    monotonic_start_ms: Annotated[int, Query(ge=0)],
    monotonic_end_ms: Annotated[int, Query(ge=0)],
    sha256: Annotated[str, Query(pattern=r"^[0-9a-fA-F]{64}$")],
    content_type: Annotated[str, Query(min_length=3, max_length=128)] = "application/octet-stream",
) -> ChunkAckResponse:
    try:
        chunk, idempotent = await accept_chunk(
            db,
            session_id=session_id,
            sequence=sequence,
            writer_id=writer_id,
            capture_epoch=capture_epoch,
            monotonic_start_ms=monotonic_start_ms,
            monotonic_end_ms=monotonic_end_ms,
            content_type=content_type,
            payload=payload,
            declared_sha256=sha256,
        )
    except (RecordingNotFound, RecordingConflict) as exc:
        raise _http_error(exc) from exc
    return ChunkAckResponse(
        session_id=chunk.session_id,
        sequence=chunk.sequence,
        sha256=chunk.sha256,
        byte_length=chunk.byte_length,
        storage_key=chunk.storage_key,
        idempotent=idempotent,
    )


@router.post(
    "/{session_id}/gaps",
    response_model=RecordingGapResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="declareRecordingGap",
)
async def post_gap(
    session_id: UUID, body: GapDeclarationRequest, db: DbSession
) -> RecordingGapResponse:
    try:
        gap = await declare_gap(
            db,
            session_id=session_id,
            client_gap_id=body.client_gap_id,
            writer_id=body.writer_id,
            capture_epoch=body.capture_epoch,
            sequence_start=body.sequence_start,
            sequence_end=body.sequence_end,
            wall_started_at=body.wall_started_at,
            wall_ended_at=body.wall_ended_at,
            reason=body.reason,
        )
    except (RecordingNotFound, RecordingConflict) as exc:
        raise _http_error(exc) from exc
    return RecordingGapResponse.model_validate(gap)


@router.post(
    "/{session_id}/finalize",
    response_model=FinalizeSessionResponse,
    operation_id="finalizeRecordingSession",
)
async def finalize(
    session_id: UUID, body: FinalizeSessionRequest, db: DbSession
) -> FinalizeSessionResponse:
    try:
        session, missing, gaps = await finalize_session(
            db,
            session_id=session_id,
            writer_id=body.writer_id,
            capture_epoch=body.capture_epoch,
            final_sequence=body.final_sequence,
            final_monotonic_end_ms=body.final_monotonic_end_ms,
            gap_sequences=body.gap_sequences,
        )
    except (RecordingNotFound, RecordingConflict) as exc:
        raise _http_error(exc) from exc
    return FinalizeSessionResponse(
        session=RecordingSessionResponse.model_validate(session),
        complete=not missing,
        missing_sequences=missing,
        gaps=[RecordingGapResponse.model_validate(gap) for gap in gaps],
    )
