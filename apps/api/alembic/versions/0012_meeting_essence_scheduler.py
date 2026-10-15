"""Meeting essence scheduler tables and host invocation lease (#73).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mi_summary_session_states",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column(
            "observed_publication_high_water", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("debounce_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_publication_boundary", sa.Integer(), nullable=True),
        sa.Column(
            "intelligence_semantically_complete",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "observed_publication_high_water >= 0",
            name="ck_mi_summary_high_water_nonneg",
        ),
        sa.CheckConstraint(
            "finalized_publication_boundary IS NULL OR finalized_publication_boundary >= 0",
            name="ck_mi_summary_final_boundary_nonneg",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "mi_summary_dispatches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("client_request_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="'pending'", nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_category", sa.String(length=32), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.String(length=512), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('pending','claimed','completed','cancelled','superseded')",
            name="ck_mi_summary_dispatch_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_mi_summary_dispatch_attempts"),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_request_id", name="uq_mi_summary_dispatch_client_request"),
    )
    op.create_index(
        "ix_mi_summary_dispatch_pending",
        "mi_summary_dispatches",
        ["status", "next_attempt_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_mi_summary_dispatch_session",
        "mi_summary_dispatches",
        ["session_id"],
        unique=False,
    )
    op.create_table(
        "mi_host_invocation_lease",
        sa.Column("id", sa.Integer(), server_default="1", nullable=False),
        sa.Column("holder_token", sa.String(length=64), nullable=True),
        sa.Column("holder_pid", sa.Integer(), nullable=True),
        sa.Column("holder_pid_created_ms", sa.BigInteger(), nullable=True),
        sa.Column("holder_entrypoint", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_invocations", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_mi_host_lease_singleton"),
        sa.CheckConstraint(
            "active_invocations >= 0 AND active_invocations <= 1",
            name="ck_mi_host_active_invocations",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text("INSERT INTO mi_host_invocation_lease (id, active_invocations) VALUES (1, 0)")
    )


def downgrade() -> None:
    op.drop_table("mi_host_invocation_lease")
    op.drop_index("ix_mi_summary_dispatch_session", table_name="mi_summary_dispatches")
    op.drop_index("ix_mi_summary_dispatch_pending", table_name="mi_summary_dispatches")
    op.drop_table("mi_summary_dispatches")
    op.drop_table("mi_summary_session_states")
