from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_db_session
from recantor.upload_contracts import (
    CreateUploadSessionRequest,
    TusHookRequest,
    UploadSessionResponse,
)
from recantor.upload_storage import UploadStorageError
from recantor.uploads import (
    UploadConflict,
    UploadExpired,
    UploadForbidden,
    UploadNotFound,
    UploadPolicyError,
    create_upload_session,
    get_upload_session,
    process_tusd_hook,
    upload_session_response,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
CapabilityHeader = Annotated[
    str,
    Header(alias="X-Recantor-Upload-Token", min_length=43, max_length=128),
]
UPLOAD_ERRORS = (
    UploadNotFound,
    UploadExpired,
    UploadForbidden,
    UploadPolicyError,
    UploadConflict,
)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UploadNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, UploadExpired):
        return HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc))
    if isinstance(exc, UploadForbidden):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, UploadPolicyError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    if isinstance(exc, UploadConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="upload failure")


@router.post(
    "",
    response_model=UploadSessionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createUploadSession",
)
async def create_upload(body: CreateUploadSessionRequest, db: DbSession) -> UploadSessionResponse:
    try:
        session, record = await create_upload_session(
            db,
            client_request_id=body.client_request_id,
            capability_token=body.capability_token,
            original_filename=body.original_filename,
            content_type=body.content_type,
            byte_length=body.byte_length,
            duration_ms=body.duration_ms,
        )
    except UPLOAD_ERRORS as exc:
        raise _http_error(exc) from exc
    return upload_session_response(session, record)


@router.post("/tusd-hooks", include_in_schema=False)
async def tusd_hook(body: TusHookRequest, db: DbSession) -> JSONResponse:
    try:
        response = await process_tusd_hook(db, body)
    except UploadStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="durable upload storage is temporarily unavailable",
        ) from exc
    return JSONResponse(content=response.model_dump(by_alias=True, exclude_none=True))


@router.get(
    "/{session_id}",
    response_model=UploadSessionResponse,
    operation_id="getUploadSession",
)
async def read_upload(
    session_id: UUID,
    capability_token: CapabilityHeader,
    db: DbSession,
) -> UploadSessionResponse:
    try:
        session, record = await get_upload_session(
            db,
            session_id=session_id,
            capability_token=capability_token,
        )
    except UPLOAD_ERRORS as exc:
        raise _http_error(exc) from exc
    return upload_session_response(session, record)
