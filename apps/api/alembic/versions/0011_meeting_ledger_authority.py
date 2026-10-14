"""Meeting ledger authority, exclusive attempts, and request aliases (#72).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mi_session_source_fences",
        sa.Column("authority_publication_boundary", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mi_session_source_fences",
        sa.Column("authority_metadata_fingerprint", sa.String(length=64), nullable=True),
    )
    op.drop_constraint("ck_mi_attempt_status", "mi_job_attempts", type_="check")
    op.create_check_constraint(
        "ck_mi_attempt_status",
        "mi_job_attempts",
        "status IN ('running','succeeded','partial','failed','cancelled','superseded')",
    )
    op.create_unique_constraint(
        "uq_mi_artifact_id_session",
        "mi_artifact_versions",
        ["id", "session_id"],
    )
    op.create_foreign_key(
        "fk_mi_relation_artifact_session",
        "mi_record_relations",
        "mi_artifact_versions",
        ["artifact_id", "session_id"],
        ["id", "session_id"],
        ondelete="CASCADE",
    )
    op.create_table(
        "mi_job_request_aliases",
        sa.Column("client_request_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("run_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
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
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "session_id"],
            ["mi_intelligence_jobs.id", "mi_intelligence_jobs.session_id"],
            name="fk_mi_request_alias_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("client_request_id"),
    )
    op.create_index("ix_mi_request_aliases_job", "mi_job_request_aliases", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_mi_request_aliases_job", table_name="mi_job_request_aliases")
    op.drop_table("mi_job_request_aliases")
    op.drop_constraint("fk_mi_relation_artifact_session", "mi_record_relations", type_="foreignkey")
    op.drop_constraint("uq_mi_artifact_id_session", "mi_artifact_versions", type_="unique")
    op.drop_constraint("ck_mi_attempt_status", "mi_job_attempts", type_="check")
    op.create_check_constraint(
        "ck_mi_attempt_status",
        "mi_job_attempts",
        "status IN ('running','succeeded','partial','failed','cancelled')",
    )
    op.drop_column("mi_session_source_fences", "authority_metadata_fingerprint")
    op.drop_column("mi_session_source_fences", "authority_publication_boundary")
