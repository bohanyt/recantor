"""Add canonical transcript segments.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("producer_key", sa.String(length=160), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(producer_key)) > 0",
            name="ck_transcript_segment_producer_key_nonblank",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_transcript_segment_sequence_positive"),
        sa.CheckConstraint("length(btrim(text)) > 0", name="ck_transcript_segment_text_nonblank"),
        sa.CheckConstraint(
            "start_ms >= 0 AND end_ms > start_ms",
            name="ck_transcript_segment_timing",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["recording_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "producer_key", name="uq_transcript_segment_session_producer_key"
        ),
        sa.UniqueConstraint(
            "session_id", "sequence", name="uq_transcript_segment_session_sequence"
        ),
    )


def downgrade() -> None:
    op.drop_table("transcript_segments")
