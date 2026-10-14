from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
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


class MISummaryDispatchStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class MISummarySessionState(Base):
    __tablename__ = "mi_summary_session_states"
    __table_args__ = (
        CheckConstraint(
            "observed_publication_high_water >= 0",
            name="ck_mi_summary_high_water_nonneg",
        ),
        CheckConstraint(
            "finalized_publication_boundary IS NULL OR finalized_publication_boundary >= 0",
            name="ck_mi_summary_final_boundary_nonneg",
        ),
        CheckConstraint(
            "highest_processed_publication >= 0",
            name="ck_mi_summary_processed_nonneg",
        ),
        CheckConstraint(
            "work_intent IN ('open','cancelled','blocked')",
            name="ck_mi_summary_work_intent",
        ),
    )

    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    observed_publication_high_water: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    highest_processed_publication: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    frozen_artifact_publication: Mapped[int | None] = mapped_column(Integer, nullable=True)
    debounce_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_publication_boundary: Mapped[int | None] = mapped_column(Integer, nullable=True)
    work_intent: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default=text("'open'")
    )
    blocked_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    blocked_disposition: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocked_source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocked_config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_final_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frozen_transcript_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frozen_transcript_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frozen_transcript_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    desired_config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    desired_source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    desired_coverage_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_coverage_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    desired_stt_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_stt_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    desired_config_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    processed_config_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    finalize_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    processed_finalize_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    material_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    intelligence_semantically_complete: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MISummaryDispatch(Base):
    __tablename__ = "mi_summary_dispatches"
    __table_args__ = (
        UniqueConstraint("client_request_id", name="uq_mi_summary_dispatch_client_request"),
        CheckConstraint(
            "status IN ('pending','claimed','completed','cancelled','superseded')",
            name="ck_mi_summary_dispatch_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_mi_summary_dispatch_attempts"),
        CheckConstraint("failure_count >= 0", name="ck_mi_summary_dispatch_failures"),
        CheckConstraint(
            "disposition IN ('runnable','awaiting_material','terminal_partial')",
            name="ck_mi_summary_dispatch_disposition",
        ),
        Index("ix_mi_summary_dispatch_pending", "status", "next_attempt_at", "created_at"),
        Index("ix_mi_summary_dispatch_session", "session_id"),
        Index(
            "uq_mi_summary_dispatch_one_open",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'claimed')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MISummaryDispatchStatus.PENDING.value,
        server_default=text("'pending'"),
    )
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    payload_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publication_boundary: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    attempt_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    explicit_retry: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    intent_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    disposition: Mapped[str] = mapped_column(
        String(32), nullable=False, default="runnable", server_default=text("'runnable'")
    )
    progress_token: Mapped[str | None] = mapped_column(String(160), nullable=True)
    stall_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    parked_material_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capacity_deferred: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    last_error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MISummaryRequestReservation(Base):
    """Durable alias for a client request, including coalesced duplicates."""

    __tablename__ = "mi_summary_request_reservations"
    __table_args__ = (Index("ix_mi_summary_request_dispatch", "dispatch_id"),)

    client_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    dispatch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("mi_summary_dispatches.id", ondelete="CASCADE"),
        nullable=False,
    )
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MIHostInvocationLease(Base):
    """Singleton row guarding one active model invocation across processes."""

    __tablename__ = "mi_host_invocation_lease"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_mi_host_lease_singleton"),
        CheckConstraint(
            "active_invocations >= 0 AND active_invocations <= 1",
            name="ck_mi_host_active_invocations",
        ),
        CheckConstraint(
            "holder_state IN ('free','held','quarantine')",
            name="ck_mi_host_lease_state",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, server_default=text("1"))
    holder_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    holder_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    holder_pid_created_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    holder_start_ticks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    holder_namespace: Mapped[str | None] = mapped_column(String(160), nullable=True)
    holder_entrypoint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    holder_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="free", server_default=text("'free'")
    )
    holder_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    owned_runtime_json: Mapped[str] = mapped_column(
        String(4096), nullable=False, default="[]", server_default=text("'[]'")
    )
    termination_acked: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    quarantine_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_invocations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    invocation_admitted: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    spawns_process_tree: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MISummaryScanCursor(Base):
    """Durable keyset cursor so one low-uuid page cannot starve later sessions."""

    __tablename__ = "mi_summary_scan_cursor"
    __table_args__ = (CheckConstraint("id = 1", name="ck_mi_summary_scan_cursor_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, server_default=text("1"))
    canonical_after: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    state_after: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MISummaryMetrics(Base):
    """One logical job: session, full input fingerprint, and config.

    The input fingerprint is the engine snapshot identity when a manifest exists.
    It includes coverage, so a same-boundary STT or coverage change is a new row.
    Source fingerprint stays as a recorded attribute and is not the job key.
    """

    __tablename__ = "mi_summary_metrics"

    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    input_fingerprint: Mapped[str] = mapped_column(
        String(64), primary_key=True, default="", server_default=text("''")
    )
    config_fingerprint: Mapped[str] = mapped_column(
        String(64), primary_key=True, default="", server_default=text("''")
    )
    source_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=text("''")
    )
    original_queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_useful_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    cache_hits: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    observed_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantum_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    observed_event_keys: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default=text("'[]'")
    )
    extract_ms: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    reconcile_ms: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    check_ms: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    render_ms: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
