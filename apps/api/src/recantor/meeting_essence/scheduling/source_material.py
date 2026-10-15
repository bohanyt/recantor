"""Canonical source loading. Audio, STT, and publication sequence stay distinct."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.meeting_essence.contracts import SourceCoverageFlags, TranscriptGapMarker
from recantor.meeting_essence.scheduling.models import MISummarySessionState
from recantor.meeting_essence.source_reader import (
    CanonicalSegmentInput,
    InMemoryCanonicalSourceReader,
)
from recantor.models import (
    AudioCompleteness,
    RecordingGap,
    RecordingSession,
    SessionState,
    STTJob,
    STTJobState,
    TranscriptSegment,
)

_STT_OPEN = (
    STTJobState.PENDING.value,
    STTJobState.CLAIMED.value,
    STTJobState.RETRY_WAIT.value,
)


async def load_transcript_artifact(db: AsyncSession, *, session_id: UUID) -> tuple[int, str]:
    """Sequence and content hash of the canonical transcript available now.

    The hash covers sequence, id, speech times, language, and text. It is the
    frozen source boundary, not a meeting-intelligence artifact version. Audio
    cursors are not included.
    """
    rows = (
        await db.execute(
            select(
                TranscriptSegment.sequence,
                TranscriptSegment.id,
                TranscriptSegment.start_ms,
                TranscriptSegment.end_ms,
                TranscriptSegment.language,
                TranscriptSegment.text,
            )
            .where(TranscriptSegment.session_id == session_id)
            .order_by(TranscriptSegment.sequence, TranscriptSegment.id)
        )
    ).all()
    digest = hashlib.sha256()
    maximum = 0
    for sequence, segment_id, start_ms, end_ms, language, text in rows:
        maximum = max(maximum, int(sequence))
        digest.update(
            f"{int(sequence)}|{segment_id}|{start_ms}|{end_ms}|{language or ''}|{text}\n".encode()
        )
    return maximum, digest.hexdigest()


async def load_session_reader(
    db: AsyncSession,
    *,
    session_id: UUID,
    publication_boundary_sequence: int,
) -> InMemoryCanonicalSourceReader:
    rows = list(
        await db.scalars(
            select(TranscriptSegment)
            .where(
                TranscriptSegment.session_id == session_id,
                TranscriptSegment.sequence <= publication_boundary_sequence,
            )
            .order_by(TranscriptSegment.sequence, TranscriptSegment.id)
        )
    )
    segments = [
        CanonicalSegmentInput(
            id=row.id,
            sequence=row.sequence,
            start_ms=row.start_ms,
            end_ms=row.end_ms,
            text=row.text,
            language=row.language,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return InMemoryCanonicalSourceReader(segments=segments)


async def build_coverage_flags(
    db: AsyncSession,
    *,
    session_id: UUID,
    publication_boundary_sequence: int,
    summary_state: MISummarySessionState | None,
) -> SourceCoverageFlags:
    del summary_state
    session = await db.get(RecordingSession, session_id)
    gaps = list(
        await db.scalars(
            select(RecordingGap)
            .where(RecordingGap.session_id == session_id)
            .order_by(RecordingGap.sequence_start, RecordingGap.id)
        )
    )
    explicit = [
        TranscriptGapMarker(
            sequence_start=gap.sequence_start,
            sequence_end=gap.sequence_end,
            reason=gap.reason,
        )
        for gap in gaps
    ]
    audio_state_complete = session is not None and session.state == SessionState.COMPLETE.value
    audio_full = (
        audio_state_complete
        and session is not None
        and session.audio_completeness == AudioCompleteness.FULL.value
        and not gaps
    )
    max_seq = int(
        await db.scalar(
            select(func.max(TranscriptSegment.sequence)).where(
                TranscriptSegment.session_id == session_id
            )
        )
        or 0
    )
    open_stt = int(
        await db.scalar(
            select(func.count())
            .select_from(STTJob)
            .where(STTJob.session_id == session_id, STTJob.state.in_(_STT_OPEN))
        )
        or 0
    )
    transcript_complete = max_seq >= publication_boundary_sequence and max_seq > 0
    return SourceCoverageFlags(
        transcript_publication_complete=transcript_complete,
        recording_audio_complete=audio_full,
        stt_backlog_pending=open_stt > 0,
        source_pending=max_seq < publication_boundary_sequence,
        explicit_gaps=explicit,
    )


@dataclass(frozen=True)
class MaterialSnapshot:
    max_sequence: int
    source_fingerprint: str
    coverage_fingerprint: str
    stt_fingerprint: str
    audio_final_sequence: int | None
    recording_complete: bool
    open_stt: int


def _digest(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


async def load_material_snapshot(
    db: AsyncSession, *, session_id: UUID, publication_boundary_sequence: int
) -> MaterialSnapshot:
    session = await db.get(RecordingSession, session_id)
    max_seq = int(
        await db.scalar(
            select(func.max(TranscriptSegment.sequence)).where(
                TranscriptSegment.session_id == session_id
            )
        )
        or 0
    )
    segment_count = int(
        await db.scalar(
            select(func.count())
            .select_from(TranscriptSegment)
            .where(TranscriptSegment.session_id == session_id)
        )
        or 0
    )
    open_stt = int(
        await db.scalar(
            select(func.count())
            .select_from(STTJob)
            .where(STTJob.session_id == session_id, STTJob.state.in_(_STT_OPEN))
        )
        or 0
    )
    done_stt = int(
        await db.scalar(
            select(func.count())
            .select_from(STTJob)
            .where(STTJob.session_id == session_id, STTJob.state == STTJobState.SUCCEEDED.value)
        )
        or 0
    )
    gap_count = int(
        await db.scalar(
            select(func.count())
            .select_from(RecordingGap)
            .where(RecordingGap.session_id == session_id)
        )
        or 0
    )
    flags = await build_coverage_flags(
        db,
        session_id=session_id,
        publication_boundary_sequence=publication_boundary_sequence,
        summary_state=None,
    )
    audio_final = None if session is None else session.final_sequence
    audio_state = "" if session is None else session.state
    audio_complete = "" if session is None else (session.audio_completeness or "")
    recording_complete = session is not None and session.state == SessionState.COMPLETE.value
    _transcript_sequence, transcript_hash = await load_transcript_artifact(
        db, session_id=session_id
    )
    return MaterialSnapshot(
        max_sequence=max_seq,
        source_fingerprint=_digest(max_seq, segment_count, transcript_hash),
        coverage_fingerprint=_digest(
            flags.transcript_publication_complete,
            flags.recording_audio_complete,
            flags.stt_backlog_pending,
            flags.source_pending,
            gap_count,
            audio_state,
            audio_complete,
            audio_final or 0,
        ),
        stt_fingerprint=_digest(open_stt, done_stt),
        audio_final_sequence=audio_final,
        recording_complete=recording_complete,
        open_stt=open_stt,
    )
