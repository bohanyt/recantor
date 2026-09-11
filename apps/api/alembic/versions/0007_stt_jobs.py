"""Add durable live STT scheduling state.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stt_jobs",
        sa.Column("utterance_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_delivery_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_category", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=96), nullable=True),
        sa.Column("last_error_message", sa.String(length=512), nullable=True),
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
            "attempt_count >= 0",
            name="ck_stt_job_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'retry_wait', 'succeeded', 'failed')",
            name="ck_stt_job_state",
        ),
        sa.CheckConstraint(
            "(state = 'claimed' AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL) "
            "OR (state <> 'claimed' AND claim_token IS NULL AND claim_expires_at IS NULL)",
            name="ck_stt_job_claim_shape",
        ),
        sa.CheckConstraint(
            "state <> 'retry_wait' OR next_attempt_at IS NOT NULL",
            name="ck_stt_job_retry_wait_has_time",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["recording_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["utterance_id"],
            ["transcription_utterances.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("utterance_id"),
    )
    op.create_index(
        "ix_stt_jobs_session_state",
        "stt_jobs",
        ["session_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_stt_jobs_state_next_attempt",
        "stt_jobs",
        ["state", "next_attempt_at"],
        unique=False,
    )

    # Backfill every already-durable utterance. Canonical transcript evidence is authoritative:
    # historical utterances that already have it enter scheduling as succeeded.
    op.execute(
        sa.text(
            """
            INSERT INTO stt_jobs (utterance_id, session_id, state, attempt_count)
            SELECT
                u.id,
                u.session_id,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM transcript_segments AS t
                        WHERE t.session_id = u.session_id
                          AND t.producer_key = 'utterance:' || u.id::text
                    )
                    THEN 'succeeded'
                    ELSE 'pending'
                END,
                0
            FROM transcription_utterances AS u
            ON CONFLICT (utterance_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_stt_jobs_state_next_attempt", table_name="stt_jobs")
    op.drop_index("ix_stt_jobs_session_state", table_name="stt_jobs")
    op.drop_table("stt_jobs")
