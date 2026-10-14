"""Meeting essence ledger (#72).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-14

Downgrade drops every ledger table, including the earlier stage-cache name, so a
database stamped at a previous draft of 0010 can return to 0009 and upgrade again.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEDGER_TABLES = (
    "mi_record_relations",
    "mi_validated_extractions",
    "mi_atomic_records",
    "mi_artifact_versions",
    "mi_attempt_checkpoints",
    "mi_stage_output_cache",
    "mi_job_attempts",
    "mi_intelligence_jobs",
    "mi_source_snapshots",
    "mi_session_source_fences",
)


def upgrade() -> None:
    op.create_table(
        "mi_session_source_fences",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("source_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("current_snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("current_snapshot_content_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("source_epoch >= 0", name="ck_mi_source_epoch_nonnegative"),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "mi_source_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_session_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_content_hash", sa.String(length=64), nullable=False),
        sa.Column("publication_boundary_sequence", sa.Integer(), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("superseded_by_snapshot_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "manifest_session_id = session_id",
            name="ck_mi_snapshot_manifest_session",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "snapshot_id", name="uq_mi_snapshot_session_id"),
        sa.UniqueConstraint(
            "session_id",
            "snapshot_content_hash",
            name="uq_mi_snapshot_session_content_hash",
        ),
    )
    op.create_index("ix_mi_source_snapshots_session", "mi_source_snapshots", ["session_id"])
    op.create_table(
        "mi_intelligence_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("client_request_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("run_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("prompt_cache_key", sa.String(length=64), nullable=False),
        sa.Column("schema_identity", sa.String(length=160), nullable=False),
        sa.Column("algorithm_identity", sa.String(length=160), nullable=False),
        sa.Column("limits_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider_adapter_id", sa.String(length=96), nullable=False),
        sa.Column("requested_model", sa.String(length=128), server_default="", nullable=False),
        sa.Column(
            "requested_reasoning_effort",
            sa.String(length=64),
            server_default="",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("active_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("current_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("stale_artifact_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','partial','failed','cancelled')",
            name="ck_mi_job_status",
        ),
        sa.CheckConstraint("active_generation >= 0", name="ck_mi_job_generation_nonnegative"),
        sa.CheckConstraint("source_epoch >= 0", name="ck_mi_job_source_epoch_nonnegative"),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "session_id", name="uq_mi_job_id_session"),
        sa.UniqueConstraint("client_request_id", name="uq_mi_job_client_request_id"),
        sa.UniqueConstraint(
            "session_id", "run_fingerprint", name="uq_mi_job_session_run_fingerprint"
        ),
    )
    op.create_index("ix_mi_jobs_session", "mi_intelligence_jobs", ["session_id"])
    op.create_table(
        "mi_job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("source_epoch", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("run_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="running", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','succeeded','partial','failed','cancelled')",
            name="ck_mi_attempt_status",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_mi_attempt_generation_positive"),
        sa.CheckConstraint("source_epoch >= 0", name="ck_mi_attempt_source_epoch_nonnegative"),
        sa.ForeignKeyConstraint(
            ["job_id", "session_id"],
            ["mi_intelligence_jobs.id", "mi_intelligence_jobs.session_id"],
            name="fk_mi_attempt_job_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "generation", name="uq_mi_attempt_job_generation"),
        sa.UniqueConstraint(
            "id",
            "job_id",
            "session_id",
            "generation",
            name="uq_mi_attempt_owner_key",
        ),
    )
    op.create_index("ix_mi_attempts_job", "mi_job_attempts", ["job_id"])
    op.create_index("ix_mi_attempts_session", "mi_job_attempts", ["session_id"])
    op.create_table(
        "mi_attempt_checkpoints",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("checkpoint_id", sa.Uuid(), nullable=False),
        sa.Column("body_json", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_table(
        "mi_validated_extractions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_key", sa.String(length=64), nullable=False),
        sa.Column("prompt_cache_key", sa.String(length=64), nullable=False),
        sa.Column("schema_identity", sa.String(length=160), nullable=False),
        sa.Column("algorithm_identity", sa.String(length=160), nullable=False),
        sa.Column("limits_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider_adapter_id", sa.String(length=96), nullable=False),
        sa.Column("requested_model", sa.String(length=128), server_default="", nullable=False),
        sa.Column(
            "requested_reasoning_effort",
            sa.String(length=64),
            server_default="",
            nullable=False,
        ),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "extraction_key",
            name="uq_mi_validated_extraction_key",
        ),
    )
    op.create_index(
        "ix_mi_validated_extractions_session",
        "mi_validated_extractions",
        ["session_id"],
    )
    op.create_table(
        "mi_artifact_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_content_hash", sa.String(length=64), nullable=False),
        sa.Column("run_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("processing_status", sa.String(length=32), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("layered_result_json", sa.Text(), nullable=True),
        sa.Column("limitations_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("records_ledger_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("relations_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "processing_status IN ('succeeded','partial','failed','cancelled')",
            name="ck_mi_artifact_processing_status",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_mi_artifact_attempt"),
    )
    op.create_index("ix_mi_artifacts_job", "mi_artifact_versions", ["job_id"])
    op.create_index("ix_mi_artifacts_session", "mi_artifact_versions", ["session_id"])
    op.create_index(
        "uq_mi_artifact_one_current_per_session",
        "mi_artifact_versions",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "mi_atomic_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("record_id", sa.String(length=96), nullable=False),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "snapshot_id"],
            ["mi_source_snapshots.session_id", "mi_source_snapshots.snapshot_id"],
            name="fk_mi_record_snapshot_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "snapshot_id",
            "record_id",
            "content_fingerprint",
            name="uq_mi_atomic_record_version",
        ),
    )
    op.create_index(
        "ix_mi_atomic_records_session_snapshot",
        "mi_atomic_records",
        ["session_id", "snapshot_id"],
    )
    op.create_table(
        "mi_record_relations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("relation_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("relation_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["mi_artifact_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "snapshot_id"],
            ["mi_source_snapshots.session_id", "mi_source_snapshots.snapshot_id"],
            name="fk_mi_relation_snapshot_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "artifact_id",
            "relation_fingerprint",
            name="uq_mi_relation_artifact_fingerprint",
        ),
    )
    op.create_index("ix_mi_relations_artifact", "mi_record_relations", ["artifact_id"])


def downgrade() -> None:
    for name in _LEDGER_TABLES:
        op.execute(sa.text(f"DROP TABLE IF EXISTS {name} CASCADE"))
