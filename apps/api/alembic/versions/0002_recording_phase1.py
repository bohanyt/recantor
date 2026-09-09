"""Add Phase 1 recording session, chunk, and gap ledgers.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recording_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_request_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("active_writer_id", sa.String(length=128), nullable=True),
        sa.Column("capture_epoch", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("interrupted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_sequence", sa.Integer(), nullable=True),
        sa.Column("final_monotonic_end_ms", sa.Integer(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_request_id"),
    )
    op.create_index(
        "ix_recording_sessions_client_request_id", "recording_sessions", ["client_request_id"]
    )
    op.create_index("ix_recording_sessions_state", "recording_sessions", ["state"])

    op.create_table(
        "recording_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("capture_epoch", sa.Integer(), nullable=False),
        sa.Column("monotonic_start_ms", sa.Integer(), nullable=False),
        sa.Column("monotonic_end_ms", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "sequence", name="uq_recording_chunk_session_sequence"),
    )
    op.create_index("ix_recording_chunks_session_id", "recording_chunks", ["session_id"])

    op.create_table(
        "recording_gaps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("client_gap_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_start", sa.Integer(), nullable=True),
        sa.Column("sequence_end", sa.Integer(), nullable=True),
        sa.Column("wall_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wall_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "client_gap_id", name="uq_recording_gap_client_id"),
    )
    op.create_index("ix_recording_gaps_session_id", "recording_gaps", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_recording_gaps_session_id", table_name="recording_gaps")
    op.drop_table("recording_gaps")
    op.drop_index("ix_recording_chunks_session_id", table_name="recording_chunks")
    op.drop_table("recording_chunks")
    op.drop_index("ix_recording_sessions_state", table_name="recording_sessions")
    op.drop_index("ix_recording_sessions_client_request_id", table_name="recording_sessions")
    op.drop_table("recording_sessions")
