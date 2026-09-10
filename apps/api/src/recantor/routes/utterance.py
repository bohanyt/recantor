from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_db_session
from recantor.schemas import UtteranceWorkPageResponse, UtteranceWorkResponse
from recantor.utterance import UtteranceWorkConflict, UtteranceWorkNotFound, read_utterance_work

router = APIRouter(prefix="/sessions", tags=["transcription-work"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get(
    "/{session_id}/utterances",
    response_model=UtteranceWorkPageResponse,
    operation_id="getSessionUtteranceWork",
)
async def get_session_utterance_work(
    session_id: UUID,
    db: DbSession,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> UtteranceWorkPageResponse:
    try:
        utterances, has_more = await read_utterance_work(
            db,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except UtteranceWorkNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UtteranceWorkConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    next_after_sequence = utterances[-1].sequence if utterances else after_sequence
    return UtteranceWorkPageResponse(
        session_id=session_id,
        after_sequence=after_sequence,
        next_after_sequence=next_after_sequence,
        has_more=has_more,
        utterances=[UtteranceWorkResponse.model_validate(item) for item in utterances],
    )
