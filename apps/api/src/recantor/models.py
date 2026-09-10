from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from recantor.db import Base


class SessionKind(StrEnum):
    LIVE = "live"
    GUEST_RECORD = "guest_record"
    UPLOAD = "upload"


class SessionState(StrEnum):
    CREATED = "created"
    RECORDING = "recording"
    INTERRUPTED = "interrupted"
    RECOVERING = "recovering"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    FAILED = "failed"


class AudioCompleteness(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    EMPTY = "empty"


class RecordingSession(Base):
    __tablename__ = "recording_sessions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    client_request_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), unique=True, nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=SessionKind.LIVE.value)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SessionState.CREATED.value, index=True
    )
    active_writer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capture_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recovery_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    interrupted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_monotonic_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finalized_writer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    finalized_capture_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_completeness: Mapped[str | None] = mapped_column(String(32), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RecordingChunk(Base):
    __tablename__ = "recording_chunks"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_recording_chunk_session_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("recording_sessions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    writer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capture_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    monotonic_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    monotonic_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RecordingGap(Base):
    __tablename__ = "recording_gaps"
    __table_args__ = (
        UniqueConstraint("session_id", "client_gap_id", name="uq_recording_gap_client_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("recording_sessions.id", ondelete="CASCADE"), nullable=False
    )
    client_gap_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, default=uuid4)
    writer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capture_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sequence_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wall_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    wall_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "sequence", name="uq_transcript_segment_session_sequence"
        ),
        UniqueConstraint(
            "session_id", "producer_key", name="uq_transcript_segment_session_producer_key"
        ),
        CheckConstraint("sequence >= 1", name="ck_transcript_segment_sequence_positive"),
        CheckConstraint(
            "start_ms >= 0 AND end_ms > start_ms", name="ck_transcript_segment_timing"
        ),
        CheckConstraint(
            "length(btrim(producer_key)) > 0", name="ck_transcript_segment_producer_key_nonblank"
        ),
        CheckConstraint(
            "length(btrim(text)) > 0", name="ck_transcript_segment_text_nonblank"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("recording_sessions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    producer_key: Mapped[str] = mapped_column(String(160), nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
