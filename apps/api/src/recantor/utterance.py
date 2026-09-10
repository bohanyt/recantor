from __future__ import annotations

import hashlib
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.models import RecordingSession, TranscriptionUtterance
from recantor.settings import get_settings
from recantor.storage import AudioStorageConflict, AudioStorageError, FilesystemAudioStorage


class UtteranceWorkError(RuntimeError):
    pass


class UtteranceWorkNotFound(UtteranceWorkError):
    pass


class UtteranceWorkConflict(UtteranceWorkError):
    pass


class UtteranceWorkStorageError(UtteranceWorkError):
    pass


def utterance_work_id(session_id: UUID, producer_key: str) -> UUID:
    canonical_key = producer_key.strip()
    return uuid5(session_id, f"recantor-utterance:{canonical_key}")


def transcript_producer_key_for_utterance(work_id: UUID) -> str:
    return f"utterance:{work_id}"


def _canonical_payload(
    *,
    producer_key: str,
    start_ms: int,
    end_ms: int,
    content_type: str,
    payload: bytes,
) -> tuple[str, int, int, str, bytes, str]:
    canonical_key = producer_key.strip()
    canonical_content_type = content_type.strip().lower()

    if not canonical_key:
        raise UtteranceWorkConflict("utterance producer key must not be blank")
    if len(canonical_key) > 160:
        raise UtteranceWorkConflict("utterance producer key exceeds 160 characters")
    if start_ms < 0 or end_ms <= start_ms:
        raise UtteranceWorkConflict("utterance timing must have 0 <= start_ms < end_ms")
    if not canonical_content_type:
        raise UtteranceWorkConflict("utterance content type must not be blank")
    if len(canonical_content_type) > 128:
        raise UtteranceWorkConflict("utterance content type exceeds 128 characters")
    if not payload:
        raise UtteranceWorkConflict("utterance audio payload must not be empty")

    digest = hashlib.sha256(payload).hexdigest()
    return canonical_key, start_ms, end_ms, canonical_content_type, payload, digest


def _retry_matches(
    work: TranscriptionUtterance,
    *,
    work_id: UUID,
    start_ms: int,
    end_ms: int,
    content_type: str,
    sha256: str,
    byte_length: int,
) -> bool:
    return (
        work.id == work_id
        and work.start_ms == start_ms
        and work.end_ms == end_ms
        and work.content_type == content_type
        and work.sha256 == sha256
        and work.byte_length == byte_length
    )


def _audio_storage() -> FilesystemAudioStorage:
    return FilesystemAudioStorage(get_settings().audio_storage_path)


async def commit_utterance_work(
    db: AsyncSession,
    *,
    session_id: UUID,
    producer_key: str,
    start_ms: int,
    end_ms: int,
    content_type: str,
    payload: bytes,
) -> tuple[TranscriptionUtterance, bool]:
    key, start, end, canonical_content_type, canonical_payload, digest = _canonical_payload(
        producer_key=producer_key,
        start_ms=start_ms,
        end_ms=end_ms,
        content_type=content_type,
        payload=payload,
    )
    work_id = utterance_work_id(session_id, key)
    storage = _audio_storage()

    async with db.begin():
        session = await db.scalar(
            select(RecordingSession).where(RecordingSession.id == session_id).with_for_update()
        )
        if session is None:
            raise UtteranceWorkNotFound("recording session not found")

        existing = await db.scalar(
            select(TranscriptionUtterance).where(
                TranscriptionUtterance.session_id == session_id,
                TranscriptionUtterance.producer_key == key,
            )
        )
        if existing is not None:
            if not _retry_matches(
                existing,
                work_id=work_id,
                start_ms=start,
                end_ms=end,
                content_type=canonical_content_type,
                sha256=digest,
                byte_length=len(canonical_payload),
            ):
                raise UtteranceWorkConflict(
                    "utterance producer-key retry conflicts with the committed work item"
                )
            if not storage.verify_utterance(
                session_id=session_id,
                work_id=existing.id,
                producer_key=existing.producer_key,
                start_ms=existing.start_ms,
                end_ms=existing.end_ms,
                content_type=existing.content_type,
                storage_key=existing.storage_key,
                sha256=existing.sha256,
                byte_length=existing.byte_length,
            ):
                raise UtteranceWorkStorageError(
                    "committed utterance storage evidence failed verification"
                )
            return existing, True

        highest_sequence = await db.scalar(
            select(func.max(TranscriptionUtterance.sequence)).where(
                TranscriptionUtterance.session_id == session_id
            )
        )

        try:
            stored = storage.commit_utterance_bytes(
                session_id=session_id,
                work_id=work_id,
                producer_key=key,
                start_ms=start,
                end_ms=end,
                content_type=canonical_content_type,
                payload=canonical_payload,
                sha256=digest,
            )
        except AudioStorageConflict as exc:
            raise UtteranceWorkConflict(
                "utterance producer-key retry conflicts with existing durable storage evidence"
            ) from exc
        except AudioStorageError as exc:
            raise UtteranceWorkStorageError("utterance audio storage failed") from exc

        work = TranscriptionUtterance(
            id=work_id,
            session_id=session_id,
            sequence=(highest_sequence or 0) + 1,
            producer_key=key,
            start_ms=start,
            end_ms=end,
            content_type=canonical_content_type,
            sha256=stored.sha256,
            byte_length=stored.byte_length,
            storage_key=stored.key,
        )
        db.add(work)
        await db.flush()

    return work, False


async def read_utterance_work(
    db: AsyncSession,
    *,
    session_id: UUID,
    after_sequence: int = 0,
    limit: int = 200,
) -> tuple[list[TranscriptionUtterance], bool]:
    if after_sequence < 0:
        raise UtteranceWorkConflict("after_sequence must be non-negative")
    if limit < 1 or limit > 1000:
        raise UtteranceWorkConflict("utterance page limit must be between 1 and 1000")

    session_exists = await db.scalar(
        select(RecordingSession.id).where(RecordingSession.id == session_id)
    )
    if session_exists is None:
        raise UtteranceWorkNotFound("recording session not found")

    result = list(
        (
            await db.scalars(
                select(TranscriptionUtterance)
                .where(
                    TranscriptionUtterance.session_id == session_id,
                    TranscriptionUtterance.sequence > after_sequence,
                )
                .order_by(TranscriptionUtterance.sequence)
                .limit(limit + 1)
            )
        ).all()
    )
    return result[:limit], len(result) > limit
