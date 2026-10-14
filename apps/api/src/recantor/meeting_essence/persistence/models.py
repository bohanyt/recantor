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


class MIJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MIAttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class MISessionSourceFence(Base):
    __tablename__ = "mi_session_source_fences"
    __table_args__ = (CheckConstraint("source_epoch >= 0", name="ck_mi_source_epoch_nonnegative"),)

    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    current_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_snapshot_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authority_publication_boundary: Mapped[int | None] = mapped_column(Integer, nullable=True)
    authority_metadata_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MISourceSnapshot(Base):
    __tablename__ = "mi_source_snapshots"
    __table_args__ = (
        UniqueConstraint("session_id", "snapshot_id", name="uq_mi_snapshot_session_id"),
        UniqueConstraint(
            "session_id",
            "snapshot_content_hash",
            name="uq_mi_snapshot_session_content_hash",
        ),
        CheckConstraint(
            "manifest_session_id = session_id",
            name="ck_mi_snapshot_manifest_session",
        ),
        Index("ix_mi_source_snapshots_session", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    manifest_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_boundary_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    superseded_by_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MIIntelligenceJob(Base):
    __tablename__ = "mi_intelligence_jobs"
    __table_args__ = (
        UniqueConstraint("id", "session_id", name="uq_mi_job_id_session"),
        UniqueConstraint("client_request_id", name="uq_mi_job_client_request_id"),
        UniqueConstraint("session_id", "run_fingerprint", name="uq_mi_job_session_run_fingerprint"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','partial','failed','cancelled')",
            name="ck_mi_job_status",
        ),
        CheckConstraint("active_generation >= 0", name="ck_mi_job_generation_nonnegative"),
        CheckConstraint("source_epoch >= 0", name="ck_mi_job_source_epoch_nonnegative"),
        Index("ix_mi_jobs_session", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    run_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    algorithm_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    limits_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_adapter_id: Mapped[str] = mapped_column(String(96), nullable=False)
    requested_model: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=text("''")
    )
    requested_reasoning_effort: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=text("''")
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MIJobStatus.PENDING.value,
        server_default=text("'pending'"),
    )
    active_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    current_artifact_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    stale_artifact_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MIJobRequestAlias(Base):
    """Every client request id that resolved to a job, including fingerprint aliases."""

    __tablename__ = "mi_job_request_aliases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "session_id"],
            ["mi_intelligence_jobs.id", "mi_intelligence_jobs.session_id"],
            name="fk_mi_request_alias_job",
            ondelete="CASCADE",
        ),
        Index("ix_mi_request_aliases_job", "job_id"),
    )

    client_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    algorithm_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    limits_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_adapter_id: Mapped[str] = mapped_column(String(96), nullable=False)
    requested_model: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=text("''")
    )
    requested_reasoning_effort: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=text("''")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MIJobAttempt(Base):
    __tablename__ = "mi_job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "generation", name="uq_mi_attempt_job_generation"),
        UniqueConstraint(
            "id",
            "job_id",
            "session_id",
            "generation",
            name="uq_mi_attempt_owner_key",
        ),
        CheckConstraint(
            "status IN ('running','succeeded','partial','failed','cancelled','superseded')",
            name="ck_mi_attempt_status",
        ),
        CheckConstraint("generation >= 1", name="ck_mi_attempt_generation_positive"),
        CheckConstraint("source_epoch >= 0", name="ck_mi_attempt_source_epoch_nonnegative"),
        ForeignKeyConstraint(
            ["job_id", "session_id"],
            ["mi_intelligence_jobs.id", "mi_intelligence_jobs.session_id"],
            name="fk_mi_attempt_job_session",
            ondelete="CASCADE",
        ),
        Index("ix_mi_attempts_job", "job_id"),
        Index("ix_mi_attempts_session", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    source_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MIAttemptStatus.RUNNING.value,
        server_default=text("'running'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MIAttemptCheckpoint(Base):
    __tablename__ = "mi_attempt_checkpoints"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "job_id", "session_id", "generation"],
            [
                "mi_job_attempts.id",
                "mi_job_attempts.job_id",
                "mi_job_attempts.session_id",
                "mi_job_attempts.generation",
            ],
            name="fk_mi_checkpoint_attempt_owner",
            ondelete="CASCADE",
        ),
    )

    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    body_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MIValidatedExtraction(Base):
    __tablename__ = "mi_validated_extractions"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "extraction_key",
            name="uq_mi_validated_extraction_key",
        ),
        Index("ix_mi_validated_extractions_session", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    extraction_key: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    algorithm_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    limits_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_adapter_id: Mapped[str] = mapped_column(String(96), nullable=False)
    requested_model: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=text("''")
    )
    requested_reasoning_effort: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=text("''")
    )
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MIArtifactVersion(Base):
    __tablename__ = "mi_artifact_versions"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_mi_artifact_attempt"),
        UniqueConstraint("id", "session_id", name="uq_mi_artifact_id_session"),
        CheckConstraint(
            "processing_status IN ('succeeded','partial','failed','cancelled')",
            name="ck_mi_artifact_processing_status",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "job_id", "session_id", "generation"],
            [
                "mi_job_attempts.id",
                "mi_job_attempts.job_id",
                "mi_job_attempts.session_id",
                "mi_job_attempts.generation",
            ],
            name="fk_mi_artifact_attempt_owner",
            ondelete="CASCADE",
        ),
        Index("ix_mi_artifacts_job", "job_id"),
        Index("ix_mi_artifacts_session", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_current: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    is_stale: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    layered_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    limitations_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default=text("'[]'")
    )
    records_ledger_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default=text("'[]'")
    )
    relations_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default=text("'[]'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MIAtomicRecordRow(Base):
    __tablename__ = "mi_atomic_records"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "snapshot_id",
            "record_id",
            "content_fingerprint",
            name="uq_mi_atomic_record_version",
        ),
        ForeignKeyConstraint(
            ["session_id", "snapshot_id"],
            ["mi_source_snapshots.session_id", "mi_source_snapshots.snapshot_id"],
            name="fk_mi_record_snapshot_owner",
            ondelete="CASCADE",
        ),
        Index("ix_mi_atomic_records_session_snapshot", "session_id", "snapshot_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    record_id: Mapped[str] = mapped_column(String(96), nullable=False)
    record_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MIRecordRelationRow(Base):
    __tablename__ = "mi_record_relations"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "relation_fingerprint",
            name="uq_mi_relation_artifact_fingerprint",
        ),
        ForeignKeyConstraint(
            ["session_id", "snapshot_id"],
            ["mi_source_snapshots.session_id", "mi_source_snapshots.snapshot_id"],
            name="fk_mi_relation_snapshot_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["artifact_id", "session_id"],
            ["mi_artifact_versions.id", "mi_artifact_versions.session_id"],
            name="fk_mi_relation_artifact_session",
            ondelete="CASCADE",
        ),
        Index("ix_mi_relations_artifact", "artifact_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("mi_artifact_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    relation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    relation_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
