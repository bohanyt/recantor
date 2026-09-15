from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_db_session
from recantor.schemas import TranscriptPageResponse, TranscriptSegmentResponse
from recantor.transcript import TranscriptConflict, TranscriptNotFound, read_transcript_segments
from recantor.upload_contracts import (
    CreateUploadSessionRequest,
    TusHookRequest,
    UploadSessionResponse,
)
from recantor.upload_result_contracts import UploadExportFormat, UploadResultStatusResponse
from recantor.upload_results import (
    UploadResultNotReady,
    ensure_upload_export_ready,
    export_spec,
    iter_upload_export,
    read_upload_result_status,
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


@router.get(
    "/{session_id}/result",
    response_model=UploadResultStatusResponse,
    operation_id="getUploadResultStatus",
)
async def get_upload_result(
    session_id: UUID,
    capability_token: CapabilityHeader,
    db: DbSession,
) -> UploadResultStatusResponse:
    try:
        return await read_upload_result_status(
            db,
            session_id=session_id,
            capability_token=capability_token,
        )
    except UPLOAD_ERRORS as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{session_id}/transcript",
    response_model=TranscriptPageResponse,
    operation_id="getUploadTranscript",
)
async def get_upload_transcript(
    session_id: UUID,
    capability_token: CapabilityHeader,
    db: DbSession,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> TranscriptPageResponse:
    try:
        await get_upload_session(db, session_id=session_id, capability_token=capability_token)
        segments, has_more = await read_transcript_segments(
            db,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except UPLOAD_ERRORS as exc:
        raise _http_error(exc) from exc
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


@router.get(
    "/{session_id}/exports/{export_format}",
    operation_id="downloadUploadTranscriptExport",
)
async def download_upload_export(
    session_id: UUID,
    export_format: UploadExportFormat,
    capability_token: CapabilityHeader,
    db: DbSession,
) -> StreamingResponse:
    try:
        await ensure_upload_export_ready(
            db,
            session_id=session_id,
            capability_token=capability_token,
        )
    except UPLOAD_ERRORS as exc:
        raise _http_error(exc) from exc
    except UploadResultNotReady as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    spec = export_spec(export_format)
    filename = f"recantor-{session_id}.{spec.extension}"
    return StreamingResponse(
        iter_upload_export(session_id=session_id, export_format=export_format),
        media_type=spec.media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
