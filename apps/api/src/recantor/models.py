from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
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
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    COMPLETE = "complete"
    FAILED = "failed"


class AudioCompleteness(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    EMPTY = "empty"


class STTJobState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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


class UploadRecord(Base):
    __tablename__ = "upload_records"
    __table_args__ = (
        CheckConstraint("declared_byte_length > 0", name="ck_upload_declared_byte_length_positive"),
        CheckConstraint("received_bytes >= 0", name="ck_upload_received_bytes_nonnegative"),
        CheckConstraint(
            "received_bytes <= declared_byte_length",
            name="ck_upload_received_bytes_within_declared",
        ),
        CheckConstraint(
            "declared_duration_ms IS NULL OR declared_duration_ms > 0",
            name="ck_upload_declared_duration_positive",
        ),
        CheckConstraint(
            "tus_upload_id IS NULL OR length(btrim(tus_upload_id)) > 0",
            name="ck_upload_tus_id_nonblank",
        ),
        CheckConstraint(
            "byte_length IS NULL OR byte_length = declared_byte_length",
            name="ck_upload_byte_length_matches_declared",
        ),
        CheckConstraint(
            "(completed_at IS NULL AND storage_key IS NULL AND sha256 IS NULL "
            "AND byte_length IS NULL) OR "
            "(completed_at IS NOT NULL AND storage_key IS NOT NULL "
            "AND length(btrim(storage_key)) > 0 AND sha256 IS NOT NULL "
            "AND sha256 ~ '^[0-9a-f]{64}$' AND byte_length > 0)",
            name="ck_upload_completion_shape",
        ),
        Index("ix_upload_records_expires_at", "expires_at"),
    )

    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    declared_byte_length: Mapped[int] = mapped_column(BigInteger, nullable=False)
    declared_duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tus_upload_id: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True)
    received_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    byte_length: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_transcript_segment_session_sequence"),
        UniqueConstraint(
            "session_id", "producer_key", name="uq_transcript_segment_session_producer_key"
        ),
        CheckConstraint("sequence >= 1", name="ck_transcript_segment_sequence_positive"),
        CheckConstraint("start_ms >= 0 AND end_ms > start_ms", name="ck_transcript_segment_timing"),
        CheckConstraint(
            "length(btrim(producer_key)) > 0", name="ck_transcript_segment_producer_key_nonblank"
        ),
        CheckConstraint("length(btrim(text)) > 0", name="ck_transcript_segment_text_nonblank"),
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


class TranscriptionUtterance(Base):
    __tablename__ = "transcription_utterances"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "sequence", name="uq_transcription_utterance_session_sequence"
        ),
        UniqueConstraint(
            "session_id", "producer_key", name="uq_transcription_utterance_session_producer_key"
        ),
        UniqueConstraint("id", "session_id", name="uq_transcription_utterance_id_session"),
        CheckConstraint("sequence >= 1", name="ck_transcription_utterance_sequence_positive"),
        CheckConstraint(
            "start_ms >= 0 AND end_ms > start_ms", name="ck_transcription_utterance_timing"
        ),
        CheckConstraint(
            "length(btrim(producer_key)) > 0",
            name="ck_transcription_utterance_producer_key_nonblank",
        ),
        CheckConstraint(
            "length(btrim(content_type)) > 0",
            name="ck_transcription_utterance_content_type_nonblank",
        ),
        CheckConstraint("byte_length > 0", name="ck_transcription_utterance_byte_length_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("recording_sessions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    producer_key: Mapped[str] = mapped_column(String(160), nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class STTJob(Base):
    __tablename__ = "stt_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["utterance_id", "session_id"],
            ["transcription_utterances.id", "transcription_utterances.session_id"],
            name="fk_stt_job_utterance_session",
            ondelete="CASCADE",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_stt_job_attempt_count_nonnegative"),
        CheckConstraint(
            "state IN ('pending', 'claimed', 'retry_wait', 'succeeded', 'failed')",
            name="ck_stt_job_state",
        ),
        CheckConstraint(
            "(state = 'claimed' AND claim_token IS NOT NULL "
            "AND length(btrim(claim_token)) > 0 AND claim_expires_at IS NOT NULL) "
            "OR (state <> 'claimed' AND claim_token IS NULL AND claim_expires_at IS NULL)",
            name="ck_stt_job_claim_shape",
        ),
        CheckConstraint(
            "(state = 'retry_wait' AND next_attempt_at IS NOT NULL) "
            "OR (state <> 'retry_wait' AND next_attempt_at IS NULL)",
            name="ck_stt_job_retry_time_shape",
        ),
        Index("ix_stt_jobs_session_state", "session_id", "state"),
        Index("ix_stt_jobs_state_next_attempt", "state", "next_attempt_at"),
        Index("ix_stt_jobs_claim_expiry", "state", "claim_expires_at"),
        Index("ix_stt_jobs_session_delivery", "session_id", "last_delivery_attempt_at"),
    )

    utterance_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("recording_sessions.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=STTJobState.PENDING.value,
        server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_delivery_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
