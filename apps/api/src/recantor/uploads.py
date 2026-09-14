from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import PurePath
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.media_spec import (
    NORMALIZATION_SPEC_ID,
    SEGMENTATION_PARAMS_JSON,
    SEGMENTATION_SPEC_ID,
)
from recantor.models import (
    RecordingSession,
    SessionKind,
    SessionState,
    UploadMediaProcessing,
    UploadProcessingState,
    UploadRecord,
)
from recantor.settings import get_settings
from recantor.upload_contracts import (
    TusHookHttpResponse,
    TusHookRequest,
    TusHookResponse,
    UploadSessionResponse,
)
from recantor.upload_storage import FilesystemUploadStorage, StoredUpload, UploadStorageError


class UploadError(RuntimeError):
    pass


class UploadNotFound(UploadError):
    pass


class UploadConflict(UploadError):
    pass


class UploadForbidden(UploadError):
    pass


class UploadExpired(UploadForbidden):
    pass


class UploadPolicyError(UploadError):
    pass


_ALLOWED_CONTENT_TYPES: dict[str, set[str]] = {
    ".wav": {"audio/wav", "audio/x-wav", "audio/vnd.wave", "application/octet-stream"},
    ".mp3": {"audio/mpeg", "audio/mp3", "application/octet-stream"},
    ".m4a": {"audio/mp4", "audio/x-m4a", "application/octet-stream"},
    ".ogg": {"audio/ogg", "application/ogg", "application/octet-stream"},
    ".webm": {"audio/webm", "video/webm", "application/octet-stream"},
    ".mp4": {"video/mp4", "audio/mp4", "application/octet-stream"},
}
_CAPABILITY_HEADER = "x-recantor-upload-token"
_SESSION_METADATA_KEY = "recantor_session_id"


@dataclass(frozen=True)
class _CompletionBinding:
    session_id: UUID
    upload_id: str
    expected_bytes: int


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not normalized or normalized in {".", ".."}:
        raise UploadPolicyError("file name is not valid")
    return normalized


def _normalize_content_type(content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    return normalized or "application/octet-stream"


def validate_upload_policy(
    *,
    original_filename: str,
    content_type: str,
    byte_length: int,
    duration_ms: int | None,
) -> tuple[str, str]:
    settings = get_settings()
    filename = _normalize_filename(original_filename)
    normalized_type = _normalize_content_type(content_type)
    allowed_types = _ALLOWED_CONTENT_TYPES.get(PurePath(filename).suffix.lower())
    if allowed_types is None:
        raise UploadPolicyError("unsupported file extension; use WAV, MP3, M4A, OGG, WebM, or MP4")
    if normalized_type not in allowed_types:
        raise UploadPolicyError("declared media type does not match the supported file type")
    if byte_length <= 0:
        raise UploadPolicyError("upload must contain at least one byte")
    if byte_length > settings.upload_max_bytes:
        raise UploadPolicyError("upload exceeds the configured size limit")
    if duration_ms is not None:
        if duration_ms <= 0:
            raise UploadPolicyError("declared duration must be positive")
        if duration_ms > settings.upload_max_duration_seconds * 1000:
            raise UploadPolicyError("upload exceeds the configured duration limit")
    return filename, normalized_type


@lru_cache
def get_upload_storage() -> FilesystemUploadStorage:
    settings = get_settings()
    return FilesystemUploadStorage(settings.audio_storage_path, settings.upload_tus_storage_prefix)


def _validate_capability(
    session: RecordingSession,
    record: UploadRecord,
    token: str,
    *,
    now: datetime | None = None,
) -> None:
    expected = session.recovery_token_hash
    if expected is None or not hmac.compare_digest(expected, _token_hash(token)):
        raise UploadForbidden("upload capability is invalid")
    if (now or utcnow()) >= _as_aware(record.expires_at):
        raise UploadExpired("upload capability has expired")


async def _load_upload(
    db: AsyncSession,
    session_id: UUID,
    *,
    for_update: bool = False,
) -> tuple[RecordingSession, UploadRecord]:
    statement = select(UploadRecord).where(UploadRecord.session_id == session_id)
    if for_update:
        statement = statement.with_for_update()
    record = (await db.execute(statement)).scalar_one_or_none()
    session = await db.get(RecordingSession, session_id)
    if record is None or session is None or session.kind != SessionKind.UPLOAD.value:
        raise UploadNotFound("upload session not found")
    return session, record


def _metadata_matches(
    record: UploadRecord,
    *,
    original_filename: str,
    content_type: str,
    byte_length: int,
    duration_ms: int | None,
) -> bool:
    return (
        record.original_filename == original_filename
        and record.content_type == content_type
        and record.declared_byte_length == byte_length
        and record.declared_duration_ms == duration_ms
    )


async def _existing_upload(
    db: AsyncSession,
    *,
    client_request_id: UUID,
    token_hash: str,
    original_filename: str,
    content_type: str,
    byte_length: int,
    duration_ms: int | None,
) -> tuple[RecordingSession, UploadRecord] | None:
    session = await db.scalar(
        select(RecordingSession).where(RecordingSession.client_request_id == client_request_id)
    )
    if session is None:
        return None
    if session.kind != SessionKind.UPLOAD.value:
        raise UploadConflict("idempotency key belongs to a different session kind")
    record = await db.get(UploadRecord, session.id)
    if record is None:
        raise UploadConflict("upload session is missing its durable upload record")
    if session.recovery_token_hash is None or not hmac.compare_digest(
        session.recovery_token_hash, token_hash
    ):
        raise UploadConflict("idempotency key capability does not match")
    if not _metadata_matches(
        record,
        original_filename=original_filename,
        content_type=content_type,
        byte_length=byte_length,
        duration_ms=duration_ms,
    ):
        raise UploadConflict("idempotency key upload metadata does not match")
    return session, record


async def create_upload_session(
    db: AsyncSession,
    *,
    client_request_id: UUID,
    capability_token: str,
    original_filename: str,
    content_type: str,
    byte_length: int,
    duration_ms: int | None,
) -> tuple[RecordingSession, UploadRecord]:
    filename, normalized_type = validate_upload_policy(
        original_filename=original_filename,
        content_type=content_type,
        byte_length=byte_length,
        duration_ms=duration_ms,
    )
    token_hash = _token_hash(capability_token)
    existing = await _existing_upload(
        db,
        client_request_id=client_request_id,
        token_hash=token_hash,
        original_filename=filename,
        content_type=normalized_type,
        byte_length=byte_length,
        duration_ms=duration_ms,
    )
    if existing is not None:
        return existing

    now = utcnow()
    session_id = uuid4()
    session = RecordingSession(
        id=session_id,
        client_request_id=client_request_id,
        kind=SessionKind.UPLOAD.value,
        state=SessionState.UPLOADING.value,
        active_writer_id=None,
        capture_epoch=0,
        recovery_token_hash=token_hash,
        created_at=now,
        updated_at=now,
    )
    record = UploadRecord(
        session_id=session_id,
        original_filename=filename,
        content_type=normalized_type,
        declared_byte_length=byte_length,
        declared_duration_ms=duration_ms,
        received_bytes=0,
        expires_at=now + timedelta(seconds=get_settings().upload_capability_ttl_seconds),
        created_at=now,
        updated_at=now,
    )
    db.add_all([session, record])
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        existing = await _existing_upload(
            db,
            client_request_id=client_request_id,
            token_hash=token_hash,
            original_filename=filename,
            content_type=normalized_type,
            byte_length=byte_length,
            duration_ms=duration_ms,
        )
        if existing is None:
            raise UploadConflict("upload session creation conflicted") from exc
        return existing
    await db.refresh(session)
    await db.refresh(record)
    return session, record


async def get_upload_session(
    db: AsyncSession, *, session_id: UUID, capability_token: str
) -> tuple[RecordingSession, UploadRecord]:
    session, record = await _load_upload(db, session_id)
    _validate_capability(session, record, capability_token)
    return session, record


def upload_session_response(
    session: RecordingSession, record: UploadRecord
) -> UploadSessionResponse:
    return UploadSessionResponse(
        session_id=session.id,
        client_request_id=session.client_request_id,
        kind=SessionKind(session.kind),
        state=SessionState(session.state),
        original_filename=record.original_filename,
        content_type=record.content_type,
        declared_byte_length=record.declared_byte_length,
        declared_duration_ms=record.declared_duration_ms,
        received_bytes=record.received_bytes,
        expires_at=record.expires_at,
        completed_at=record.completed_at,
        failure_code=record.failure_code,
        failure_message=record.failure_message,
        upload_endpoint=get_settings().tus_public_endpoint,
    )


def _hook_header(request: TusHookRequest, name: str) -> str | None:
    for header_name, values in request.event.http_request.header.items():
        if header_name.lower() == name.lower() and values:
            return values[0]
    return None


def _hook_session_id(request: TusHookRequest) -> UUID:
    raw = request.event.upload.metadata.get(_SESSION_METADATA_KEY)
    if not raw:
        raise UploadForbidden("upload metadata is missing the Recantor session")
    try:
        return UUID(raw)
    except ValueError as exc:
        raise UploadForbidden("upload metadata contains an invalid Recantor session") from exc


def _validate_hook_identity(record: UploadRecord, request: TusHookRequest) -> None:
    upload = request.event.upload
    if upload.size_is_deferred:
        raise UploadPolicyError("deferred upload length is not supported")
    if upload.size != record.declared_byte_length:
        raise UploadConflict("tus upload length does not match the declared file")
    filename = upload.metadata.get("filename")
    filetype = upload.metadata.get("filetype") or "application/octet-stream"
    if (
        filename != record.original_filename
        or _normalize_content_type(filetype) != record.content_type
    ):
        raise UploadConflict("tus upload metadata does not match the declared file")
    if upload.offset > record.declared_byte_length:
        raise UploadConflict("tus upload offset exceeds the declared file length")


def _hook_error_response(request: TusHookRequest, exc: UploadError) -> TusHookResponse:
    if isinstance(exc, UploadExpired):
        status_code = 410
    elif isinstance(exc, UploadForbidden):
        status_code = 403
    elif isinstance(exc, UploadPolicyError):
        status_code = 422
    elif isinstance(exc, UploadNotFound):
        status_code = 404
    else:
        status_code = 409
    return TusHookResponse(
        http_response=TusHookHttpResponse(
            status_code=status_code,
            body=json.dumps({"detail": str(exc)}, separators=(",", ":")),
            header={"Content-Type": "application/json"},
        ),
        reject_upload=request.type == "pre-create",
        stop_upload=request.type == "post-receive",
    )


def _bind_tus_upload(record: UploadRecord, upload_id: str | None) -> None:
    if upload_id is None:
        raise UploadConflict("tus upload id is missing after creation")
    if record.tus_upload_id is None:
        record.tus_upload_id = upload_id
    elif record.tus_upload_id != upload_id:
        raise UploadConflict("upload session is already bound to a different tus upload")


async def _process_nonfinish_hook(
    db: AsyncSession,
    request: TusHookRequest,
    *,
    session_id: UUID,
    capability_token: str,
) -> None:
    session, record = await _load_upload(db, session_id, for_update=True)
    _validate_capability(session, record, capability_token)
    _validate_hook_identity(record, request)
    upload = request.event.upload
    if request.type == "pre-create":
        if record.tus_upload_id is not None:
            raise UploadConflict("upload session already has a tus upload")
        await db.commit()
        return
    _bind_tus_upload(record, upload.id)
    record.received_bytes = max(
        record.received_bytes, min(upload.offset, record.declared_byte_length)
    )
    record.updated_at = utcnow()
    await db.commit()


async def _prepare_completion(
    db: AsyncSession,
    request: TusHookRequest,
    *,
    session_id: UUID,
    capability_token: str,
) -> _CompletionBinding:
    # This transaction only validates/binds the durable upload row. It is committed BEFORE
    # whole-file hashing, so no upload-row lock or database transaction spans O(file-size) I/O.
    session, record = await _load_upload(db, session_id, for_update=True)
    _validate_capability(session, record, capability_token)
    _validate_hook_identity(record, request)
    upload = request.event.upload
    _bind_tus_upload(record, upload.id)
    if upload.offset != record.declared_byte_length:
        raise UploadConflict("tus completion arrived before all declared bytes")
    record.received_bytes = max(record.received_bytes, record.declared_byte_length)
    record.updated_at = utcnow()
    binding = _CompletionBinding(session_id, record.tus_upload_id, record.declared_byte_length)
    await db.commit()
    return binding


def _processing_identity_matches(processing: UploadMediaProcessing, record: UploadRecord) -> bool:
    return (
        processing.source_storage_key == record.storage_key
        and processing.source_sha256 == record.sha256
        and processing.source_byte_length == record.byte_length
        and _as_aware(processing.source_completed_at) == _as_aware(record.completed_at)
        and processing.normalization_spec_id == NORMALIZATION_SPEC_ID
        and processing.segmentation_spec_id == SEGMENTATION_SPEC_ID
        and processing.segmentation_params_json == SEGMENTATION_PARAMS_JSON
    )


async def _publish_completion(
    db: AsyncSession,
    request: TusHookRequest,
    *,
    binding: _CompletionBinding,
    capability_token: str,
    stored: StoredUpload,
) -> None:
    session, record = await _load_upload(db, binding.session_id, for_update=True)
    _validate_capability(session, record, capability_token)
    _validate_hook_identity(record, request)
    upload = request.event.upload
    _bind_tus_upload(record, upload.id)
    if (
        record.tus_upload_id != binding.upload_id
        or record.declared_byte_length != binding.expected_bytes
    ):
        raise UploadConflict("upload binding changed while completion was being hashed")
    if upload.offset != binding.expected_bytes:
        raise UploadConflict("tus completion identity changed before publish")

    # Revalidate the path/inode/size/mtime evidence after reacquiring the row lock. The bytes
    # were hashed outside the transaction, but publication is conditional on that same object.
    get_upload_storage().revalidate_completed(
        binding.upload_id,
        stored,
        expected_bytes=binding.expected_bytes,
    )
    if record.completed_at is not None:
        if (
            record.storage_key != stored.key
            or record.byte_length != stored.byte_length
            or record.sha256 != stored.sha256
        ):
            raise UploadConflict("duplicate completion does not match durable upload evidence")
    else:
        completed_at = utcnow()
        record.storage_key = stored.key
        record.sha256 = stored.sha256
        record.byte_length = stored.byte_length
        record.received_bytes = stored.byte_length
        record.completed_at = completed_at
        record.failure_code = None
        record.failure_message = None
        session.state = SessionState.UPLOADED.value
        session.updated_at = completed_at

    if (
        record.completed_at is None
        or record.storage_key is None
        or record.sha256 is None
        or record.byte_length is None
    ):
        raise UploadConflict("completed upload identity is incomplete")
    processing = await db.get(UploadMediaProcessing, record.session_id)
    if processing is None:
        db.add(
            UploadMediaProcessing(
                session_id=record.session_id,
                source_storage_key=record.storage_key,
                source_sha256=record.sha256,
                source_byte_length=record.byte_length,
                source_completed_at=record.completed_at,
                state=UploadProcessingState.PENDING.value,
                normalization_spec_id=NORMALIZATION_SPEC_ID,
                segmentation_spec_id=SEGMENTATION_SPEC_ID,
                segmentation_params_json=SEGMENTATION_PARAMS_JSON,
            )
        )
    elif not _processing_identity_matches(processing, record):
        raise UploadConflict(
            "upload processing identity drifted from immutable completion evidence"
        )
    await db.commit()


async def _process_completion_hook(
    db: AsyncSession,
    request: TusHookRequest,
    *,
    session_id: UUID,
    capability_token: str,
) -> None:
    binding = await _prepare_completion(
        db,
        request,
        session_id=session_id,
        capability_token=capability_token,
    )
    # Hashing is explicitly off the asyncio event loop and outside any database transaction.
    stored = await asyncio.to_thread(
        get_upload_storage().inspect_completed,
        binding.upload_id,
        expected_bytes=binding.expected_bytes,
    )
    await _publish_completion(
        db,
        request,
        binding=binding,
        capability_token=capability_token,
        stored=stored,
    )


async def process_tusd_hook(db: AsyncSession, request: TusHookRequest) -> TusHookResponse:
    if request.type not in {
        "pre-create",
        "post-create",
        "post-receive",
        "pre-finish",
        "post-finish",
    }:
        return TusHookResponse()
    try:
        session_id = _hook_session_id(request)
        token = _hook_header(request, _CAPABILITY_HEADER)
        if not token:
            raise UploadForbidden("upload capability header is missing")
        if request.type in {"pre-finish", "post-finish"}:
            await _process_completion_hook(
                db,
                request,
                session_id=session_id,
                capability_token=token,
            )
        else:
            await _process_nonfinish_hook(
                db,
                request,
                session_id=session_id,
                capability_token=token,
            )
    except UploadStorageError:
        await db.rollback()
        raise
    except UploadError as exc:
        await db.rollback()
        return _hook_error_response(request, exc)
    return TusHookResponse()
