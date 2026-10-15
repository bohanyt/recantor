"""Durable summary cursors. Capture calls the scheduler, never awaits it."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import case, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.meeting_essence.scheduling.models import MISummarySessionState
from recantor.settings import get_settings

logger = logging.getLogger(__name__)
_NOTICE_TIMEOUT_SECONDS = 0.05
_background: set[asyncio.Task[None]] = set()


def _debounce_until(now: datetime) -> datetime:
    settings = get_settings()
    return now + timedelta(seconds=float(settings.meeting_essence_debounce_seconds))


async def note_transcript_publication(
    db: AsyncSession,
    *,
    session_id: UUID,
    publication_sequence: int,
) -> None:
    """Raise the observed cursor monotonically. A late lower sequence is a no-op."""
    if publication_sequence < 0:
        raise ValueError("publication sequence must be non-negative")
    now = datetime.now(tz=UTC)
    debounce = _debounce_until(now)
    observed = MISummarySessionState.observed_publication_high_water
    stmt = (
        insert(MISummarySessionState)
        .values(
            session_id=session_id,
            observed_publication_high_water=publication_sequence,
            debounce_until=debounce,
        )
        .on_conflict_do_update(
            index_elements=[MISummarySessionState.session_id],
            set_={
                "observed_publication_high_water": func.greatest(observed, publication_sequence),
                "debounce_until": debounce,
                "updated_at": now,
            },
        )
    )
    await db.execute(stmt)


async def note_session_finalized(
    db: AsyncSession,
    *,
    session_id: UUID,
    audio_final_sequence: int,
    transcript_sequence: int,
    transcript_hash: str,
) -> None:
    """Record the audio stop cursor and freeze the canonical transcript that exists now.

    The frozen fields are the publication sequence and content hash available at
    Stop. They are not a meeting-intelligence artifact version, and this path does
    not invent one. Later transcript publications advance the observed cursor.
    They do not replace the frozen sequence or hash. The audio cursor is not a
    transcript cap. Pending STT stays visible on the coverage flags, and a later
    publication is a new logical input.
    """
    if audio_final_sequence < 0 or transcript_sequence < 0:
        raise ValueError("final cursors must be non-negative")
    if len(transcript_hash) != 64:
        raise ValueError("transcript hash must be 64 hex characters")
    now = datetime.now(tz=UTC)
    debounce = _debounce_until(now)
    frozen = MISummarySessionState.frozen_transcript_sequence
    epoch = MISummarySessionState.finalize_epoch
    audio = MISummarySessionState.audio_final_sequence
    stmt = (
        insert(MISummarySessionState)
        .values(
            session_id=session_id,
            audio_final_sequence=audio_final_sequence,
            finalized_publication_boundary=transcript_sequence,
            frozen_artifact_publication=transcript_sequence,
            frozen_transcript_sequence=transcript_sequence,
            frozen_transcript_hash=transcript_hash,
            finalize_epoch=1,
            debounce_until=debounce,
            intelligence_semantically_complete=False,
        )
        .on_conflict_do_update(
            index_elements=[MISummarySessionState.session_id],
            set_={
                "audio_final_sequence": func.greatest(
                    func.coalesce(audio, audio_final_sequence), audio_final_sequence
                ),
                "finalized_publication_boundary": func.coalesce(frozen, transcript_sequence),
                "frozen_artifact_publication": func.coalesce(frozen, transcript_sequence),
                "frozen_transcript_sequence": func.coalesce(frozen, transcript_sequence),
                "frozen_transcript_hash": func.coalesce(
                    MISummarySessionState.frozen_transcript_hash, transcript_hash
                ),
                "finalize_epoch": case((frozen.is_(None), epoch + 1), else_=epoch),
                "debounce_until": debounce,
                "intelligence_semantically_complete": False,
                "updated_at": now,
            },
        )
    )
    await db.execute(stmt)


def schedule_publication_notice(*, session_id: UUID, publication_sequence: int) -> None:
    """Return immediately. A lost notice is recovered by the canonical scan."""
    _schedule(_bounded_publication_notice(session_id, publication_sequence))


def schedule_finalized_notice(*, session_id: UUID, audio_final_sequence: int) -> None:
    _schedule(_bounded_finalized_notice(session_id, audio_final_sequence))


def _schedule(coro: object) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()  # type: ignore[attr-defined]
        return
    task = loop.create_task(coro)  # type: ignore[arg-type]
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _bounded_publication_notice(session_id: UUID, publication_sequence: int) -> None:
    try:
        await asyncio.wait_for(
            _write_publication(session_id, publication_sequence),
            timeout=_NOTICE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.info(
            "summary publication notice deferred session=%s sequence=%s",
            session_id,
            publication_sequence,
        )


async def _bounded_finalized_notice(session_id: UUID, audio_final_sequence: int) -> None:
    try:
        await asyncio.wait_for(
            _write_finalized(session_id, audio_final_sequence),
            timeout=_NOTICE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.info(
            "summary finalize notice deferred session=%s audio_final=%s",
            session_id,
            audio_final_sequence,
        )


async def _write_publication(session_id: UUID, publication_sequence: int) -> None:
    from recantor.db import get_sessionmaker

    async with get_sessionmaker()() as db, db.begin():
        await note_transcript_publication(
            db, session_id=session_id, publication_sequence=publication_sequence
        )


async def _write_finalized(session_id: UUID, audio_final_sequence: int) -> None:
    from recantor.db import get_sessionmaker
    from recantor.meeting_essence.scheduling.source_material import load_transcript_artifact

    async with get_sessionmaker()() as db, db.begin():
        transcript_sequence, transcript_hash = await load_transcript_artifact(
            db, session_id=session_id
        )
        await note_session_finalized(
            db,
            session_id=session_id,
            audio_final_sequence=audio_final_sequence,
            transcript_sequence=transcript_sequence,
            transcript_hash=transcript_hash,
        )
