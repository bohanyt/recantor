from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_db_session
from recantor.meeting_essence.http.dev_access import require_trusted_dev_api
from recantor.meeting_essence.http.pagination import DEFAULT_PAGE_SIZE
from recantor.meeting_essence.http.schemas import (
    MeetingEssenceCancelResponse,
    MeetingEssenceDerivationStatusResponse,
    MeetingEssenceDeriveRequest,
    MeetingEssenceDeriveResponse,
    MeetingEssenceEvidenceResponse,
    MeetingEssenceResultPageResponse,
    MeetingEssenceRetryResponse,
)
from recantor.meeting_essence.http.service import (
    MeetingEssenceApiService,
    MeetingEssenceBadRequest,
    MeetingEssenceConflict,
    MeetingEssenceNotFound,
    MeetingEssenceQueueFull,
    MeetingEssenceRetryDenied,
)
from recantor.settings import Settings, get_settings

router = APIRouter(prefix="/sessions", tags=["meeting-essence"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


_CLIENT_ERRORS = (
    MeetingEssenceNotFound,
    MeetingEssenceConflict,
    MeetingEssenceBadRequest,
    MeetingEssenceQueueFull,
    MeetingEssenceRetryDenied,
)


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MeetingEssenceNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, MeetingEssenceBadRequest):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, MeetingEssenceQueueFull):
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    if isinstance(exc, (MeetingEssenceConflict, MeetingEssenceRetryDenied)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="meeting essence failure",
    )


@router.post(
    "/{session_id}/meeting-essence/derivations",
    response_model=MeetingEssenceDeriveResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="submitMeetingEssenceDerivation",
)
async def submit_derivation(
    session_id: UUID,
    body: MeetingEssenceDeriveRequest,
    db: DbSession,
    settings: AppSettings,
) -> MeetingEssenceDeriveResponse:
    require_trusted_dev_api(settings)
    service = MeetingEssenceApiService(db)
    try:
        return await service.submit_derivation(
            session_id=session_id,
            client_request_id=body.client_request_id,
            operation_version=body.operation_version,
        )
    except _CLIENT_ERRORS as exc:
        raise _map_error(exc) from exc


@router.get(
    "/{session_id}/meeting-essence/derivations/{dispatch_id}",
    response_model=MeetingEssenceDerivationStatusResponse,
    operation_id="getMeetingEssenceDerivationStatus",
)
async def get_derivation_status(
    session_id: UUID,
    dispatch_id: UUID,
    db: DbSession,
    settings: AppSettings,
) -> MeetingEssenceDerivationStatusResponse:
    require_trusted_dev_api(settings)
    service = MeetingEssenceApiService(db)
    try:
        return await service.derivation_status(session_id=session_id, dispatch_id=dispatch_id)
    except _CLIENT_ERRORS as exc:
        raise _map_error(exc) from exc


@router.get(
    "/{session_id}/meeting-essence/artifacts/{artifact_id}/results/{layer}",
    response_model=MeetingEssenceResultPageResponse,
    operation_id="getMeetingEssenceResultPage",
)
async def get_result_page(
    session_id: UUID,
    artifact_id: UUID,
    layer: str,
    db: DbSession,
    settings: AppSettings,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=64)] = DEFAULT_PAGE_SIZE,
) -> MeetingEssenceResultPageResponse:
    require_trusted_dev_api(settings)
    service = MeetingEssenceApiService(db)
    try:
        return await service.paginated_results(
            session_id=session_id,
            artifact_id=artifact_id,
            layer=layer,
            cursor=cursor,
            limit=limit,
        )
    except _CLIENT_ERRORS as exc:
        raise _map_error(exc) from exc


@router.get(
    "/{session_id}/meeting-essence/artifacts/{artifact_id}/records/{record_id}/evidence",
    response_model=MeetingEssenceEvidenceResponse,
    operation_id="getMeetingEssenceRecordEvidence",
)
async def get_record_evidence(
    session_id: UUID,
    artifact_id: UUID,
    record_id: str,
    db: DbSession,
    settings: AppSettings,
) -> MeetingEssenceEvidenceResponse:
    require_trusted_dev_api(settings)
    service = MeetingEssenceApiService(db)
    try:
        return await service.resolve_evidence(
            session_id=session_id, artifact_id=artifact_id, record_id=record_id
        )
    except _CLIENT_ERRORS as exc:
        raise _map_error(exc) from exc


@router.post(
    "/{session_id}/meeting-essence/derivations/{dispatch_id}/cancel",
    response_model=MeetingEssenceCancelResponse,
    operation_id="cancelMeetingEssenceDerivation",
)
async def cancel_derivation(
    session_id: UUID,
    dispatch_id: UUID,
    db: DbSession,
    settings: AppSettings,
) -> MeetingEssenceCancelResponse:
    require_trusted_dev_api(settings)
    service = MeetingEssenceApiService(db)
    try:
        await service.cancel(session_id=session_id, dispatch_id=dispatch_id)
    except _CLIENT_ERRORS as exc:
        raise _map_error(exc) from exc
    return MeetingEssenceCancelResponse(dispatch_id=dispatch_id, cancelled=True)


@router.post(
    "/{session_id}/meeting-essence/derivations/{dispatch_id}/retry",
    response_model=MeetingEssenceRetryResponse,
    operation_id="retryMeetingEssenceDerivation",
)
async def retry_derivation(
    session_id: UUID,
    dispatch_id: UUID,
    db: DbSession,
    settings: AppSettings,
) -> MeetingEssenceRetryResponse:
    require_trusted_dev_api(settings)
    service = MeetingEssenceApiService(db)
    try:
        await service.retry(session_id=session_id, dispatch_id=dispatch_id)
    except _CLIENT_ERRORS as exc:
        raise _map_error(exc) from exc
    return MeetingEssenceRetryResponse(dispatch_id=dispatch_id, accepted=True)
