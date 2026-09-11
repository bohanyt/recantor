from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from alembic import command
from recantor.settings import get_settings


def test_migration_0007_backfills_existing_utterances(monkeypatch) -> None:
    api_root = Path(__file__).resolve().parents[1]
    source_url = make_url(get_settings().database_url)
    database_name = f"recantor_backfill_{uuid4().hex[:12]}"
    admin_url = source_url.set(database="postgres").render_as_string(hide_password=False)
    migration_url = source_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    migration_engine = None
    try:
        monkeypatch.setenv("DATABASE_URL", migration_url)
        get_settings.cache_clear()
        config = Config(str(api_root / "alembic.ini"))
        command.upgrade(config, "0006")

        migration_engine = create_engine(migration_url)
        session_id = uuid4()
        canonical_id = uuid4()
        pending_id = uuid4()
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO recording_sessions
                        (id, client_request_id, kind, state, capture_epoch)
                    VALUES
                        (:id, :client_request_id, 'live', 'recording', 1)
                    """
                ),
                {"id": session_id, "client_request_id": uuid4()},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO transcription_utterances
                        (id, session_id, sequence, producer_key, start_ms, end_ms,
                         content_type, sha256, byte_length, storage_key)
                    VALUES
                        (:canonical_id, :session_id, 1, 'historical:canonical', 0, 900,
                         'audio/wav', :sha, 10, 'historical/canonical.wav'),
                        (:pending_id, :session_id, 2, 'historical:pending', 1000, 1900,
                         'audio/wav', :sha, 10, 'historical/pending.wav')
                    """
                ),
                {
                    "canonical_id": canonical_id,
                    "pending_id": pending_id,
                    "session_id": session_id,
                    "sha": "a" * 64,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO transcript_segments
                        (id, session_id, sequence, producer_key, start_ms, end_ms, text, language)
                    VALUES
                        (:id, :session_id, 1, :producer_key, 0, 900,
                         'historical canonical transcript', 'id')
                    """
                ),
                {
                    "id": uuid4(),
                    "session_id": session_id,
                    "producer_key": f"utterance:{canonical_id}",
                },
            )

        command.upgrade(config, "0007")

        with migration_engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT utterance_id, session_id, state, attempt_count
                        FROM stt_jobs
                        ORDER BY utterance_id
                        """
                    )
                )
                .mappings()
                .all()
            )
            constraint_defs = dict(
                connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid)
                        FROM pg_constraint
                        WHERE conname IN (
                            'fk_stt_job_utterance_session',
                            'uq_transcription_utterance_id_session',
                            'ck_stt_job_claim_shape',
                            'ck_stt_job_retry_time_shape'
                        )
                        """
                    )
                ).all()
            )
            defaults = dict(
                connection.execute(
                    text(
                        """
                        SELECT column_name, column_default
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'stt_jobs'
                          AND column_name IN ('state', 'attempt_count')
                        """
                    )
                ).all()
            )

        states = {row["utterance_id"]: row["state"] for row in rows}
        assert len(rows) == 2
        assert all(row["session_id"] == session_id for row in rows)
        assert all(row["attempt_count"] == 0 for row in rows)
        assert states[canonical_id] == "succeeded"
        assert states[pending_id] == "pending"
        assert set(constraint_defs) == {
            "fk_stt_job_utterance_session",
            "uq_transcription_utterance_id_session",
            "ck_stt_job_claim_shape",
            "ck_stt_job_retry_time_shape",
        }
        assert "btrim" in constraint_defs["ck_stt_job_claim_shape"].lower()
        assert "next_attempt_at" in constraint_defs["ck_stt_job_retry_time_shape"]
        assert "pending" in (defaults["state"] or "")
        assert (defaults["attempt_count"] or "").startswith("0")

        # Exercise the migrated composite identity constraint itself, not only its name.
        # The probe utterance intentionally has no STTJob yet so a mismatched insert cannot
        # fail earlier on the STTJob primary key.
        other_session_id = uuid4()
        fk_probe_id = uuid4()
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO recording_sessions
                        (id, client_request_id, kind, state, capture_epoch)
                    VALUES
                        (:id, :client_request_id, 'live', 'recording', 1)
                    """
                ),
                {"id": other_session_id, "client_request_id": uuid4()},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO transcription_utterances
                        (id, session_id, sequence, producer_key, start_ms, end_ms,
                         content_type, sha256, byte_length, storage_key)
                    VALUES
                        (:id, :session_id, 3, 'historical:fk-probe', 2000, 2900,
                         'audio/wav', :sha, 10, 'historical/fk-probe.wav')
                    """
                ),
                {"id": fk_probe_id, "session_id": session_id, "sha": "b" * 64},
            )
            with pytest.raises(IntegrityError) as mismatch:
                with connection.begin_nested():
                    connection.execute(
                        text(
                            """
                            INSERT INTO stt_jobs (utterance_id, session_id)
                            VALUES (:utterance_id, :session_id)
                            """
                        ),
                        {"utterance_id": fk_probe_id, "session_id": other_session_id},
                    )
            assert mismatch.value.orig.diag.constraint_name == "fk_stt_job_utterance_session"

            # Retry deadlines are legal iff the state is retry_wait.
            with pytest.raises(IntegrityError) as retry_shape:
                with connection.begin_nested():
                    connection.execute(
                        text(
                            """
                            UPDATE stt_jobs
                            SET next_attempt_at = clock_timestamp()
                            WHERE utterance_id = :utterance_id
                            """
                        ),
                        {"utterance_id": pending_id},
                    )
            assert retry_shape.value.orig.diag.constraint_name == "ck_stt_job_retry_time_shape"
    finally:
        if migration_engine is not None:
            migration_engine.dispose()
        get_settings.cache_clear()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()
