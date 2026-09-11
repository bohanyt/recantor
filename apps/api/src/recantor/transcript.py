from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.models import RecordingSession, TranscriptSegment
from recantor.transcript_realtime import enqueue_transcript_available

TranscriptCommitGuard = Callable[[AsyncSession], Awaitable[bool]]
logger = logging.getLogger(__name__)


class TranscriptError(RuntimeError):
    pass


class TranscriptNotFound(TranscriptError):
    pass


class TranscriptConflict(TranscriptError):
    pass


class TranscriptCommitRejected(TranscriptError):
    pass


def _canonical_payload(
    *, producer_key: str, start_ms: int, end_ms: int, text: str, language: str | None
) -> tuple[str, int, int, str, str | None]:
    canonical_key = producer_key.strip()
    canonical_text = text.strip()
    canonical_language = language.strip().lower() if language and language.strip() else None

    if not canonical_key:
        raise TranscriptConflict("transcript producer key must not be blank")
    if len(canonical_key) > 160:
        raise TranscriptConflict("transcript producer key exceeds 160 characters")
    if start_ms < 0 or end_ms <= start_ms:
        raise TranscriptConflict("transcript segment timing must have 0 <= start_ms < end_ms")
    if not canonical_text:
        raise TranscriptConflict("transcript text must not be blank")
    if canonical_language is not None and len(canonical_language) > 32:
        raise TranscriptConflict("transcript language exceeds 32 characters")

    return canonical_key, start_ms, end_ms, canonical_text, canonical_language


def _retry_matches(
    segment: TranscriptSegment,
    *,
    start_ms: int,
    end_ms: int,
    text: str,
    language: str | None,
) -> bool:
    return (
        segment.start_ms == start_ms
        and segment.end_ms == end_ms
        and segment.text == text
        and segment.language == language
    )


def _notify_realtime_best_effort(segment: TranscriptSegment) -> None:
    try:
        enqueue_transcript_available(
            session_id=segment.session_id,
            sequence=segment.sequence,
        )
    except Exception:  # Delivery bugs must never roll back committed transcript or capture state.
        logger.exception(
            "unexpected transcript realtime enqueue failure for session %s sequence %s",
            segment.session_id,
            segment.sequence,
        )


async def commit_transcript_segment(
    db: AsyncSession,
    *,
    session_id: UUID,
    producer_key: str,
    start_ms: int,
    end_ms: int,
    text: str,
    language: str | None = None,
    commit_guard: TranscriptCommitGuard | None = None,
) -> tuple[TranscriptSegment, bool]:
    key, start, end, canonical_text, canonical_language = _canonical_payload(
        producer_key=producer_key,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        language=language,
    )

    async with db.begin():
        session = await db.scalar(
            select(RecordingSession).where(RecordingSession.id == session_id).with_for_update()
        )
        if session is None:
            raise TranscriptNotFound("recording session not found")

        if commit_guard is not None and not await commit_guard(db):
            raise TranscriptCommitRejected("canonical transcript commit guard rejected mutation")

        existing = await db.scalar(
            select(TranscriptSegment).where(
                TranscriptSegment.session_id == session_id,
                TranscriptSegment.producer_key == key,
            )
        )
        if existing is not None:
            if not _retry_matches(
                existing,
                start_ms=start,
                end_ms=end,
                text=canonical_text,
                language=canonical_language,
            ):
                raise TranscriptConflict(
                    "transcript producer-key retry conflicts with the committed segment"
                )
            return existing, True

        highest_sequence = await db.scalar(
            select(func.max(TranscriptSegment.sequence)).where(
                TranscriptSegment.session_id == session_id
            )
        )
        segment = TranscriptSegment(
            session_id=session_id,
            sequence=(highest_sequence or 0) + 1,
            producer_key=key,
            start_ms=start,
            end_ms=end,
            text=canonical_text,
            language=canonical_language,
        )
        db.add(segment)
        await db.flush()

    # Queue only after the transaction commits. Redis delivery is an ephemeral wake-up hint,
    # and the bounded notifier never delays canonical/STT completion.
    _notify_realtime_best_effort(segment)
    return segment, False


async def read_transcript_segments(
    db: AsyncSession,
    *,
    session_id: UUID,
    after_sequence: int = 0,
    limit: int = 200,
) -> tuple[list[TranscriptSegment], bool]:
    if after_sequence < 0:
        raise TranscriptConflict("after_sequence must be non-negative")
    if limit < 1 or limit > 1000:
        raise TranscriptConflict("transcript page limit must be between 1 and 1000")

    session_exists = await db.scalar(
        select(RecordingSession.id).where(RecordingSession.id == session_id)
    )
    if session_exists is None:
        raise TranscriptNotFound("recording session not found")

    result = list(
        (
            await db.scalars(
                select(TranscriptSegment)
                .where(
                    TranscriptSegment.session_id == session_id,
                    TranscriptSegment.sequence > after_sequence,
                )
                .order_by(TranscriptSegment.sequence)
                .limit(limit + 1)
            )
        ).all()
    )
    return result[:limit], len(result) > limit
