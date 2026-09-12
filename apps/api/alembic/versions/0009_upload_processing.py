"""Add durable uploaded-media processing and no-speech STT terminal state.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEGMENTATION_PARAMS_JSON = (
    '{"absolute_threshold_dbfs":-50.0,"frame_ms":20,"frame_samples":320,'
    '"hard_max_ms":180000,"initial_noise_dbfs":-65.0,"min_voiced_ms":160,'
    '"noise_alpha":0.95,"noise_margin_db":12.0,"pre_roll_ms":200,'
    '"sample_rate":16000,"trailing_silence_ms":600}'
)


def upgrade() -> None:
    op.drop_constraint("ck_stt_job_state", "stt_jobs", type_="check")
    op.create_check_constraint(
        "ck_stt_job_state",
        "stt_jobs",
        "state IN ('pending', 'claimed', 'retry_wait', 'succeeded', 'no_speech', 'failed')",
    )

    op.create_table(
        "upload_media_processing",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("source_storage_key", sa.String(length=512), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_byte_length", sa.BigInteger(), nullable=False),
        sa.Column("source_completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selected_audio_stream", sa.Integer(), nullable=True),
        sa.Column("normalization_spec_id", sa.String(length=96), nullable=False),
        sa.Column("normalized_storage_key", sa.String(length=512), nullable=True),
        sa.Column("normalized_sha256", sa.String(length=64), nullable=True),
        sa.Column("normalized_byte_length", sa.BigInteger(), nullable=True),
        sa.Column("normalized_total_samples", sa.BigInteger(), nullable=True),
        sa.Column("segmentation_spec_id", sa.String(length=96), nullable=False),
        sa.Column("segmentation_params_json", sa.Text(), nullable=False),
        sa.Column("expected_utterance_count", sa.Integer(), nullable=True),
        sa.Column("outcome_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_category", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=96), nullable=True),
        sa.Column("last_error_message", sa.String(length=512), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "state IN ('pending','claimed','retry_wait','waiting_stt','succeeded','failed')",
            name="ck_upload_processing_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_upload_processing_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "(state = 'claimed' AND claim_token IS NOT NULL "
            "AND length(btrim(claim_token)) > 0 AND claim_expires_at IS NOT NULL) "
            "OR (state <> 'claimed' AND claim_token IS NULL AND claim_expires_at IS NULL)",
            name="ck_upload_processing_claim_shape",
        ),
        sa.CheckConstraint(
            "(state = 'retry_wait' AND next_attempt_at IS NOT NULL) "
            "OR (state <> 'retry_wait' AND next_attempt_at IS NULL)",
            name="ck_upload_processing_retry_shape",
        ),
        sa.CheckConstraint(
            "source_byte_length > 0",
            name="ck_upload_processing_source_bytes_positive",
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_upload_processing_source_sha",
        ),
        sa.CheckConstraint(
            "expected_utterance_count IS NULL OR expected_utterance_count >= 0",
            name="ck_upload_processing_expected_count_nonnegative",
        ),
        sa.CheckConstraint(
            "normalized_total_samples IS NULL OR normalized_total_samples >= 0",
            name="ck_upload_processing_samples_nonnegative",
        ),
        sa.CheckConstraint(
            "(normalized_storage_key IS NULL AND normalized_sha256 IS NULL "
            "AND normalized_byte_length IS NULL AND normalized_total_samples IS NULL) OR "
            "(normalized_storage_key IS NOT NULL AND length(btrim(normalized_storage_key)) > 0 "
            "AND normalized_sha256 ~ '^[0-9a-f]{64}$' AND normalized_byte_length > 0 "
            "AND normalized_total_samples >= 0)",
            name="ck_upload_processing_normalized_shape",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["upload_records.session_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "ix_upload_processing_state_retry",
        "upload_media_processing",
        ["state", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_upload_processing_claim_expiry",
        "upload_media_processing",
        ["state", "claim_expires_at"],
        unique=False,
    )

    # #44 completion evidence is immutable. Existing completed uploads enter exactly one
    # PostgreSQL-backed processing identity with the frozen D3-A snapshot.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO upload_media_processing (
                session_id,
                source_storage_key,
                source_sha256,
                source_byte_length,
                source_completed_at,
                state,
                attempt_count,
                normalization_spec_id,
                segmentation_spec_id,
                segmentation_params_json
            )
            SELECT
                session_id,
                storage_key,
                sha256,
                byte_length,
                completed_at,
                'pending',
                0,
                'upload-pcm16k-mono-s16le-v1',
                'upload-energy-vad-180s-v1',
                :segmentation_params
            FROM upload_records
            WHERE completed_at IS NOT NULL
            ON CONFLICT (session_id) DO NOTHING
            """
        ),
        {"segmentation_params": _SEGMENTATION_PARAMS_JSON},
    )


def downgrade() -> None:
    op.drop_index(
        "ix_upload_processing_claim_expiry",
        table_name="upload_media_processing",
    )
    op.drop_index(
        "ix_upload_processing_state_retry",
        table_name="upload_media_processing",
    )
    op.drop_table("upload_media_processing")

    # The historical vocabulary cannot represent no_speech. Downgrade makes those jobs
    # pending so the old schema remains valid rather than fabricating TranscriptSegment rows.
    op.execute("UPDATE stt_jobs SET state = 'pending' WHERE state = 'no_speech'")
    op.drop_constraint("ck_stt_job_state", "stt_jobs", type_="check")
    op.create_check_constraint(
        "ck_stt_job_state",
        "stt_jobs",
        "state IN ('pending', 'claimed', 'retry_wait', 'succeeded', 'failed')",
    )
