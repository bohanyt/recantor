from __future__ import annotations

import asyncio
import hashlib
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.models import RecordingSession, SessionKind, SessionState, UploadRecord
from recantor.settings import get_settings
from recantor.upload_contracts import TusHookRequest
from recantor.uploads import (
    UploadConflict,
    UploadPolicyError,
    create_upload_session,
    get_upload_storage,
    process_tusd_hook,
    upload_session_response,
    validate_upload_policy,
)

TOKEN_A = "A" * 43
TOKEN_B = "B" * 43


@pytest_asyncio.fixture
async def clean_upload_state(clean_recording_state):
    get_upload_storage.cache_clear()
    yield
    get_upload_storage.cache_clear()


def hook(
    hook_type: str,
    *,
    session_id,
    token: str,
    size: int,
    offset: int,
    upload_id: str | None,
    filename: str = "meeting.wav",
    content_type: str = "audio/wav",
) -> TusHookRequest:
    return TusHookRequest.model_validate(
        {
            "Type": hook_type,
            "Event": {
                "Upload": {
                    "ID": upload_id,
                    "Size": size,
                    "SizeIsDeferred": False,
                    "Offset": offset,
                    "MetaData": {
                        "recantor_session_id": str(session_id),
                        "filename": filename,
                        "filetype": content_type,
                    },
                    "Storage": {},
                },
                "HTTPRequest": {
                    "Method": "POST" if hook_type == "pre-create" else "PATCH",
                    "URI": "/files/" if upload_id is None else f"/files/{upload_id}",
                    "Header": {"X-Recantor-Upload-Token": [token]},
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_create_upload_is_idempotent_and_stores_only_capability_hash(clean_upload_state):
    request_id = uuid4()
    async with get_sessionmaker()() as db:
        first_session, first_record = await create_upload_session(
            db,
            client_request_id=request_id,
            capability_token=TOKEN_A,
            original_filename="meeting.wav",
            content_type="audio/wav",
            byte_length=4096,
            duration_ms=1500,
        )
        second_session, second_record = await create_upload_session(
            db,
            client_request_id=request_id,
            capability_token=TOKEN_A,
            original_filename="meeting.wav",
            content_type="audio/wav",
            byte_length=4096,
            duration_ms=1500,
        )

        assert first_session.id == second_session.id
        assert first_record.session_id == second_record.session_id
        assert first_session.kind == SessionKind.UPLOAD.value
        assert first_session.state == SessionState.UPLOADING.value
        assert first_session.recovery_token_hash == hashlib.sha256(TOKEN_A.encode()).hexdigest()
        assert first_session.recovery_token_hash != TOKEN_A

        public = upload_session_response(first_session, first_record).model_dump()
        assert "capability_token" not in public
        assert "recovery_token_hash" not in public
        assert "storage_key" not in public
        assert "tus_upload_id" not in public

        with pytest.raises(UploadConflict, match="capability"):
            await create_upload_session(
                db,
                client_request_id=request_id,
                capability_token=TOKEN_B,
                original_filename="meeting.wav",
                content_type="audio/wav",
                byte_length=4096,
                duration_ms=1500,
            )


@pytest.mark.asyncio
async def test_upload_policy_rejects_bad_type_size_and_duration(clean_upload_state):
    settings = get_settings()
    old_bytes = settings.upload_max_bytes
    old_duration = settings.upload_max_duration_seconds
    settings.upload_max_bytes = 100
    settings.upload_max_duration_seconds = 2
    try:
        with pytest.raises(UploadPolicyError, match="extension"):
            validate_upload_policy(
                original_filename="notes.txt",
                content_type="text/plain",
                byte_length=1,
                duration_ms=None,
            )
        with pytest.raises(UploadPolicyError, match="media type"):
            validate_upload_policy(
                original_filename="meeting.wav",
                content_type="video/mp4",
                byte_length=1,
                duration_ms=None,
            )
        with pytest.raises(UploadPolicyError, match="size limit"):
            validate_upload_policy(
                original_filename="meeting.wav",
                content_type="audio/wav",
                byte_length=101,
                duration_ms=None,
            )
        with pytest.raises(UploadPolicyError, match="duration limit"):
            validate_upload_policy(
                original_filename="meeting.wav",
                content_type="audio/wav",
                byte_length=100,
                duration_ms=2001,
            )
    finally:
        settings.upload_max_bytes = old_bytes
        settings.upload_max_duration_seconds = old_duration


@pytest.mark.asyncio
async def test_tusd_resume_progress_and_duplicate_completion_are_durable(clean_upload_state):
    payload = b"resume-safe-upload" * 256
    upload_id = "abcdeffedcba0123456789abcdef0123"
    async with get_sessionmaker()() as db:
        session, _ = await create_upload_session(
            db,
            client_request_id=uuid4(),
            capability_token=TOKEN_A,
            original_filename="meeting.wav",
            content_type="audio/wav",
            byte_length=len(payload),
            duration_ms=None,
        )
        session_id = session.id
        pre_create = await process_tusd_hook(
            db,
            hook(
                "pre-create",
                session_id=session_id,
                token=TOKEN_A,
                size=len(payload),
                offset=0,
                upload_id=None,
            ),
        )
        assert pre_create.reject_upload is False
        await process_tusd_hook(
            db,
            hook(
                "post-create",
                session_id=session_id,
                token=TOKEN_A,
                size=len(payload),
                offset=0,
                upload_id=upload_id,
            ),
        )
        halfway = len(payload) // 2
        await process_tusd_hook(
            db,
            hook(
                "post-receive",
                session_id=session_id,
                token=TOKEN_A,
                size=len(payload),
                offset=halfway,
                upload_id=upload_id,
            ),
        )
        record = await db.get(UploadRecord, session_id)
        assert record is not None
        assert record.received_bytes == halfway

    storage = get_upload_storage()
    storage.upload_root.mkdir(parents=True, exist_ok=True)
    (storage.upload_root / upload_id).write_bytes(payload)

    async with get_sessionmaker()() as db:
        finish = hook(
            "pre-finish",
            session_id=session_id,
            token=TOKEN_A,
            size=len(payload),
            offset=len(payload),
            upload_id=upload_id,
        )
        await process_tusd_hook(db, finish)
        await process_tusd_hook(
            db,
            hook(
                "post-finish",
                session_id=session_id,
                token=TOKEN_A,
                size=len(payload),
                offset=len(payload),
                upload_id=upload_id,
            ),
        )
        record = await db.get(UploadRecord, session_id)
        session = await db.get(RecordingSession, session_id)
        assert record is not None and session is not None
        first_completed_at = record.completed_at
        assert record.received_bytes == len(payload)
        assert record.byte_length == len(payload)
        assert record.storage_key == f"uploads/tus/{upload_id}"
        assert record.completed_at is not None
        assert session.state == SessionState.UPLOADED.value

        await process_tusd_hook(db, finish)
        record = await db.get(UploadRecord, session_id)
        assert record is not None
        assert record.completed_at == first_completed_at


@pytest.mark.asyncio
async def test_upload_capabilities_isolate_parallel_sessions(clean_upload_state):
    async with get_sessionmaker()() as db:
        session_a, _ = await create_upload_session(
            db,
            client_request_id=uuid4(),
            capability_token=TOKEN_A,
            original_filename="a.wav",
            content_type="audio/wav",
            byte_length=1000,
            duration_ms=None,
        )
        session_b, _ = await create_upload_session(
            db,
            client_request_id=uuid4(),
            capability_token=TOKEN_B,
            original_filename="b.wav",
            content_type="audio/wav",
            byte_length=1000,
            duration_ms=None,
        )

    async def bind(session_id, token, upload_id, filename):
        async with get_sessionmaker()() as db:
            return await process_tusd_hook(
                db,
                hook(
                    "post-create",
                    session_id=session_id,
                    token=token,
                    size=1000,
                    offset=0,
                    upload_id=upload_id,
                    filename=filename,
                ),
            )

    response_a, response_b = await asyncio.gather(
        bind(session_a.id, TOKEN_A, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "a.wav"),
        bind(session_b.id, TOKEN_B, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "b.wav"),
    )
    assert response_a.http_response is None
    assert response_b.http_response is None

    wrong = await bind(session_b.id, TOKEN_A, "cccccccccccccccccccccccccccccccc", "b.wav")
    assert wrong.http_response is not None
    assert wrong.http_response.status_code == 403

    async with get_sessionmaker()() as db:
        record_a = await db.get(UploadRecord, session_a.id)
        record_b = await db.get(UploadRecord, session_b.id)
        assert record_a is not None and record_b is not None
        assert record_a.tus_upload_id == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert record_b.tus_upload_id == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.mark.asyncio
async def test_upload_api_requires_capability_and_hides_private_storage(clean_upload_state):
    body = {
        "client_request_id": str(uuid4()),
        "capability_token": TOKEN_A,
        "original_filename": "browser.webm",
        "content_type": "audio/webm",
        "byte_length": 2048,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/v1/uploads", json=body)
        assert created.status_code == 201, created.text
        payload = created.json()
        session_id = payload["session_id"]
        assert payload["state"] == "uploading"
        assert "capability_token" not in payload
        assert "storage_key" not in payload
        assert "tus_upload_id" not in payload

        missing = await client.get(f"/api/v1/uploads/{session_id}")
        assert missing.status_code == 422
        wrong = await client.get(
            f"/api/v1/uploads/{session_id}",
            headers={"X-Recantor-Upload-Token": TOKEN_B},
        )
        assert wrong.status_code == 403
        ok = await client.get(
            f"/api/v1/uploads/{session_id}",
            headers={"X-Recantor-Upload-Token": TOKEN_A},
        )
        assert ok.status_code == 200
        assert ok.json()["received_bytes"] == 0
