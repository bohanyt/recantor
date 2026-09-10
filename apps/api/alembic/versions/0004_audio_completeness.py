"""Persist terminal audio completeness for downstream consumers.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column("audio_completeness", sa.String(length=32), nullable=True),
    )

    # Existing COMPLETE rows predate the persisted classification. Reconcile them
    # once during migration so every terminal session resource is immediately
    # useful to downstream consumers. Runtime finalization persists the same
    # vocabulary transactionally and never derives it on read.
    op.execute(
        sa.text(
            """
            UPDATE recording_sessions AS s
            SET audio_completeness = CASE
                WHEN s.final_sequence = 0 THEN 'empty'
                WHEN s.final_sequence > 0 AND EXISTS (
                    SELECT 1
                    FROM recording_gaps AS g
                    WHERE g.session_id = s.id
                      AND g.sequence_start IS NOT NULL
                      AND g.sequence_end IS NOT NULL
                      AND g.sequence_end >= 1
                      AND g.sequence_start <= s.final_sequence
                      AND EXISTS (
                          SELECT 1
                          FROM generate_series(
                              GREATEST(g.sequence_start, 1),
                              LEAST(g.sequence_end, s.final_sequence)
                          ) AS seq(value)
                          WHERE NOT EXISTS (
                              SELECT 1
                              FROM recording_chunks AS c
                              WHERE c.session_id = s.id
                                AND c.sequence = seq.value
                          )
                      )
                ) THEN 'partial'
                WHEN s.final_sequence > 0 THEN 'full'
                ELSE NULL
            END
            WHERE s.state = 'complete'
            """
        )
    )


def downgrade() -> None:
    op.drop_column("recording_sessions", "audio_completeness")
