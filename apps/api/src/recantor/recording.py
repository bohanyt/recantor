from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.models import (
    RecordingChunk,
    RecordingGap,
    RecordingSession,
    SessionKind,
    SessionState,
)
from recantor.schemas import SequenceRange
from recantor.settings import get_settings
from recantor.storage import FilesystemAudioStorage


class RecordingError(RuntimeError):
    pass


class RecordingNotFound(RecordingError):
    pass


class RecordingConflict(RecordingError):
    pass


class StaleWriter(RecordingConflict):
    pass


@lru_cache
def get_audio_storage() -> FilesystemAudioStorage:
    return FilesystemAudioStorage(get_settings().audio_storage_path)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _locked_session(db: AsyncSession, session_id: UUID) -> RecordingSession:
    result = await db.execute(
        select(RecordingSession).where(RecordingSession.id == session_id).with_for_update()
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise RecordingNotFound("recording session not found")
    return session


def _validate_writer(session: RecordingSession, writer_id: str, capture_epoch: int) -> None:
    if session.active_writer_id != writer_id or session.capture_epoch != capture_epoch:
        raise StaleWriter("capture writer or epoch is no longer active")


def _validate_completed_finalizer(
    session: RecordingSession, writer_id: str, capture_epoch: int
) -> None:
    if session.finalized_writer_id != writer_id or session.finalized_capture_epoch != capture_epoch:
        raise StaleWriter("finalize retry does not match the completed capture owner")


def _can_accept_chunks(session: RecordingSession) -> bool:
    return session.state in {
        SessionState.RECORDING.value,
        SessionState.RECOVERING.value,
        SessionState.INTERRUPTED.value,
        SessionState.FINALIZING.value,
    }


def _mark_interrupted_if_stale(session: RecordingSession) -> bool:
    if session.state != SessionState.RECORDING.value or session.last_heartbeat_at is None:
        return False
    timeout = timedelta(seconds=get_settings().recording_heartbeat_timeout_seconds)
    last_heartbeat = _as_aware(session.last_heartbeat_at)
    if last_heartbeat is None or utcnow() - last_heartbeat <= timeout:
        return False
    session.state = SessionState.INTERRUPTED.value
    session.interrupted_at = last_heartbeat
    session.updated_at = utcnow()
    return True


async def create_live_session(
    db: AsyncSession, *, client_request_id: UUID, writer_id: str
) -> RecordingSession:
    existing = await db.scalar(
        select(RecordingSession).where(RecordingSession.client_request_id == client_request_id)
    )
    if existing is not None:
        if existing.active_writer_id != writer_id:
            raise RecordingConflict("idempotency key already belongs to a different writer")
        return existing

    now = utcnow()
    session = RecordingSession(
        client_request_id=client_request_id,
        kind=SessionKind.LIVE.value,
        state=SessionState.RECORDING.value,
        active_writer_id=writer_id,
        capture_epoch=1,
        last_heartbeat_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(
            select(RecordingSession).where(RecordingSession.client_request_id == client_request_id)
        )
        if existing is None:
            raise
        if existing.active_writer_id != writer_id:
            raise RecordingConflict(
                "idempotency key already belongs to a different writer"
            ) from None
        return existing
    await db.refresh(session)
    return session


async def get_recording_session(db: AsyncSession, session_id: UUID) -> RecordingSession:
    async with db.begin():
        session = await _locked_session(db, session_id)
        _mark_interrupted_if_stale(session)
    return session


async def claim_capture(
    db: AsyncSession,
    *,
    session_id: UUID,
    writer_id: str,
    expected_epoch: int | None,
) -> RecordingSession:
    async with db.begin():
        session = await _locked_session(db, session_id)
        _mark_interrupted_if_stale(session)
        if session.state in {
            SessionState.FINALIZING.value,
            SessionState.COMPLETE.value,
            SessionState.FAILED.value,
        }:
            raise RecordingConflict(f"session in state {session.state} cannot be claimed")
        if session.active_writer_id not in {None, writer_id}:
            raise StaleWriter("another capture writer owns this session")
        if expected_epoch is not None and session.capture_epoch != expected_epoch:
            raise StaleWriter("capture epoch changed")
        if session.active_writer_id is None:
            session.active_writer_id = writer_id
            session.capture_epoch += 1
        session.state = SessionState.RECORDING.value
        session.last_heartbeat_at = utcnow()
        session.updated_at = utcnow()
    return session


async def heartbeat(
    db: AsyncSession,
    *,
    session_id: UUID,
    writer_id: str,
    capture_epoch: int,
) -> RecordingSession:
    async with db.begin():
        session = await _locked_session(db, session_id)
        _validate_writer(session, writer_id, capture_epoch)
        if session.state in {SessionState.COMPLETE.value, SessionState.FAILED.value}:
            raise RecordingConflict(f"session in state {session.state} cannot heartbeat")
        session.last_heartbeat_at = utcnow()
        if session.state == SessionState.INTERRUPTED.value:
            session.state = SessionState.RECORDING.value
        session.updated_at = utcnow()
    return session


def compress_sequences(sequences: list[int]) -> list[SequenceRange]:
    if not sequences:
        return []
    ordered = sorted(set(sequences))
    ranges: list[SequenceRange] = []
    start = previous = ordered[0]
    for sequence in ordered[1:]:
        if sequence == previous + 1:
            previous = sequence
            continue
        ranges.append(SequenceRange(start=start, end=previous))
        start = previous = sequence
    ranges.append(SequenceRange(start=start, end=previous))
    return ranges


def highest_contiguous(sequences: list[int]) -> int:
    expected = 1
    for sequence in sorted(set(sequences)):
        if sequence < expected:
            continue
        if sequence != expected:
            break
        expected += 1
    return expected - 1


async def recording_state(
    db: AsyncSession, session_id: UUID
) -> tuple[RecordingSession, list[int], list[RecordingGap]]:
    session = await get_recording_session(db, session_id)
    chunks = list(
        (
            await db.scalars(
                select(RecordingChunk.sequence)
                .where(RecordingChunk.session_id == session_id)
                .order_by(RecordingChunk.sequence)
            )
        ).all()
    )
    gaps = list(
        (
            await db.scalars(
                select(RecordingGap)
                .where(RecordingGap.session_id == session_id)
                .order_by(RecordingGap.created_at, RecordingGap.id)
            )
        ).all()
    )
    return session, chunks, gaps


def _chunk_retry_matches(
    chunk: RecordingChunk,
    *,
    writer_id: str,
    capture_epoch: int,
    monotonic_start_ms: int,
    monotonic_end_ms: int,
    content_type: str,
    sha256: str,
    byte_length: int,
) -> bool:
    return (
        chunk.writer_id == writer_id
        and chunk.capture_epoch == capture_epoch
        and chunk.monotonic_start_ms == monotonic_start_ms
        and chunk.monotonic_end_ms == monotonic_end_ms
        and chunk.content_type == content_type
        and chunk.sha256 == sha256
        and chunk.byte_length == byte_length
    )


async def accept_chunk(
    db: AsyncSession,
    *,
    session_id: UUID,
    sequence: int,
    writer_id: str,
    capture_epoch: int,
    monotonic_start_ms: int,
    monotonic_end_ms: int,
    content_type: str,
    payload: bytes,
    declared_sha256: str,
) -> tuple[RecordingChunk, bool]:
    if monotonic_end_ms < monotonic_start_ms:
        raise RecordingConflict("monotonic_end_ms precedes monotonic_start_ms")
    if not payload:
        raise RecordingConflict("empty recording chunks are not accepted")
    if len(payload) > get_settings().recording_max_chunk_bytes:
        raise RecordingConflict("recording chunk exceeds configured size limit")

    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != declared_sha256.lower():
        raise RecordingConflict("recording chunk sha256 does not match payload")

    storage = get_audio_storage()
    async with db.begin():
        session = await _locked_session(db, session_id)
        existing = await db.scalar(
            select(RecordingChunk).where(
                RecordingChunk.session_id == session_id,
                RecordingChunk.sequence == sequence,
            )
        )
        if existing is not None:
            if not _chunk_retry_matches(
                existing,
                writer_id=writer_id,
                capture_epoch=capture_epoch,
                monotonic_start_ms=monotonic_start_ms,
                monotonic_end_ms=monotonic_end_ms,
                content_type=content_type,
                sha256=actual_sha256,
                byte_length=len(payload),
            ):
                raise RecordingConflict("sequence retry metadata conflicts with accepted chunk")
            valid = await asyncio.to_thread(
                storage.verify,
                existing.storage_key,
                sha256=existing.sha256,
                byte_length=existing.byte_length,
            )
            if not valid:
                raise RecordingConflict(
                    "accepted chunk metadata exists but durable audio is missing"
                )
            return existing, True

        _validate_writer(session, writer_id, capture_epoch)
        if not _can_accept_chunks(session):
            raise RecordingConflict(f"session in state {session.state} cannot accept chunks")
        if (
            session.state == SessionState.FINALIZING.value
            and session.final_sequence is not None
            and sequence > session.final_sequence
        ):
            raise RecordingConflict("chunk sequence is beyond the declared final boundary")

        stored = await asyncio.to_thread(
            storage.commit_bytes,
            session_id=session_id,
            sequence=sequence,
            content_type=content_type,
            payload=payload,
            sha256=actual_sha256,
        )
        chunk = RecordingChunk(
            session_id=session_id,
            sequence=sequence,
            writer_id=writer_id,
            capture_epoch=capture_epoch,
            monotonic_start_ms=monotonic_start_ms,
            monotonic_end_ms=monotonic_end_ms,
            content_type=content_type,
            sha256=actual_sha256,
            byte_length=len(payload),
            storage_key=stored.key,
        )
        db.add(chunk)
        session.updated_at = utcnow()
        await db.flush()
    return chunk, False


async def declare_gap(
    db: AsyncSession,
    *,
    session_id: UUID,
    client_gap_id: UUID,
    writer_id: str,
    capture_epoch: int,
    sequence_start: int | None,
    sequence_end: int | None,
    wall_started_at: datetime | None,
    wall_ended_at: datetime | None,
    reason: str,
) -> RecordingGap:
    async with db.begin():
        session = await _locked_session(db, session_id)
        _validate_writer(session, writer_id, capture_epoch)
        existing = await db.scalar(
            select(RecordingGap).where(
                RecordingGap.session_id == session_id,
                RecordingGap.client_gap_id == client_gap_id,
            )
        )
        if existing is not None:
            matches = (
                existing.writer_id == writer_id
                and existing.capture_epoch == capture_epoch
                and existing.sequence_start == sequence_start
                and existing.sequence_end == sequence_end
                and _as_aware(existing.wall_started_at) == _as_aware(wall_started_at)
                and _as_aware(existing.wall_ended_at) == _as_aware(wall_ended_at)
                and existing.reason == reason
            )
            if not matches:
                raise RecordingConflict("gap retry conflicts with the accepted declaration")
            return existing
        gap = RecordingGap(
            session_id=session_id,
            client_gap_id=client_gap_id,
            writer_id=writer_id,
            capture_epoch=capture_epoch,
            sequence_start=sequence_start,
            sequence_end=sequence_end,
            wall_started_at=wall_started_at,
            wall_ended_at=wall_ended_at,
            reason=reason,
        )
        db.add(gap)
        await db.flush()
    return gap


def _sequences_covered_by_gaps(gaps: list[RecordingGap], final_sequence: int) -> set[int]:
    covered: set[int] = set()
    for gap in gaps:
        if gap.sequence_start is None or gap.sequence_end is None:
            continue
        start = max(1, gap.sequence_start)
        end = min(final_sequence, gap.sequence_end)
        if end >= start:
            covered.update(range(start, end + 1))
    return covered


def _group_sequences(sequences: list[int]) -> list[tuple[int, int]]:
    return [(item.start, item.end) for item in compress_sequences(sequences)]


async def finalize_session(
    db: AsyncSession,
    *,
    session_id: UUID,
    writer_id: str,
    capture_epoch: int,
    final_sequence: int,
    final_monotonic_end_ms: int,
    gap_sequences: list[int],
) -> tuple[RecordingSession, list[int], list[RecordingGap]]:
    requested_gaps = sorted({item for item in gap_sequences if 1 <= item <= final_sequence})

    async with db.begin():
        session = await _locked_session(db, session_id)
        if session.final_sequence is not None and session.final_sequence != final_sequence:
            raise RecordingConflict(
                "final sequence boundary conflicts with an earlier finalize request"
            )
        if (
            session.final_monotonic_end_ms is not None
            and session.final_monotonic_end_ms != final_monotonic_end_ms
        ):
            raise RecordingConflict(
                "final monotonic boundary conflicts with an earlier finalize request"
            )

        if session.state == SessionState.COMPLETE.value:
            _validate_completed_finalizer(session, writer_id, capture_epoch)
            missing_after: list[int] = []
        else:
            _validate_writer(session, writer_id, capture_epoch)
            chunks = list(
                (
                    await db.scalars(
                        select(RecordingChunk).where(RecordingChunk.session_id == session_id)
                    )
                ).all()
            )
            if any(chunk.sequence > final_sequence for chunk in chunks):
                raise RecordingConflict("final sequence excludes an already accepted chunk")
            max_chunk_end_ms = max((chunk.monotonic_end_ms for chunk in chunks), default=0)
            if final_monotonic_end_ms < max_chunk_end_ms:
                raise RecordingConflict("final monotonic boundary precedes accepted audio")

            session.final_sequence = final_sequence
            session.final_monotonic_end_ms = final_monotonic_end_ms
            session.state = SessionState.FINALIZING.value
            session.updated_at = utcnow()

            accepted = {chunk.sequence for chunk in chunks}
            gaps = list(
                (
                    await db.scalars(
                        select(RecordingGap).where(RecordingGap.session_id == session_id)
                    )
                ).all()
            )
            covered = _sequences_covered_by_gaps(gaps, final_sequence)
            missing_before = [
                sequence
                for sequence in range(1, final_sequence + 1)
                if sequence not in accepted and sequence not in covered
            ]
            requested_gap_set = set(requested_gaps)
            declarable = [sequence for sequence in missing_before if sequence in requested_gap_set]
            for start, end in _group_sequences(declarable):
                gap = RecordingGap(
                    session_id=session_id,
                    client_gap_id=uuid4(),
                    writer_id=writer_id,
                    capture_epoch=capture_epoch,
                    sequence_start=start,
                    sequence_end=end,
                    reason="client_declared_missing_at_finalize",
                )
                db.add(gap)
                covered.update(range(start, end + 1))

            missing_after = [
                sequence
                for sequence in range(1, final_sequence + 1)
                if sequence not in accepted and sequence not in covered
            ]
            if not missing_after:
                session.state = SessionState.COMPLETE.value
                session.finalized_writer_id = writer_id
                session.finalized_capture_epoch = capture_epoch
                session.active_writer_id = None
                session.finalized_at = utcnow()
                session.updated_at = utcnow()
            await db.flush()

    gaps = list(
        (
            await db.scalars(
                select(RecordingGap)
                .where(RecordingGap.session_id == session_id)
                .order_by(RecordingGap.created_at, RecordingGap.id)
            )
        ).all()
    )
    return session, missing_after, gaps
