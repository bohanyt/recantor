"""Separate audio and transcript freezes, job metrics, and capacity holds.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mi_summary_session_states",
        sa.Column("blocked_disposition", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("blocked_source_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("blocked_config_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("audio_final_sequence", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("frozen_transcript_sequence", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("frozen_transcript_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("frozen_transcript_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column(
            "capacity_deferred",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE mi_summary_metrics SET source_fingerprint = '' WHERE source_fingerprint IS NULL"
    )
    op.execute(
        "UPDATE mi_summary_metrics SET config_fingerprint = '' WHERE config_fingerprint IS NULL"
    )
    op.alter_column(
        "mi_summary_metrics",
        "source_fingerprint",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default=sa.text("''"),
    )
    op.alter_column(
        "mi_summary_metrics",
        "config_fingerprint",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default=sa.text("''"),
    )
    op.add_column(
        "mi_summary_metrics",
        sa.Column(
            "observed_event_keys",
            sa.Text(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.drop_constraint("mi_summary_metrics_pkey", "mi_summary_metrics", type_="primary")
    op.create_primary_key(
        "mi_summary_metrics_pkey",
        "mi_summary_metrics",
        ["session_id", "source_fingerprint", "config_fingerprint"],
    )


def downgrade() -> None:
    op.drop_constraint("mi_summary_metrics_pkey", "mi_summary_metrics", type_="primary")
    op.create_primary_key("mi_summary_metrics_pkey", "mi_summary_metrics", ["session_id"])
    op.drop_column("mi_summary_metrics", "observed_event_keys")
    op.alter_column(
        "mi_summary_metrics",
        "config_fingerprint",
        existing_type=sa.String(length=64),
        nullable=True,
        server_default=None,
    )
    op.alter_column(
        "mi_summary_metrics",
        "source_fingerprint",
        existing_type=sa.String(length=64),
        nullable=True,
        server_default=None,
    )
    op.drop_column("mi_summary_dispatches", "capacity_deferred")
    for name in (
        "frozen_transcript_hash",
        "frozen_transcript_version",
        "frozen_transcript_sequence",
        "audio_final_sequence",
        "blocked_config_fingerprint",
        "blocked_source_fingerprint",
        "blocked_disposition",
    ):
        op.drop_column("mi_summary_session_states", name)
