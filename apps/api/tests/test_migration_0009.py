from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from recantor.db import get_engine
from recantor.media_spec import SEGMENTATION_PARAMS_JSON


def _load_migration(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / name
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(connection, module: ModuleType, direction: str) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(module, direction)()


def _row_mappings(connection, statement, params: dict[str, object] | None = None):
    return [dict(row) for row in connection.execute(statement, params or {}).mappings().all()]


@pytest.mark.asyncio
async def test_0009_backfill_and_down_up_preserve_0008_scheduler_and_canonical_truth():
    migration_0008 = _load_migration("0008_upload_foundation.py")
    migration_0009 = _load_migration("0009_upload_processing.py")
    schema = f"migration_0009_{uuid4().hex}"

    completed_session = uuid4()
    incomplete_session = uuid4()
    succeeded_job = uuid4()
    no_speech_job = uuid4()
    transcript_id = uuid4()
    now = datetime.now(UTC)

    def proof(connection) -> None:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))
        connection.execute(
            sa.text(
                """
                CREATE TABLE recording_sessions (
                    id UUID PRIMARY KEY
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                CREATE TABLE stt_jobs (
                    utterance_id UUID PRIMARY KEY,
                    state VARCHAR(32) NOT NULL DEFAULT 'pending',
                    CONSTRAINT ck_stt_job_state
                        CHECK (state IN ('pending','claimed','retry_wait','succeeded','failed'))
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                CREATE TABLE transcript_segments (
                    id UUID PRIMARY KEY,
                    session_id UUID NOT NULL,
                    producer_key VARCHAR(160) NOT NULL,
                    text TEXT NOT NULL
                )
                """
            )
        )
        _run_migration(connection, migration_0008, "upgrade")

        connection.execute(
            sa.text("INSERT INTO recording_sessions (id) VALUES (:completed), (:incomplete)"),
            {"completed": completed_session, "incomplete": incomplete_session},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO upload_records (
                    session_id,
                    original_filename,
                    content_type,
                    declared_byte_length,
                    received_bytes,
                    storage_key,
                    sha256,
                    byte_length,
                    expires_at,
                    completed_at
                ) VALUES (
                    :session_id,
                    'completed.wav',
                    'audio/wav',
                    321,
                    321,
                    'uploads/tus/completed',
                    :sha256,
                    321,
                    :expires_at,
                    :completed_at
                )
                """
            ),
            {
                "session_id": completed_session,
                "sha256": "a" * 64,
                "expires_at": now + timedelta(hours=1),
                "completed_at": now,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO upload_records (
                    session_id,
                    original_filename,
                    content_type,
                    declared_byte_length,
                    received_bytes,
                    expires_at
                ) VALUES (
                    :session_id,
                    'incomplete.wav',
                    'audio/wav',
                    999,
                    100,
                    :expires_at
                )
                """
            ),
            {"session_id": incomplete_session, "expires_at": now + timedelta(hours=1)},
        )
        connection.execute(
            sa.text("INSERT INTO stt_jobs (utterance_id, state) VALUES (:job, 'succeeded')"),
            {"job": succeeded_job},
        )
        connection.execute(
            sa.text("INSERT INTO stt_jobs (utterance_id, state) VALUES (:job, 'pending')"),
            {"job": no_speech_job},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO transcript_segments (id, session_id, producer_key, text)
                VALUES (:id, :session_id, 'utterance:already-canonical', 'canonical truth')
                """
            ),
            {"id": transcript_id, "session_id": completed_session},
        )

        _run_migration(connection, migration_0009, "upgrade")
        first_backfill = _row_mappings(
            connection,
            sa.text(
                """
                SELECT
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
                FROM upload_media_processing
                ORDER BY session_id
                """
            ),
        )
        assert len(first_backfill) == 1
        row = first_backfill[0]
        assert row["session_id"] == completed_session
        assert row["source_storage_key"] == "uploads/tus/completed"
        assert row["source_sha256"] == "a" * 64
        assert row["source_byte_length"] == 321
        assert row["source_completed_at"] == now
        assert row["state"] == "pending"
        assert row["attempt_count"] == 0
        assert row["normalization_spec_id"] == "upload-pcm16k-mono-s16le-v1"
        assert row["segmentation_spec_id"] == "upload-energy-vad-180s-v1"
        assert row["segmentation_params_json"] == SEGMENTATION_PARAMS_JSON
        assert incomplete_session not in {item["session_id"] for item in first_backfill}

        # 0009 admits no_speech as scheduler evidence only. It must not synthesize canonical text.
        connection.execute(
            sa.text("UPDATE stt_jobs SET state = 'no_speech' WHERE utterance_id = :job"),
            {"job": no_speech_job},
        )
        before_down = _row_mappings(
            connection,
            sa.text("SELECT utterance_id, state FROM stt_jobs ORDER BY utterance_id"),
        )
        assert {item["utterance_id"]: item["state"] for item in before_down} == {
            succeeded_job: "succeeded",
            no_speech_job: "no_speech",
        }
        canonical_before = _row_mappings(
            connection,
            sa.text("SELECT id, text FROM transcript_segments"),
        )
        assert canonical_before == [{"id": transcript_id, "text": "canonical truth"}]

        _run_migration(connection, migration_0009, "downgrade")
        after_down = _row_mappings(
            connection,
            sa.text("SELECT utterance_id, state FROM stt_jobs ORDER BY utterance_id"),
        )
        assert {item["utterance_id"]: item["state"] for item in after_down} == {
            succeeded_job: "succeeded",
            no_speech_job: "pending",
        }
        canonical_after_down = _row_mappings(
            connection,
            sa.text("SELECT id, text FROM transcript_segments"),
        )
        assert canonical_after_down == canonical_before

        _run_migration(connection, migration_0009, "upgrade")
        second_backfill = _row_mappings(
            connection,
            sa.text(
                """
                SELECT
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
                FROM upload_media_processing
                ORDER BY session_id
                """
            ),
        )
        assert second_backfill == first_backfill
        final_jobs = _row_mappings(
            connection,
            sa.text("SELECT utterance_id, state FROM stt_jobs ORDER BY utterance_id"),
        )
        assert {item["utterance_id"]: item["state"] for item in final_jobs} == {
            succeeded_job: "succeeded",
            no_speech_job: "pending",
        }
        canonical_after_up = _row_mappings(
            connection,
            sa.text("SELECT id, text FROM transcript_segments"),
        )
        assert canonical_after_up == canonical_before

        connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))

    async with get_engine().begin() as connection:
        await connection.run_sync(proof)
