from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_db_session
from recantor.schemas import TranscriptPageResponse, TranscriptSegmentResponse
from recantor.transcript import TranscriptConflict, TranscriptNotFound, read_transcript_segments

router = APIRouter(prefix="/sessions", tags=["transcript"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get(
    "/{session_id}/transcript",
    response_model=TranscriptPageResponse,
    operation_id="getSessionTranscript",
)
async def get_session_transcript(
    session_id: UUID,
    db: DbSession,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> TranscriptPageResponse:
    try:
        segments, has_more = await read_transcript_segments(
            db,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except TranscriptNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except TranscriptConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    next_after_sequence = segments[-1].sequence if segments else after_sequence
    return TranscriptPageResponse(
        session_id=session_id,
        after_sequence=after_sequence,
        next_after_sequence=next_after_sequence,
        has_more=has_more,
        segments=[TranscriptSegmentResponse.model_validate(segment) for segment in segments],
    )
