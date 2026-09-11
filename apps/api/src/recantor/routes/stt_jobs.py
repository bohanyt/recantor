from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_db_session
from recantor.stt_jobs import STTJobError, STTJobNotFound, read_stt_diagnostics
from recantor.stt_schemas import STTJobFailureResponse, STTSchedulingDiagnosticsResponse

router = APIRouter(prefix="/sessions", tags=["stt-scheduling"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get(
    "/{session_id}/stt-scheduling",
    response_model=STTSchedulingDiagnosticsResponse,
    operation_id="getSessionSttScheduling",
)
async def get_session_stt_scheduling(
    session_id: UUID,
    db: DbSession,
    failure_limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> STTSchedulingDiagnosticsResponse:
    try:
        counts, failures = await read_stt_diagnostics(
            db,
            session_id=session_id,
            failure_limit=failure_limit,
        )
    except STTJobNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except STTJobError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return STTSchedulingDiagnosticsResponse(
        session_id=session_id,
        total=sum(counts.values()),
        counts=counts,
        recent_failures=[
            STTJobFailureResponse.model_validate(failure) for failure in failures
        ],
    )
