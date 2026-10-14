"""Fair summary pagination, material intent, and host-authority columns.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mi_summary_session_states",
        sa.Column("desired_source_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("desired_coverage_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("processed_coverage_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("desired_stt_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mi_summary_session_states",
        sa.Column("processed_stt_fingerprint", sa.String(length=64), nullable=True),
    )
    for name in (
        "desired_config_epoch",
        "processed_config_epoch",
        "finalize_epoch",
        "processed_finalize_epoch",
        "material_epoch",
    ):
        op.add_column(
            "mi_summary_session_states",
            sa.Column(name, sa.Integer(), server_default="0", nullable=False),
        )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column("intent_generation", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column(
            "disposition",
            sa.String(length=32),
            server_default=sa.text("'runnable'"),
            nullable=False,
        ),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column("progress_token", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column("stall_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "mi_summary_dispatches",
        sa.Column("parked_material_epoch", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_mi_summary_dispatch_disposition",
        "mi_summary_dispatches",
        "disposition IN ('runnable','awaiting_material','terminal_partial')",
    )
    op.create_index(
        "uq_mi_summary_dispatch_one_open",
        "mi_summary_dispatches",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )
    op.add_column(
        "mi_host_invocation_lease",
        sa.Column(
            "invocation_admitted",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "mi_host_invocation_lease",
        sa.Column(
            "spawns_process_tree",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_table(
        "mi_summary_scan_cursor",
        sa.Column("id", sa.Integer(), server_default="1", nullable=False),
        sa.Column("canonical_after", sa.Uuid(), nullable=True),
        sa.Column("state_after", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_mi_summary_scan_cursor_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "mi_summary_metrics",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("original_queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_useful_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_result_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_hits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("observed_tokens", sa.Integer(), nullable=True),
        sa.Column("quantum_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("extract_ms", sa.Float(), server_default="0", nullable=False),
        sa.Column("reconcile_ms", sa.Float(), server_default="0", nullable=False),
        sa.Column("check_ms", sa.Float(), server_default="0", nullable=False),
        sa.Column("render_ms", sa.Float(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )


def downgrade() -> None:
    op.drop_table("mi_summary_metrics")
    op.drop_table("mi_summary_scan_cursor")
    op.drop_column("mi_host_invocation_lease", "spawns_process_tree")
    op.drop_column("mi_host_invocation_lease", "invocation_admitted")
    op.drop_index("uq_mi_summary_dispatch_one_open", table_name="mi_summary_dispatches")
    op.drop_constraint("ck_mi_summary_dispatch_disposition", "mi_summary_dispatches", type_="check")
    op.drop_column("mi_summary_dispatches", "parked_material_epoch")
    op.drop_column("mi_summary_dispatches", "stall_count")
    op.drop_column("mi_summary_dispatches", "progress_token")
    op.drop_column("mi_summary_dispatches", "disposition")
    op.drop_column("mi_summary_dispatches", "intent_generation")
    for name in (
        "material_epoch",
        "processed_finalize_epoch",
        "finalize_epoch",
        "processed_config_epoch",
        "desired_config_epoch",
        "processed_stt_fingerprint",
        "desired_stt_fingerprint",
        "processed_coverage_fingerprint",
        "desired_coverage_fingerprint",
        "desired_source_fingerprint",
    ):
        op.drop_column("mi_summary_session_states", name)
