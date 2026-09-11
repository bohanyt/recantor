from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

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
                    SELECT utterance_id, session_id, state
                    FROM stt_jobs
                    ORDER BY utterance_id
                    """
                    )
                )
                .mappings()
                .all()
            )
            constraints = set(
                connection.execute(
                    text(
                        """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conname IN (
                            'fk_stt_job_utterance_session',
                            'uq_transcription_utterance_id_session'
                        )
                        """
                    )
                ).scalars()
            )

        states = {row["utterance_id"]: row["state"] for row in rows}
        assert len(rows) == 2
        assert all(row["session_id"] == session_id for row in rows)
        assert states[canonical_id] == "succeeded"
        assert states[pending_id] == "pending"
        assert constraints == {
            "fk_stt_job_utterance_session",
            "uq_transcription_utterance_id_session",
        }
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
