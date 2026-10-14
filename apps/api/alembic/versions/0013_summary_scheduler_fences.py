"""Summary scheduler fences, request reservations, and permit quarantine.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mi_summary_session_states",
        sa.Column(
            "highest_processed_publication",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("frozen_artifact_publication", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column(
            "work_intent",
            sa.String(length=16),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("blocked_category", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("desired_config_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("processed_config_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("processed_source_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_mi_summary_processed_nonneg",
        "mi_summary_session_states",
        "highest_processed_publication >= 0",
    )
    op.create_check_constraint(
        "ck_mi_summary_work_intent",
        "mi_summary_session_states",
        "work_intent IN ('open','cancelled','blocked')",
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column("publication_boundary", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column("config_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column("attempt_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column(
            "explicit_retry",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_mi_summary_dispatch_failures",
        "mi_summary_dispatches",
        "failure_count >= 0",
    )
    op.create_index(
        "uq_mi_summary_dispatch_open_session",
        "mi_summary_dispatches",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )
    op.create_table(
        "mi_summary_request_reservations",
        sa.Column("client_request_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), nullable=False),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dispatch_id"], ["mi_summary_dispatches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("client_request_id"),
    )
    op.create_index(
        "ix_mi_summary_request_dispatch",
        "mi_summary_request_reservations",
        ["dispatch_id"],
        unique=False,
    )
    op.add_column(
        "mi_host_invocation_lease",
        sa.Column("holder_start_ticks", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "mi_host_invocation_lease",
        sa.Column(
            "holder_state",
            sa.String(length=16),
            server_default=sa.text("'free'"),
            nullable=False,
        ),
    )
    op.add_column(
        "mi_host_invocation_lease",
        sa.Column("holder_generation", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "mi_host_invocation_lease",
        sa.Column(
            "owned_runtime_json",
            sa.String(length=4096),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.add_column(
        "mi_host_invocation_lease",
        sa.Column(
            "termination_acked",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "mi_host_invocation_lease",
        sa.Column("quarantine_reason", sa.String(length=128), nullable=True),
    )
    op.create_check_constraint(
        "ck_mi_host_lease_state",
        "mi_host_invocation_lease",
        "holder_state IN ('free','held','quarantine')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_mi_host_lease_state", "mi_host_invocation_lease", type_="check")
    op.drop_column("mi_host_invocation_lease", "quarantine_reason")
    op.drop_column("mi_host_invocation_lease", "termination_acked")
    op.drop_column("mi_host_invocation_lease", "owned_runtime_json")
    op.drop_column("mi_host_invocation_lease", "holder_generation")
    op.drop_column("mi_host_invocation_lease", "holder_state")
    op.drop_column("mi_host_invocation_lease", "holder_start_ticks")
    op.drop_index("ix_mi_summary_request_dispatch", table_name="mi_summary_request_reservations")
    op.drop_table("mi_summary_request_reservations")
    op.drop_index(
        "uq_mi_summary_dispatch_open_session",
        table_name="mi_summary_dispatches",
    )
    op.drop_constraint(
        "ck_mi_summary_dispatch_failures",
        "mi_summary_dispatches",
        type_="check",
    )
    op.drop_column("mi_summary_dispatches", "explicit_retry")
    op.drop_column("mi_summary_dispatches", "attempt_generation")
    op.drop_column("mi_summary_dispatches", "attempt_id")
    op.drop_column("mi_summary_dispatches", "config_fingerprint")
    op.drop_column("mi_summary_dispatches", "publication_boundary")
    op.drop_column("mi_summary_dispatches", "payload_fingerprint")
    op.drop_column("mi_summary_dispatches", "failure_count")
    op.drop_constraint(
        "ck_mi_summary_work_intent",
        "mi_summary_session_states",
        type_="check",
    )
    op.drop_constraint(
        "ck_mi_summary_processed_nonneg",
        "mi_summary_session_states",
        type_="check",
    )
    op.drop_column("mi_summary_session_states", "processed_source_fingerprint")
    op.drop_column("mi_summary_session_states", "processed_config_fingerprint")
    op.drop_column("mi_summary_session_states", "desired_config_fingerprint")
    op.drop_column("mi_summary_session_states", "blocked_category")
    op.drop_column("mi_summary_session_states", "work_intent")
    op.drop_column("mi_summary_session_states", "frozen_artifact_publication")
    op.drop_column("mi_summary_session_states", "highest_processed_publication")
