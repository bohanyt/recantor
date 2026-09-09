"""Add hashed recovery capability for capture-generation takeover.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column("recovery_token_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recording_sessions", "recovery_token_hash")
