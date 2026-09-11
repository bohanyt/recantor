"""Add resumable existing-recording upload foundation.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_records",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("declared_byte_length", sa.BigInteger(), nullable=False),
        sa.Column("declared_duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("tus_upload_id", sa.String(length=160), nullable=True),
        sa.Column("received_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("byte_length", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.String(length=512), nullable=True),
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
            "declared_byte_length > 0",
            name="ck_upload_declared_byte_length_positive",
        ),
        sa.CheckConstraint("received_bytes >= 0", name="ck_upload_received_bytes_nonnegative"),
        sa.CheckConstraint(
            "received_bytes <= declared_byte_length",
            name="ck_upload_received_bytes_within_declared",
        ),
        sa.CheckConstraint(
            "declared_duration_ms IS NULL OR declared_duration_ms > 0",
            name="ck_upload_declared_duration_positive",
        ),
        sa.CheckConstraint(
            "tus_upload_id IS NULL OR length(btrim(tus_upload_id)) > 0",
            name="ck_upload_tus_id_nonblank",
        ),
        sa.CheckConstraint(
            "byte_length IS NULL OR byte_length = declared_byte_length",
            name="ck_upload_byte_length_matches_declared",
        ),
        sa.CheckConstraint(
            "(completed_at IS NULL AND storage_key IS NULL AND byte_length IS NULL) OR "
            "(completed_at IS NOT NULL AND storage_key IS NOT NULL AND byte_length > 0)",
            name="ck_upload_completion_shape",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("tus_upload_id", name="uq_upload_records_tus_upload_id"),
    )
    op.create_index("ix_upload_records_expires_at", "upload_records", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_upload_records_expires_at", table_name="upload_records")
    op.drop_table("upload_records")
