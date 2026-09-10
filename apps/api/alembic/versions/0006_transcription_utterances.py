"""Add durable transcription utterance work.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcription_utterances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("producer_key", sa.String(length=160), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(content_type)) > 0",
            name="ck_transcription_utterance_content_type_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(producer_key)) > 0",
            name="ck_transcription_utterance_producer_key_nonblank",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_transcription_utterance_sequence_positive",
        ),
        sa.CheckConstraint(
            "start_ms >= 0 AND end_ms > start_ms",
            name="ck_transcription_utterance_timing",
        ),
        sa.CheckConstraint(
            "byte_length > 0",
            name="ck_transcription_utterance_byte_length_positive",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "producer_key",
            name="uq_transcription_utterance_session_producer_key",
        ),
        sa.UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_transcription_utterance_session_sequence",
        ),
    )


def downgrade() -> None:
    op.drop_table("transcription_utterances")
