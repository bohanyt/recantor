from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest
import pytest_asyncio
from alembic.config import Config
from meeting_intelligence_fixtures import (
    FIXTURE_SESSION_ID,
    SEGMENT_SPECS,
    build_s01_s06_bundle,
    build_s01_s06_manifest,
)
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine.url import make_url

from alembic import command
from recantor.db import get_engine, get_sessionmaker
from recantor.meeting_essence.budget import EngineLimits
from recantor.meeting_essence.checkpoint_binding import CHECKPOINT_BINDING_VERSION
from recantor.meeting_essence.contracts import (
    CheckStatus,
    DeadlineNormalizationBasis,
    EvidenceReference,
    ManifestSegmentEntry,
    ProcessingJobStatus,
    SourceCoverageFlags,
    SourceSnapshotManifest,
    StageOperation,
)
from recantor.meeting_essence.deterministic_provider import (
    STRUCTURAL_CHECK_METHOD,
    DeterministicMeetingEssenceProvider,
)
from recantor.meeting_essence.engine import EngineRunConfig
from recantor.meeting_essence.identity import (
    compute_input_fingerprint,
    finalize_snapshot_manifest,
    segment_content_hash,
)
from recantor.meeting_essence.persistence.checkpoint_store import (
    CheckpointSerializationError,
    deserialize_checkpoint,
    serialize_checkpoint,
)
from recantor.meeting_essence.persistence.models import (
    MIArtifactVersion,
    MIAtomicRecordRow,
    MIJobStatus,
    MIValidatedExtraction,
)
from recantor.meeting_essence.persistence.repository import (
    AttemptBusyError,
    AttemptFenceError,
    CheckpointRegressionError,
    EvidenceOwnershipError,
    IdempotencyConflictError,
    MeetingIntelligenceRepository,
    MeetingIntelligenceRepositoryError,
    PublicationIntegrityError,
    SessionIsolationError,
    SnapshotImmutableError,
    StaleGenerationError,
)
from recantor.meeting_essence.persistence.service import MeetingIntelligenceLedgerService
from recantor.meeting_essence.progress import EngineCheckpoint
from recantor.meeting_essence.source_reader import (
    CanonicalSegmentInput,
    InMemoryCanonicalSourceReader,
)
from recantor.models import RecordingSession, SessionKind, SessionState, TranscriptSegment
from recantor.settings import get_settings

API_ROOT = Path(__file__).resolve().parents[1]
WITNESS = Path(__file__).resolve().parent / "persistence_restart_witness.py"
SESSION_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail("DATABASE_URL is required; this file has no hardcoded database default")
    return url


def _cluster_owned() -> bool:
    return os.environ.get("RECANTOR_PG_OWNED") == "1"


def _pg_data() -> Path:
    override = os.environ.get("RECANTOR_PG_DATA")
    if not override:
        pytest.fail("RECANTOR_PG_DATA is required when RECANTOR_PG_OWNED=1")
    return Path(override)


def _pg_port() -> str:
    port = os.environ.get("RECANTOR_PG_PORT")
    if not port:
        pytest.fail("RECANTOR_PG_PORT is required when RECANTOR_PG_OWNED=1")
    return port


def _pg_executable(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    root = os.environ.get("RECANTOR_PG_BIN")
    if root:
        candidate = Path(root) / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    found = shutil.which(f"{name}{suffix}") or shutil.which(name)
    if found is None:
        pytest.fail(f"cannot find {name}; set RECANTOR_PG_BIN")
    return Path(found)


def _pg_log_path() -> Path:
    override = os.environ.get("RECANTOR_PG_LOG")
    if override:
        return Path(override)
    return _pg_data().parent / "postgresql-owned.log"


def _pg_socket_dir() -> Path | None:
    if os.name == "nt":
        return None
    override = os.environ.get("RECANTOR_PG_SOCKET_DIR")
    path = Path(override) if override else _pg_data().parent / "pg-socket"
    if " " in str(path):
        pytest.fail(f"PostgreSQL socket directory must not contain spaces: {path}")
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _postgres_start_options() -> str:
    options = (
        f"-h 127.0.0.1 -p {_pg_port()} -c shared_buffers=32MB "
        "-c max_connections=16 -c max_worker_processes=2"
    )
    socket_dir = _pg_socket_dir()
    if socket_dir is not None:
        options = f"{options} -k {socket_dir}"
    return options


def _read_server_log(path: Path) -> str:
    if not path.is_file():
        return f"server log was not created at {path}"
    data = path.read_bytes()
    if not data:
        return f"server log at {path} is empty"
    text_value = data.decode("utf-8", errors="replace")
    if len(text_value) > 16000:
        return text_value[-16000:]
    return text_value


def _pg_ctl(*args: str) -> subprocess.CompletedProcess[str]:
    # Do not capture stdout/stderr. pg_ctl start detaches postgres, and an inherited
    # pipe keeps the parent waiting after pg_ctl itself has exited.
    return subprocess.run(
        [str(_pg_executable("pg_ctl")), "-D", str(_pg_data()), *args],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=90,
    )


def _pg_is_running() -> bool:
    return _pg_ctl("status").returncode == 0


def _start_pg() -> None:
    if not _cluster_owned():
        return
    if _pg_is_running():
        return
    log_path = _pg_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = _pg_ctl(
        "-l",
        str(log_path),
        "-o",
        _postgres_start_options(),
        "-w",
        "-t",
        "60",
        "start",
    )
    if result.returncode != 0 or not _pg_is_running():
        pytest.fail(
            "failed to start the disposable PostgreSQL cluster this fixture owns\n"
            f"pg_ctl exit {result.returncode}\n"
            f"server log:\n{_read_server_log(log_path)}"
        )


def _release_database_caches() -> None:
    if get_engine.cache_info().currsize:
        get_engine().sync_engine.dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    get_settings.cache_clear()


async def _release_database_caches_async() -> None:
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    get_settings.cache_clear()


def _stop_pg() -> None:
    if not _cluster_owned():
        return
    if _pg_is_running():
        _pg_ctl("stop", "-m", "fast")


def _require_owned_cluster() -> None:
    if not _cluster_owned():
        pytest.skip(
            "process and PostgreSQL restart proof runs on the disposable cluster "
            "identified by RECANTOR_PG_OWNED=1"
        )


def _alembic_config() -> Config:
    os.environ["DATABASE_URL"] = _database_url()
    get_settings.cache_clear()
    return Config(str(API_ROOT / "alembic.ini"))


@pytest.fixture(scope="module")
def synthetic_postgresql():
    """Use DATABASE_URL as-is. Start and stop PostgreSQL only when this process owns it."""
    url = _database_url()
    owned = _cluster_owned()
    if owned:
        _start_pg()
    try:
        yield url
    finally:
        _release_database_caches()
        if owned:
            _stop_pg()
            assert not _pg_is_running()


@pytest.fixture(scope="module")
def mi_db_migrated(synthetic_postgresql: str):
    os.environ["DATABASE_URL"] = synthetic_postgresql
    _release_database_caches()
    command.upgrade(_alembic_config(), "head")
    yield
    _release_database_caches()


@pytest_asyncio.fixture
async def mi_db(mi_db_migrated: None, synthetic_postgresql: str, monkeypatch: pytest.MonkeyPatch):
    del mi_db_migrated
    monkeypatch.setenv("DATABASE_URL", synthetic_postgresql)
    await _release_database_caches_async()
    try:
        async with get_sessionmaker()() as db:
            await _ensure_session(db, FIXTURE_SESSION_ID)
            await _ensure_session(db, SESSION_B)
            await _ensure_segments(db, FIXTURE_SESSION_ID, SEGMENT_SPECS)
            await _ensure_segments(db, SESSION_B, _shifted_specs())
            await db.commit()
        yield
        async with get_sessionmaker()() as db:
            repo = MeetingIntelligenceRepository(db)
            await repo.delete_session_intelligence(FIXTURE_SESSION_ID)
            await repo.delete_session_intelligence(SESSION_B)
            await db.commit()
    finally:
        await _release_database_caches_async()


async def _ensure_session(db, session_id: UUID) -> None:  # type: ignore[no-untyped-def]
    if await db.get(RecordingSession, session_id) is None:
        db.add(
            RecordingSession(
                id=session_id,
                client_request_id=uuid4(),
                kind=SessionKind.LIVE.value,
                state=SessionState.COMPLETE.value,
            )
        )


async def _ensure_segments(
    db,  # type: ignore[no-untyped-def]
    session_id: UUID,
    specs: list[tuple[str, int, int, int, str]],
) -> None:
    for segment_id, sequence, start_ms, end_ms, segment_text in specs:
        if await db.get(TranscriptSegment, UUID(segment_id)) is None:
            db.add(
                TranscriptSegment(
                    id=UUID(segment_id),
                    session_id=session_id,
                    sequence=sequence,
                    producer_key=f"synthetic-{segment_id}",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=segment_text,
                )
            )


def _reader_from_specs(
    specs: list[tuple[str, int, int, int, str]] = SEGMENT_SPECS,
) -> InMemoryCanonicalSourceReader:
    return InMemoryCanonicalSourceReader(
        segments=[
            CanonicalSegmentInput(
                id=UUID(segment_id),
                sequence=sequence,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
            for segment_id, sequence, start_ms, end_ms, text in specs
        ]
    )


def _coverage() -> SourceCoverageFlags:
    return SourceCoverageFlags(transcript_publication_complete=True)


def _shift_uuid(value: str) -> str:
    return value.replace("11111111", "12121212")


def _shifted_specs() -> list[tuple[str, int, int, int, str]]:
    return [
        (_shift_uuid(segment_id), sequence, start_ms, end_ms, text)
        for segment_id, sequence, start_ms, end_ms, text in SEGMENT_SPECS
    ]


def _bundle_for_session_b():
    bundle = build_s01_s06_bundle()

    def shift_evidence(evidence):  # type: ignore[no-untyped-def]
        return evidence.model_copy(
            update={"segment_id": UUID(_shift_uuid(str(evidence.segment_id)))}
        )

    segments = [
        segment.model_copy(update={"id": UUID(_shift_uuid(str(segment.id)))})
        for segment in bundle.manifest.segments
    ]
    manifest = finalize_snapshot_manifest(
        bundle.manifest.model_copy(
            update={
                "session_id": SESSION_B,
                "segments": segments,
                "snapshot_id": "snap_tmp0",
                "snapshot_content_hash": "0" * 64,
            }
        )
    )
    records = [
        record.model_copy(update={"evidence": [shift_evidence(item) for item in record.evidence]})
        for record in bundle.records
    ]
    relations = [
        relation.model_copy(
            update={"evidence": [shift_evidence(item) for item in relation.evidence]}
        )
        for relation in bundle.relations
    ]
    return bundle.model_copy(
        update={"manifest": manifest, "records": records, "relations": relations}
    )


_LATE_SEGMENT = (
    "11111111-1111-4111-8111-111111111107",
    7,
    110_000,
    111_000,
    "Catatan tambahan setelah freeze.",
)


async def _seed_late_segment(db) -> None:  # type: ignore[no-untyped-def]
    await _ensure_segments(db, FIXTURE_SESSION_ID, [_LATE_SEGMENT])


def _late_manifest() -> SourceSnapshotManifest:
    late_manifest = build_s01_s06_manifest()
    extra = _LATE_SEGMENT
    segments = [
        *late_manifest.segments,
        ManifestSegmentEntry(
            id=UUID(extra[0]),
            sequence=7,
            start_ms=extra[2],
            end_ms=extra[3],
            text=extra[4],
            content_hash=segment_content_hash(extra[4]),
        ),
    ]
    return finalize_snapshot_manifest(
        late_manifest.model_copy(update={"segments": segments, "publication_boundary_sequence": 7})
    )


async def _run(service: MeetingIntelligenceLedgerService, **kwargs):  # type: ignore[no-untyped-def]
    bundle = kwargs.pop("bundle", build_s01_s06_bundle())
    session_id = kwargs.pop("session_id", FIXTURE_SESSION_ID)
    reader = kwargs.pop("reader", _reader_from_specs())
    provider = kwargs.pop("provider", DeterministicMeetingEssenceProvider(gold=bundle))
    return await service.run_engine(
        session_id=session_id,
        client_request_id=kwargs.pop("client_request_id", uuid4()),
        reader=reader,
        provider=provider,
        publication_boundary_sequence=kwargs.pop("boundary", 6),
        coverage=_coverage(),
        meeting_metadata=kwargs.pop("meeting_metadata", None),
        config=kwargs.pop("config", None),
    )


async def _table_names() -> set[str]:
    async with get_engine().connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'mi_%'"
            )
        )
        return {row[0] for row in rows}


def _migration_database_url(base_url: str, database: str) -> str:
    return make_url(base_url).set(database=database).render_as_string(hide_password=False)


def _recreate_database(admin_url: str, database: str) -> None:
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": database},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
            connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_migration_0009_to_head_roundtrip(synthetic_postgresql: str) -> None:
    """Alembic up/down on a database this test creates, not the shared create_all fixture."""
    database = "recantor_ledger_migration"
    side_url = _migration_database_url(synthetic_postgresql, database)
    _recreate_database(synthetic_postgresql, database)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = side_url
    _release_database_caches()
    try:
        config = Config(str(API_ROOT / "alembic.ini"))
        command.upgrade(config, "head")
        async with get_engine().connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            names = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name LIKE 'mi_%'"
                    )
                )
            }
        assert revision == "0011"
        assert "mi_job_request_aliases" in names
        assert "mi_validated_extractions" in names
        assert "mi_stage_output_cache" not in names
        await get_engine().dispose()
        command.downgrade(config, "0009")
        async with get_engine().connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            remaining = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name LIKE 'mi_%'"
                    )
                )
            }
        assert revision == "0009"
        assert remaining == set()
        await get_engine().dispose()
        command.upgrade(config, "head")
        async with get_engine().connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "0011"
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        _release_database_caches()
        _recreate_database(synthetic_postgresql, database)
        admin = create_engine(synthetic_postgresql, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
        finally:
            admin.dispose()


def test_checkpoint_serde_rejects_unknown_and_missing_keys() -> None:
    checkpoint = EngineCheckpoint(pending_check_fingerprints={"page-1"})
    payload = json.loads(serialize_checkpoint(checkpoint))
    loaded = deserialize_checkpoint(json.dumps(payload))
    assert loaded.pending_check_fingerprints == {"page-1"}
    assert loaded.source_check_cursor == 0
    payload["not_a_field"] = 1
    with pytest.raises(CheckpointSerializationError):
        deserialize_checkpoint(json.dumps(payload))
    del payload["not_a_field"]
    del payload["acked_check_fingerprints"]
    with pytest.raises(CheckpointSerializationError):
        deserialize_checkpoint(json.dumps(payload))


@pytest.mark.asyncio
async def test_duplicate_completion_reuses_stored_identity(mi_db: None) -> None:
    del mi_db
    request_id = uuid4()
    async with get_sessionmaker()() as db:
        first = await _run(MeetingIntelligenceLedgerService(db), client_request_id=request_id)
    async with get_sessionmaker()() as db:
        second = await _run(MeetingIntelligenceLedgerService(db), client_request_id=request_id)
    assert second.job.id == first.job.id
    assert second.served_from_completed_cache
    assert second.provider_calls == 0
    assert second.extract_provider_calls == 0
    assert second.attempt.id == first.attempt.id
    assert second.attempt.id.int != 0
    assert second.result.trace.prompt_cache_key
    assert second.result.trace.schema_identity
    assert second.result.checkpoint.prompt_cache_key == second.result.trace.prompt_cache_key
    async with get_sessionmaker()() as db:
        artifacts = await db.scalar(
            select(func.count())
            .select_from(MIArtifactVersion)
            .where(MIArtifactVersion.job_id == first.job.id)
        )
        extractions = await db.scalar(
            select(func.count())
            .select_from(MIValidatedExtraction)
            .where(MIValidatedExtraction.session_id == FIXTURE_SESSION_ID)
        )
    assert artifacts == 1
    assert extractions and extractions > 0


@pytest.mark.asyncio
async def test_idempotency_key_rejects_changed_model(mi_db: None) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    request_id = uuid4()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=request_id,
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="a" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="b" * 64,
            provider_adapter_id="deterministic-v1",
            requested_model="model-a",
        )
        with pytest.raises(IdempotencyConflictError):
            await repo.create_or_get_job(
                session_id=FIXTURE_SESSION_ID,
                client_request_id=request_id,
                snapshot_id=manifest.snapshot_id,
                source_content_hash=manifest.snapshot_content_hash,
                run_fingerprint="c" * 64,
                prompt_cache_key="p" * 64,
                schema_identity="schema",
                algorithm_identity="algorithm",
                limits_fingerprint="b" * 64,
                provider_adapter_id="deterministic-v1",
                requested_model="model-b",
            )


@pytest.mark.asyncio
async def test_two_sessions_do_not_share_evidence_or_current_pointer(mi_db: None) -> None:
    del mi_db
    bundle_b = _bundle_for_session_b()
    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        outcome_a = await _run(service)
        outcome_b = await _run(
            service,
            session_id=SESSION_B,
            bundle=bundle_b,
            reader=_reader_from_specs(_shifted_specs()),
            provider=DeterministicMeetingEssenceProvider(gold=bundle_b),
        )
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        with pytest.raises(SessionIsolationError):
            await repo.get_job(outcome_a.job.id, session_id=SESSION_B)
        with pytest.raises(SessionIsolationError):
            await repo.load_artifact(outcome_a.job.current_artifact_id, session_id=SESSION_B)  # type: ignore[arg-type]
        with pytest.raises(EvidenceOwnershipError):
            await repo.resolve_record_evidence(
                session_id=SESSION_B,
                snapshot_content_hash=outcome_a.job.source_content_hash,
                record=build_s01_s06_bundle().records[0],
            )
        with pytest.raises(SessionIsolationError):
            await repo.upsert_snapshot(session_id=SESSION_B, manifest=build_s01_s06_manifest())
        published_b = await repo.load_current_publication(
            job_id=outcome_b.job.id, session_id=SESSION_B
        )
    assert outcome_a.job.current_artifact_id != outcome_b.job.current_artifact_id
    assert published_b is not None
    assert published_b.session_id == SESSION_B


@pytest.mark.asyncio
async def test_fences_reject_crossed_cancelled_and_stale_writes(mi_db: None) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        job_a = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="1" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="l" * 64,
            provider_adapter_id="deterministic-v1",
            requested_model="model-a",
        )
        job_b = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="2" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="l" * 64,
            provider_adapter_id="deterministic-v1",
            requested_model="model-b",
        )
        attempt_a = await repo.begin_attempt(job_id=job_a.id, session_id=FIXTURE_SESSION_ID)
        attempt_b = await repo.begin_attempt(job_id=job_b.id, session_id=FIXTURE_SESSION_ID)
        with pytest.raises(AttemptFenceError):
            await repo.save_checkpoint(
                attempt_id=attempt_a.id,
                job_id=job_b.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt_a.generation,
                checkpoint=EngineCheckpoint(),
            )
        await repo.cancel_attempt(
            attempt_id=attempt_a.id,
            session_id=FIXTURE_SESSION_ID,
            generation=attempt_a.generation,
        )
        with pytest.raises(AttemptFenceError):
            await repo.save_checkpoint(
                attempt_id=attempt_a.id,
                job_id=job_a.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt_a.generation,
                checkpoint=EngineCheckpoint(),
            )
        with pytest.raises(AttemptFenceError):
            await repo.finalize_attempt(
                job_id=job_a.id,
                attempt_id=attempt_a.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt_a.generation,
                processing_status=ProcessingJobStatus.SUCCEEDED,
                layered=None,
                limitations=[],
                snapshot_id=manifest.snapshot_id,
                snapshot_content_hash=manifest.snapshot_content_hash,
                run_fingerprint=job_a.run_fingerprint,
                publish_current=True,
                mark_stale_previous=False,
            )
        newer = await repo.begin_attempt(
            job_id=job_b.id, session_id=FIXTURE_SESSION_ID, recover=True
        )
        with pytest.raises(StaleGenerationError):
            await repo.finalize_attempt(
                job_id=job_b.id,
                attempt_id=attempt_b.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt_b.generation,
                processing_status=ProcessingJobStatus.SUCCEEDED,
                layered=None,
                limitations=[],
                snapshot_id=manifest.snapshot_id,
                snapshot_content_hash=manifest.snapshot_content_hash,
                run_fingerprint=job_b.run_fingerprint,
                publish_current=True,
                mark_stale_previous=False,
            )
        await db.commit()
        refreshed = await repo.get_job(job_b.id, session_id=FIXTURE_SESSION_ID)
    assert newer.generation == attempt_b.generation + 1
    assert refreshed.current_artifact_id is None
    assert refreshed.status != "succeeded"
    assert refreshed.active_generation == newer.generation


@pytest.mark.asyncio
async def test_late_source_blocks_old_publish_and_reuses_extraction(mi_db: None) -> None:
    del mi_db
    narrow = EngineRunConfig(limits=EngineLimits(max_segments_per_extract_page=1))
    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        first = await _run(service, config=narrow)
        first_artifact = first.job.current_artifact_id
        specs = [
            *SEGMENT_SPECS,
            (
                "11111111-1111-4111-8111-111111111107",
                7,
                110_000,
                111_000,
                "Catatan tambahan setelah freeze.",
            ),
        ]
        await _ensure_segments(db, FIXTURE_SESSION_ID, specs)
        await db.commit()
        second = await _run(
            service,
            reader=_reader_from_specs(specs),
            boundary=7,
            config=narrow,
        )
    assert first.extract_provider_calls > 0
    assert second.cache_hits > 0
    assert second.extract_provider_calls < first.extract_provider_calls
    assert second.provider_calls > 0
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        previous = await repo.load_artifact(first_artifact, session_id=FIXTURE_SESSION_ID)  # type: ignore[arg-type]
    assert previous.is_current is False
    assert previous.is_stale is True
    assert second.job.current_artifact_id != first_artifact


@pytest.mark.asyncio
async def test_old_attempt_cannot_publish_after_new_source(mi_db: None) -> None:
    del mi_db
    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        context = await service.prepare_run(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            manifest=build_s01_s06_manifest(),
            provider_adapter_id="deterministic-v1",
        )
        await _seed_late_segment(db)
        await service.register_snapshot(
            session_id=FIXTURE_SESSION_ID, manifest=_late_manifest(), supersede_previous=True
        )
        published = await service.repository.finalize_attempt(
            job_id=context.job.id,
            attempt_id=context.attempt.id,
            session_id=FIXTURE_SESSION_ID,
            generation=context.attempt.generation,
            processing_status=ProcessingJobStatus.PARTIAL,
            layered=None,
            limitations=[],
            snapshot_id=context.attempt.snapshot_id,
            snapshot_content_hash=context.attempt.source_content_hash,
            run_fingerprint=context.attempt.run_fingerprint,
            publish_current=True,
            mark_stale_previous=False,
        )
        await db.commit()
        row = await db.scalar(
            select(MIArtifactVersion).where(MIArtifactVersion.attempt_id == context.attempt.id)
        )
    assert published is None
    assert row is not None
    assert row.is_current is False
    assert row.is_stale is True


@pytest.mark.asyncio
async def test_missing_point_stays_failed_on_cache_hit(mi_db: None) -> None:
    del mi_db
    bundle = build_s01_s06_bundle()
    provider = DeterministicMeetingEssenceProvider(
        gold=bundle, drop_record_ids={bundle.records[-1].record_id}
    )
    request_id = uuid4()
    async with get_sessionmaker()() as db:
        first = await _run(
            MeetingIntelligenceLedgerService(db),
            provider=provider,
            client_request_id=request_id,
        )
    assert first.result.processing_status == ProcessingJobStatus.PARTIAL
    assert first.result.checkpoint.backward_semantic_check_status.value == "failed"
    assert first.extract_provider_calls > 0
    assert first.served_from_completed_cache is False
    async with get_sessionmaker()() as db:
        second = await _run(
            MeetingIntelligenceLedgerService(db),
            provider=DeterministicMeetingEssenceProvider(
                gold=bundle, drop_record_ids={bundle.records[-1].record_id}
            ),
            client_request_id=request_id,
        )
    assert second.served_from_completed_cache is False
    assert second.cache_hits > 0
    assert second.extract_provider_calls == 0
    assert second.result.processing_status == ProcessingJobStatus.PARTIAL
    assert second.result.checkpoint.backward_semantic_check_status.value == "failed"


@pytest.mark.asyncio
async def test_model_change_invalidates_and_failure_keeps_prior_result(mi_db: None) -> None:
    del mi_db
    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        first = await _run(service, config=EngineRunConfig(requested_model="model-a"))
        failed = await _run(
            service,
            provider=DeterministicMeetingEssenceProvider(
                gold=build_s01_s06_bundle(), fail_check=True
            ),
            config=EngineRunConfig(requested_model="model-b"),
        )
    assert failed.provider_calls > 0
    assert failed.served_from_completed_cache is False
    assert failed.result.processing_status != ProcessingJobStatus.SUCCEEDED
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        kept = await repo.load_artifact(
            first.job.current_artifact_id, session_id=FIXTURE_SESSION_ID
        )  # type: ignore[arg-type]
    assert kept.is_current is True
    assert kept.layered is not None
    assert kept.processing_status == ProcessingJobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_record_versions_do_not_hide_the_ledger(mi_db: None) -> None:
    del mi_db
    bundle = build_s01_s06_bundle()
    record = bundle.records[0]
    stale = record.model_copy(update={"uncertainty": "pre-reconcile leftover"})
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=bundle.manifest)
        await repo.persist_atomic_records(
            session_id=FIXTURE_SESSION_ID,
            snapshot_id=bundle.manifest.snapshot_id,
            snapshot_content_hash=bundle.manifest.snapshot_content_hash,
            records=[record],
        )
        await repo.persist_atomic_records(
            session_id=FIXTURE_SESSION_ID,
            snapshot_id=bundle.manifest.snapshot_id,
            snapshot_content_hash=bundle.manifest.snapshot_content_hash,
            records=[stale],
        )
        job = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            snapshot_id=bundle.manifest.snapshot_id,
            source_content_hash=bundle.manifest.snapshot_content_hash,
            run_fingerprint="d" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="e" * 64,
            provider_adapter_id="deterministic-v1",
        )
        attempt = await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID)
        await repo.finalize_attempt(
            job_id=job.id,
            attempt_id=attempt.id,
            session_id=FIXTURE_SESSION_ID,
            generation=attempt.generation,
            processing_status=ProcessingJobStatus.PARTIAL,
            layered=None,
            limitations=[],
            snapshot_id=bundle.manifest.snapshot_id,
            snapshot_content_hash=bundle.manifest.snapshot_content_hash,
            run_fingerprint=job.run_fingerprint,
            publish_current=False,
            mark_stale_previous=False,
            records=[record],
        )
        await db.commit()
        versions = (
            await db.scalars(
                select(MIAtomicRecordRow).where(
                    MIAtomicRecordRow.session_id == FIXTURE_SESSION_ID,
                    MIAtomicRecordRow.record_id == record.record_id,
                )
            )
        ).all()
        artifact = await db.scalar(
            select(MIArtifactVersion).where(MIArtifactVersion.attempt_id == attempt.id)
        )
        assert artifact is not None
        loaded = await repo.load_ledger_records(
            artifact_id=artifact.id, session_id=FIXTURE_SESSION_ID
        )
    assert len(versions) == 2
    assert loaded[0].uncertainty is None


@pytest.mark.asyncio
async def test_caller_config_callback_is_not_replaced(mi_db: None) -> None:
    del mi_db
    config = EngineRunConfig()
    async with get_sessionmaker()() as db:
        await _run(MeetingIntelligenceLedgerService(db), config=config)
    assert config.on_checkpoint_committed is None
    assert config.on_validated_stage is None


@pytest.mark.asyncio
async def test_concurrent_attempts_receive_distinct_generations(mi_db: None) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        job = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="f" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="g" * 64,
            provider_adapter_id="deterministic-v1",
        )
        await db.commit()
        job_id = job.id

    async def _acquire() -> str:
        async with get_sessionmaker()() as db:
            repo = MeetingIntelligenceRepository(db)
            try:
                attempt = await repo.begin_attempt(job_id=job_id, session_id=FIXTURE_SESSION_ID)
            except AttemptBusyError as exc:
                await db.rollback()
                return f"busy:{exc.generation}"
            await db.commit()
            return f"gen:{attempt.generation}"

    outcomes = set(await asyncio.gather(_acquire(), _acquire()))
    assert outcomes == {"gen:1", "busy:1"}


def _run_witness(phase: str, client_request_id: UUID) -> dict[str, object]:
    env = os.environ.copy()
    env["DATABASE_URL"] = _database_url()
    env["PYTHONPATH"] = os.pathsep.join([str(API_ROOT / "src"), str(API_ROOT / "tests")])
    completed = subprocess.run(
        [
            sys.executable,
            str(WITNESS),
            phase,
            "--database-url",
            _database_url(),
            "--client-request-id",
            str(client_request_id),
            "--session-id",
            str(FIXTURE_SESSION_ID),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=API_ROOT,
        env=env,
    )
    if completed.returncode != 0:
        pytest.fail(completed.stderr or completed.stdout)
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    pytest.fail(completed.stdout)


@pytest.mark.asyncio
async def test_fresh_process_and_postgres_restart_resumes_without_repeating_extract(
    mi_db: None,
) -> None:
    _require_owned_cluster()
    del mi_db
    client_request_id = uuid4()
    produced = _run_witness("produce-extract", client_request_id)
    assert produced["extract_complete"] is True
    await _release_database_caches_async()
    _stop_pg()
    assert not _pg_is_running()
    _start_pg()
    consumed = _run_witness("consume", client_request_id)
    assert consumed["status"] == "succeeded"
    assert consumed["extract_provider_calls"] == 0
    assert consumed["served_from_completed_cache"] is False
    assert consumed["provider_calls"] > 0
    assert consumed["prompt_cache_key"]
    assert consumed["attempt_id"] != "00000000-0000-0000-0000-000000000000"


@pytest.mark.asyncio
async def test_restart_after_acked_check_does_not_repeat_that_page(mi_db: None) -> None:
    _require_owned_cluster()
    del mi_db
    client_request_id = uuid4()
    produced = _run_witness("produce-check", client_request_id)
    assert produced["acked"]
    assert produced["extract_complete"] is True
    await _release_database_caches_async()
    _stop_pg()
    _start_pg()
    consumed = _run_witness("consume-check", client_request_id)
    assert consumed["extract_provider_calls"] == 0
    assert set(produced["acked"]).isdisjoint(consumed["check_invocations"])
    assert set(produced["acked"]) <= set(consumed["acked"])
    assert consumed["generation"] >= 2


def _bound_checkpoint(job, manifest, **updates):  # type: ignore[no-untyped-def]
    checkpoint = EngineCheckpoint(
        binding_version="meeting-essence-checkpoint.v2",
        snapshot_id=manifest.snapshot_id,
        snapshot_content_hash=manifest.snapshot_content_hash,
        prompt_cache_key=job.prompt_cache_key,
        schema_identity=job.schema_identity,
        algorithm_identity=job.algorithm_identity,
        input_fingerprint=compute_input_fingerprint(manifest),
        session_id=str(job.session_id),
        requested_model=job.requested_model,
        requested_reasoning_effort=job.requested_reasoning_effort,
        limits_fingerprint=job.limits_fingerprint,
        extract_complete=True,
        acked_check_fingerprints={"page-a"},
        completed_fragment_indices=(0,),
    )
    for key, value in updates.items():
        setattr(checkpoint, key, value)
    return checkpoint


class _AnchoredDeadlineProvider(DeterministicMeetingEssenceProvider):
    async def run_stage(self, **kwargs: object) -> object:
        if kwargs.get("operation") == StageOperation.EXTRACT:
            request = kwargs["request"]
            meta = request.source.meeting_metadata  # type: ignore[attr-defined]
            meeting_day = date.fromisoformat(meta["meeting_date"])
            normalized = datetime.combine(
                meeting_day + timedelta(days=1),
                time(12, 0),
                tzinfo=ZoneInfo(meta["timezone"]),
            )

            def apply(record):  # type: ignore[no-untyped-def]
                if record.deadline_raw != "besok pukul 12.00":
                    return record
                return record.model_copy(
                    update={
                        "deadline_normalized": normalized,
                        "deadline_normalization_basis": (
                            DeadlineNormalizationBasis.MEETING_DATE_TIMEZONE
                        ),
                    }
                )

            self.extraction_records = [apply(record) for record in self.extraction_records]
            self.evaluation_records = [apply(record) for record in (self.evaluation_records or [])]
            mapping: dict[UUID, list] = {}
            for record in self.extraction_records:
                for evidence in record.evidence:
                    mapping.setdefault(evidence.segment_id, []).append(record)
            self.extract_by_segment = mapping
        return await super().run_stage(**kwargs)  # type: ignore[arg-type]


def _dini_deadline(outcome):  # type: ignore[no-untyped-def]
    return next(
        record
        for record in outcome.result.checkpoint.atomic_records
        if record.deadline_raw == "besok pukul 12.00"
    )


@pytest.mark.asyncio
async def test_meeting_metadata_changes_normalized_deadline_and_extract_key(mi_db: None) -> None:
    del mi_db
    bundle = build_s01_s06_bundle()
    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        first = await _run(
            service,
            provider=_AnchoredDeadlineProvider(gold=bundle),
            meeting_metadata={"meeting_date": "2026-10-14", "timezone": "Asia/Bangkok"},
        )
        second = await _run(
            service,
            provider=_AnchoredDeadlineProvider(gold=bundle),
            meeting_metadata={"meeting_date": "2026-10-21", "timezone": "Asia/Bangkok"},
        )
    assert first.extract_provider_calls > 0
    assert second.extract_provider_calls > 0
    assert second.cache_hits == 0
    assert first.result.processing_status == ProcessingJobStatus.SUCCEEDED
    assert second.result.processing_status == ProcessingJobStatus.SUCCEEDED
    assert second.job.status == MIJobStatus.SUCCEEDED
    assert second.job.current_artifact_id is not None
    assert second.job.source_content_hash != first.job.source_content_hash
    assert _dini_deadline(first).deadline_normalized == datetime(
        2026, 10, 15, 12, 0, tzinfo=ZoneInfo("Asia/Bangkok")
    )
    assert _dini_deadline(second).deadline_normalized == datetime(
        2026, 10, 22, 12, 0, tzinfo=ZoneInfo("Asia/Bangkok")
    )


@pytest.mark.asyncio
async def test_replaying_an_older_snapshot_does_not_roll_authority_backwards(mi_db: None) -> None:
    del mi_db
    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        await service.repository.delete_session_intelligence(FIXTURE_SESSION_ID)
        await db.commit()
        first = await _run(service)
        await _seed_late_segment(db)
        late = _late_manifest()
        await service.register_snapshot(session_id=FIXTURE_SESSION_ID, manifest=late)
        await db.commit()
        assert await service.repository.current_source_hash(FIXTURE_SESSION_ID) == (
            late.snapshot_content_hash
        )
        replay = await _run(service)
        current = await service.repository.current_source_hash(FIXTURE_SESSION_ID)
        replay_artifact = await db.scalar(
            select(MIArtifactVersion).where(MIArtifactVersion.attempt_id == replay.attempt.id)
        )
    assert first.result.processing_status == ProcessingJobStatus.SUCCEEDED
    assert current == late.snapshot_content_hash
    assert current != first.job.source_content_hash
    assert replay.job.current_artifact_id is None
    assert replay.job.status == MIJobStatus.PARTIAL
    assert replay_artifact is not None
    assert replay_artifact.is_current is False
    assert replay_artifact.is_stale is True


@pytest.mark.asyncio
async def test_interleaved_transactions_cannot_restore_an_older_source(
    mi_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    late = _late_manifest()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await _seed_late_segment(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        await db.commit()
    release = asyncio.Event()
    started = asyncio.Event()
    original = MeetingIntelligenceRepository._lock_fence
    state = {"n": 0}

    async def wrapped(self, session_id: UUID):  # type: ignore[no-untyped-def]
        fence = await original(self, session_id)
        state["n"] += 1
        if state["n"] == 1:
            started.set()
            await release.wait()
        return fence

    monkeypatch.setattr(MeetingIntelligenceRepository, "_lock_fence", wrapped)

    async def promote_newer() -> None:
        async with get_sessionmaker()() as db:
            repo = MeetingIntelligenceRepository(db)
            await repo.upsert_snapshot(
                session_id=FIXTURE_SESSION_ID, manifest=late, supersede_previous=True
            )
            await db.commit()

    async def replay_older() -> None:
        async with get_sessionmaker()() as db:
            repo = MeetingIntelligenceRepository(db)
            await repo.upsert_snapshot(
                session_id=FIXTURE_SESSION_ID, manifest=manifest, supersede_previous=True
            )
            await db.commit()

    newer_task = asyncio.create_task(promote_newer())
    await started.wait()
    older_task = asyncio.create_task(replay_older())
    await asyncio.sleep(0.4)
    release.set()
    await newer_task
    await older_task
    async with get_sessionmaker()() as db:
        current = await MeetingIntelligenceRepository(db).current_source_hash(FIXTURE_SESSION_ID)
    assert current == late.snapshot_content_hash


@pytest.mark.asyncio
async def test_live_duplicate_is_busy_and_recovery_is_a_new_fence(mi_db: None) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        job = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="c" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="l" * 64,
            provider_adapter_id="deterministic-v1",
            requested_model="model-a",
        )
        live = await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID)
        saved = _bound_checkpoint(job, manifest)
        await repo.save_checkpoint(
            attempt_id=live.id,
            job_id=job.id,
            session_id=FIXTURE_SESSION_ID,
            generation=live.generation,
            checkpoint=saved,
        )
        with pytest.raises(AttemptBusyError) as busy:
            await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID, reclaim=True)
        recovered = await repo.begin_attempt(
            job_id=job.id, session_id=FIXTURE_SESSION_ID, recover=True
        )
        loaded = await repo.load_checkpoint(attempt_id=recovered.id, session_id=FIXTURE_SESSION_ID)
        with pytest.raises(StaleGenerationError):
            await repo.save_checkpoint(
                attempt_id=live.id,
                job_id=job.id,
                session_id=FIXTURE_SESSION_ID,
                generation=live.generation,
                checkpoint=_bound_checkpoint(
                    job, manifest, acked_check_fingerprints={"page-a", "page-b"}
                ),
            )
        await db.commit()
    assert busy.value.attempt_id == live.id
    assert busy.value.generation == live.generation
    assert recovered.id != live.id
    assert recovered.generation == live.generation + 1
    assert loaded is not None
    assert loaded.acked_check_fingerprints == {"page-a"}


@pytest.mark.asyncio
async def test_checkpoint_rejects_unbound_cross_config_and_regression(mi_db: None) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        job = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="d" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="l" * 64,
            provider_adapter_id="deterministic-v1",
            requested_model="model-a",
            requested_reasoning_effort="low",
        )
        attempt = await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID)
        with pytest.raises(AttemptFenceError):
            await repo.save_checkpoint(
                attempt_id=attempt.id,
                job_id=job.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt.generation,
                checkpoint=EngineCheckpoint(),
            )
        with pytest.raises(AttemptFenceError):
            await repo.save_checkpoint(
                attempt_id=attempt.id,
                job_id=job.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt.generation,
                checkpoint=_bound_checkpoint(job, manifest, requested_model="other-model"),
            )
        with pytest.raises(AttemptFenceError):
            await repo.save_checkpoint(
                attempt_id=attempt.id,
                job_id=job.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt.generation,
                checkpoint=_bound_checkpoint(job, manifest, limits_fingerprint="f" * 64),
            )
        with pytest.raises(AttemptFenceError, match="not recognized"):
            await repo.save_checkpoint(
                attempt_id=attempt.id,
                job_id=job.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt.generation,
                checkpoint=_bound_checkpoint(
                    job, manifest, binding_version="meeting-essence-checkpoint.v9"
                ),
            )
        with pytest.raises(AttemptFenceError, match="input fingerprint"):
            await repo.save_checkpoint(
                attempt_id=attempt.id,
                job_id=job.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt.generation,
                checkpoint=_bound_checkpoint(job, manifest, input_fingerprint="cd" * 32),
            )
        assert CHECKPOINT_BINDING_VERSION == "meeting-essence-checkpoint.v2"
        await repo.save_checkpoint(
            attempt_id=attempt.id,
            job_id=job.id,
            session_id=FIXTURE_SESSION_ID,
            generation=attempt.generation,
            checkpoint=_bound_checkpoint(job, manifest),
        )
        with pytest.raises(CheckpointRegressionError):
            await repo.save_checkpoint(
                attempt_id=attempt.id,
                job_id=job.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt.generation,
                checkpoint=_bound_checkpoint(job, manifest, acked_check_fingerprints=set()),
            )
        await db.commit()


@pytest.mark.asyncio
async def test_cancel_is_durable_and_retry_is_explicit(mi_db: None) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        repo = service.repository
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        job = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="e" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="l" * 64,
            provider_adapter_id="deterministic-v1",
        )
        attempt = await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID)
        await db.commit()
        await service.cancel_work(
            attempt_id=attempt.id,
            session_id=FIXTURE_SESSION_ID,
            generation=attempt.generation,
        )
        cancelled = await repo.get_job(job.id, session_id=FIXTURE_SESSION_ID)
        unresolved = await service.list_unresolved_work(session_id=FIXTURE_SESSION_ID)
        with pytest.raises(AttemptFenceError):
            await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID)
        retried = await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID, retry=True)
        await db.commit()
        again = await service.list_unresolved_work(session_id=FIXTURE_SESSION_ID)
    assert cancelled.status == MIJobStatus.CANCELLED
    assert all(item.job.id != job.id for item in unresolved.items)
    assert retried.generation == attempt.generation + 1
    assert retried.id != attempt.id
    assert any(item.job.id == job.id and item.live_attempt_id == retried.id for item in again.items)


@pytest.mark.asyncio
async def test_cancel_commits_before_a_waiting_checkpoint(
    mi_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        job = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="9" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="l" * 64,
            provider_adapter_id="deterministic-v1",
            requested_model="model-a",
        )
        attempt = await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID)
        await db.commit()
        job_id = job.id
        attempt_id = attempt.id
        generation = attempt.generation
    release = asyncio.Event()
    started = asyncio.Event()
    original = MeetingIntelligenceRepository._running_attempt
    state = {"n": 0}

    async def wrapped(self, **kwargs):  # type: ignore[no-untyped-def]
        locked = await original(self, **kwargs)
        state["n"] += 1
        if state["n"] == 1:
            started.set()
            await release.wait()
        return locked

    monkeypatch.setattr(MeetingIntelligenceRepository, "_running_attempt", wrapped)

    async def writer() -> None:
        async with get_sessionmaker()() as db:
            repo = MeetingIntelligenceRepository(db)
            job = await repo.get_job(job_id, session_id=FIXTURE_SESSION_ID)
            await repo.save_checkpoint(
                attempt_id=attempt_id,
                job_id=job_id,
                session_id=FIXTURE_SESSION_ID,
                generation=generation,
                checkpoint=_bound_checkpoint(job, manifest),
            )
            await db.commit()

    async def canceller() -> None:
        await started.wait()
        async with get_sessionmaker()() as db:
            repo = MeetingIntelligenceRepository(db)
            await repo.cancel_attempt(
                attempt_id=attempt_id,
                session_id=FIXTURE_SESSION_ID,
                generation=generation,
            )
            await db.commit()

    writer_task = asyncio.create_task(writer())
    await started.wait()
    cancel_task = asyncio.create_task(canceller())
    await asyncio.sleep(0.4)
    release.set()
    await writer_task
    await cancel_task
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        job = await repo.get_job(job_id, session_id=FIXTURE_SESSION_ID)
        with pytest.raises(AttemptFenceError):
            await repo.finalize_attempt(
                job_id=job_id,
                attempt_id=attempt_id,
                session_id=FIXTURE_SESSION_ID,
                generation=generation,
                processing_status=ProcessingJobStatus.SUCCEEDED,
                layered=None,
                limitations=[],
                snapshot_id=manifest.snapshot_id,
                snapshot_content_hash=manifest.snapshot_content_hash,
                run_fingerprint=job.run_fingerprint,
                publish_current=True,
                mark_stale_previous=False,
            )
    assert job.status == MIJobStatus.CANCELLED


@pytest.mark.asyncio
async def test_snapshot_identity_and_request_aliases_are_durable(mi_db: None) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        stamped = manifest.model_copy(update={"captured_at": datetime.now(tz=UTC)})
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=stamped)
        poisoned = manifest.model_copy(update={"meeting_metadata": {"injected": "1"}})
        with pytest.raises(SnapshotImmutableError):
            await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=poisoned)
        request_a = uuid4()
        request_b = uuid4()
        first = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=request_a,
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="a" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="l" * 64,
            provider_adapter_id="deterministic-v1",
            requested_model="model-a",
        )
        aliased = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=request_b,
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="a" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="l" * 64,
            provider_adapter_id="deterministic-v1",
            requested_model="model-a",
        )
        with pytest.raises(IdempotencyConflictError):
            await repo.create_or_get_job(
                session_id=FIXTURE_SESSION_ID,
                client_request_id=request_b,
                snapshot_id=manifest.snapshot_id,
                source_content_hash=manifest.snapshot_content_hash,
                run_fingerprint="b" * 64,
                prompt_cache_key="p" * 64,
                schema_identity="schema",
                algorithm_identity="algorithm",
                limits_fingerprint="m" * 64,
                provider_adapter_id="deterministic-v1",
                requested_model="model-b",
            )
        await db.commit()
    assert aliased.id == first.id


@pytest.mark.asyncio
async def test_relation_cross_session_evidence_is_rejected(mi_db: None) -> None:
    del mi_db
    bundle = build_s01_s06_bundle()
    manifest = bundle.manifest
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        await repo.persist_atomic_records(
            session_id=FIXTURE_SESSION_ID,
            snapshot_id=manifest.snapshot_id,
            snapshot_content_hash=manifest.snapshot_content_hash,
            records=bundle.records,
        )
        job = await repo.create_or_get_job(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint="7" * 64,
            prompt_cache_key="p" * 64,
            schema_identity="schema",
            algorithm_identity="algorithm",
            limits_fingerprint="l" * 64,
            provider_adapter_id="deterministic-v1",
        )
        attempt = await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID)
        foreign = bundle.relations[0].model_copy(
            update={
                "evidence": [
                    EvidenceReference(
                        segment_id=UUID(_shift_uuid("11111111-1111-4111-8111-111111111103")),
                        start_ms=96_000,
                        end_ms=102_000,
                    )
                ]
            }
        )
        with pytest.raises(EvidenceOwnershipError):
            await repo.finalize_attempt(
                job_id=job.id,
                attempt_id=attempt.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt.generation,
                processing_status=ProcessingJobStatus.PARTIAL,
                layered=None,
                limitations=[],
                snapshot_id=manifest.snapshot_id,
                snapshot_content_hash=manifest.snapshot_content_hash,
                run_fingerprint=job.run_fingerprint,
                publish_current=False,
                mark_stale_previous=False,
                records=bundle.records,
                relations=[foreign],
            )
        missing = bundle.records[0].model_copy(update={"uncertainty": "not stored"})
        with pytest.raises(EvidenceOwnershipError):
            await repo.finalize_attempt(
                job_id=job.id,
                attempt_id=attempt.id,
                session_id=FIXTURE_SESSION_ID,
                generation=attempt.generation,
                processing_status=ProcessingJobStatus.PARTIAL,
                layered=None,
                limitations=[],
                snapshot_id=manifest.snapshot_id,
                snapshot_content_hash=manifest.snapshot_content_hash,
                run_fingerprint=job.run_fingerprint,
                publish_current=False,
                mark_stale_previous=False,
                records=[missing],
            )


def _metadata_variant(metadata: dict[str, str]) -> SourceSnapshotManifest:
    base = build_s01_s06_manifest()
    return finalize_snapshot_manifest(
        base.model_copy(
            update={
                "meeting_metadata": metadata,
                "snapshot_id": "snap_tmp",
                "snapshot_content_hash": "0" * 64,
            }
        )
    )


def _replace_segment(manifest: SourceSnapshotManifest, index: int, segment: ManifestSegmentEntry):
    segments = list(manifest.segments)
    segments[index] = segment
    return finalize_snapshot_manifest(
        manifest.model_copy(
            update={
                "segments": segments,
                "snapshot_id": "snap_tmp",
                "snapshot_content_hash": "0" * 64,
            }
        )
    )


async def _direct_job(
    repo: MeetingIntelligenceRepository, manifest: SourceSnapshotManifest, mark: str
):
    return await repo.create_or_get_job(
        session_id=FIXTURE_SESSION_ID,
        client_request_id=uuid4(),
        snapshot_id=manifest.snapshot_id,
        source_content_hash=manifest.snapshot_content_hash,
        run_fingerprint=mark * 64,
        prompt_cache_key="p" * 64,
        schema_identity="schema",
        algorithm_identity="algorithm",
        limits_fingerprint="l" * 64,
        provider_adapter_id="deterministic-v1",
        requested_model="model-a",
        requested_reasoning_effort="low",
    )


@pytest.mark.asyncio
async def test_cursor_regression_preserves_source_output_and_relation_progress(mi_db: None) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    record = build_s01_s06_bundle().records[0]
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        job = await _direct_job(repo, manifest, "4")
        attempt = await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID)
        accepted = _bound_checkpoint(
            job,
            manifest,
            source_check_cursor=3,
            source_check_record_cursor=2,
            source_check_claim_cursor=1,
            output_check_cursor=2,
            output_support_cursor=1,
            output_check_record_cursor=1,
            relation_context_cursor=4,
            atomic_records=[record],
        )
        await repo.save_checkpoint(
            attempt_id=attempt.id,
            job_id=job.id,
            session_id=FIXTURE_SESSION_ID,
            generation=attempt.generation,
            checkpoint=accepted,
        )
        regressions = [
            {"source_check_cursor": 0},
            {"source_check_cursor": 3, "source_check_record_cursor": 0},
            {"output_check_cursor": 0},
            {"output_check_cursor": 2, "output_support_cursor": 0},
            {"relation_context_cursor": 1},
            {"atomic_records": []},
        ]
        for update in regressions:
            with pytest.raises(CheckpointRegressionError):
                await repo.save_checkpoint(
                    attempt_id=attempt.id,
                    job_id=job.id,
                    session_id=FIXTURE_SESSION_ID,
                    generation=attempt.generation,
                    checkpoint=_bound_checkpoint(
                        job, manifest, **{**_progress(accepted), **update}
                    ),
                )
        await repo.save_checkpoint(
            attempt_id=attempt.id,
            job_id=job.id,
            session_id=FIXTURE_SESSION_ID,
            generation=attempt.generation,
            checkpoint=_bound_checkpoint(
                job,
                manifest,
                **{
                    **_progress(accepted),
                    "source_check_cursor": 4,
                    "source_check_record_cursor": 0,
                    "source_check_claim_cursor": 0,
                    "source_check_relation_cursor": 0,
                    "output_check_cursor": 3,
                    "output_support_cursor": 0,
                    "output_check_record_cursor": 0,
                },
            ),
        )
        await db.commit()
        attempt_id = attempt.id
    async with get_sessionmaker()() as db:
        loaded = await MeetingIntelligenceRepository(db).load_checkpoint(
            attempt_id=attempt_id, session_id=FIXTURE_SESSION_ID
        )
    assert loaded is not None
    assert loaded.source_check_cursor == 4
    assert loaded.source_check_record_cursor == 0
    assert loaded.output_check_cursor == 3
    assert loaded.output_support_cursor == 0
    assert loaded.relation_context_cursor == 4
    assert loaded.atomic_records[0].record_id == record.record_id
    assert loaded.acked_check_fingerprints == {"page-a"}


def _progress(checkpoint: EngineCheckpoint) -> dict[str, object]:
    return {
        "source_check_cursor": checkpoint.source_check_cursor,
        "source_check_record_cursor": checkpoint.source_check_record_cursor,
        "source_check_claim_cursor": checkpoint.source_check_claim_cursor,
        "source_check_relation_cursor": checkpoint.source_check_relation_cursor,
        "output_check_cursor": checkpoint.output_check_cursor,
        "output_support_cursor": checkpoint.output_support_cursor,
        "output_check_record_cursor": checkpoint.output_check_record_cursor,
        "output_check_claim_cursor": checkpoint.output_check_claim_cursor,
        "output_check_relation_cursor": checkpoint.output_check_relation_cursor,
        "relation_context_cursor": checkpoint.relation_context_cursor,
        "atomic_records": list(checkpoint.atomic_records),
        "acked_check_fingerprints": set(checkpoint.acked_check_fingerprints),
        "completed_fragment_indices": checkpoint.completed_fragment_indices,
    }


@pytest.mark.asyncio
async def test_publication_requires_accepted_checkpoint_and_bound_claims(mi_db: None) -> None:
    del mi_db
    bundle = build_s01_s06_bundle()
    manifest = bundle.manifest
    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        context = await service.prepare_run(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            manifest=manifest,
            provider_adapter_id="deterministic-v1",
        )
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        job = await repo.get_job(context.job.id, session_id=FIXTURE_SESSION_ID)
        attempt = await repo.get_attempt(context.attempt.id, session_id=FIXTURE_SESSION_ID)

        async def publish(**overrides: object) -> None:
            payload: dict[str, object] = {
                "job_id": job.id,
                "attempt_id": attempt.id,
                "session_id": FIXTURE_SESSION_ID,
                "generation": attempt.generation,
                "processing_status": ProcessingJobStatus.SUCCEEDED,
                "limitations": [],
                "snapshot_id": manifest.snapshot_id,
                "snapshot_content_hash": manifest.snapshot_content_hash,
                "run_fingerprint": job.run_fingerprint,
                "publish_current": True,
                "mark_stale_previous": False,
                "records": list(bundle.records),
                "relations": list(bundle.relations),
                "layered": bundle.result,
            }
            payload.update(overrides)
            await repo.finalize_attempt(**payload)  # type: ignore[arg-type]

        foreign = bundle.result.model_copy(update={"snapshot_id": "snap_FOREIGN_RECORDING"})
        with pytest.raises(PublicationIntegrityError):
            await publish(layered=foreign, records=[], relations=[])
        bullet = bundle.result.overview_bullets[0]
        crossed = bundle.result.model_copy(
            update={
                "overview_bullets": [
                    bullet.model_copy(
                        update={
                            "evidence": [
                                EvidenceReference(
                                    segment_id=UUID(
                                        _shift_uuid(str(bullet.evidence[0].segment_id))
                                    ),
                                    start_ms=bullet.evidence[0].start_ms,
                                    end_ms=bullet.evidence[0].end_ms,
                                )
                            ]
                        }
                    ),
                    *bundle.result.overview_bullets[1:],
                ]
            }
        )
        with pytest.raises(PublicationIntegrityError):
            await publish(layered=crossed)
        with pytest.raises(PublicationIntegrityError):
            await publish(records=[])
        await repo.save_checkpoint(
            attempt_id=attempt.id,
            job_id=job.id,
            session_id=FIXTURE_SESSION_ID,
            generation=attempt.generation,
            checkpoint=_bound_checkpoint(
                job, manifest, extract_complete=True, check_complete=False
            ),
        )
        with pytest.raises(PublicationIntegrityError):
            await publish()
        await repo.save_checkpoint(
            attempt_id=attempt.id,
            job_id=job.id,
            session_id=FIXTURE_SESSION_ID,
            generation=attempt.generation,
            checkpoint=_bound_checkpoint(
                job,
                manifest,
                extract_complete=True,
                reconcile_complete=True,
                render_complete=True,
                check_complete=False,
                backward_semantic_check_status=CheckStatus.FAILED,
                semantic_check_method=STRUCTURAL_CHECK_METHOD,
            ),
        )
        with pytest.raises(PublicationIntegrityError):
            await publish()
        await repo.save_checkpoint(
            attempt_id=attempt.id,
            job_id=job.id,
            session_id=FIXTURE_SESSION_ID,
            generation=attempt.generation,
            checkpoint=_bound_checkpoint(
                job,
                manifest,
                extract_complete=True,
                reconcile_complete=True,
                check_complete=True,
                render_complete=True,
                forward_semantic_check_status=CheckStatus.COMPLETED,
                backward_semantic_check_status=CheckStatus.COMPLETED,
                semantic_check_method=STRUCTURAL_CHECK_METHOD,
                processing_status=ProcessingJobStatus.SUCCEEDED,
                layered_partial=bundle.result,
                atomic_records=list(bundle.records),
                relations=list(bundle.relations),
            ),
        )
        with pytest.raises(PublicationIntegrityError):
            await publish()
        await db.rollback()
        current = await repo.load_current_publication(job_id=job.id, session_id=FIXTURE_SESSION_ID)
    assert current is None


@pytest.mark.asyncio
async def test_altered_payload_cannot_replace_an_accepted_result(mi_db: None) -> None:
    del mi_db
    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        outcome = await _run(service)
        repo = service.repository
        duplicate = await repo.finalize_attempt(
            job_id=outcome.job.id,
            attempt_id=outcome.attempt.id,
            session_id=FIXTURE_SESSION_ID,
            generation=outcome.attempt.generation,
            processing_status=outcome.result.processing_status,
            layered=outcome.result.layered_result,
            limitations=outcome.result.limitations,
            snapshot_id=outcome.job.snapshot_id,
            snapshot_content_hash=outcome.job.source_content_hash,
            run_fingerprint=outcome.job.run_fingerprint,
            publish_current=True,
            mark_stale_previous=False,
            records=outcome.result.checkpoint.atomic_records,
            relations=outcome.result.checkpoint.relations,
        )
        context = await service.prepare_run(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            manifest=build_s01_s06_manifest(),
            provider_adapter_id="deterministic-alt",
        )
        copied = outcome.result.checkpoint
        await repo.save_checkpoint(
            attempt_id=context.attempt.id,
            job_id=context.job.id,
            session_id=FIXTURE_SESSION_ID,
            generation=context.attempt.generation,
            checkpoint=copied,
        )
        await db.commit()
        layered = copied.layered_partial
        assert layered is not None
        altered = layered.model_copy(
            update={
                "overview_bullets": [
                    layered.overview_bullets[0].model_copy(
                        update={"text": f"{layered.overview_bullets[0].text} extra"}
                    ),
                    *layered.overview_bullets[1:],
                ]
            }
        )
        with pytest.raises(PublicationIntegrityError):
            await repo.finalize_attempt(
                job_id=context.job.id,
                attempt_id=context.attempt.id,
                session_id=FIXTURE_SESSION_ID,
                generation=context.attempt.generation,
                processing_status=ProcessingJobStatus.SUCCEEDED,
                layered=altered,
                limitations=outcome.result.limitations,
                snapshot_id=context.job.snapshot_id,
                snapshot_content_hash=context.job.source_content_hash,
                run_fingerprint=context.job.run_fingerprint,
                publish_current=True,
                mark_stale_previous=False,
                records=copied.atomic_records,
                relations=copied.relations,
            )
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        loaded = await repo.load_checkpoint(
            attempt_id=context.attempt.id, session_id=FIXTURE_SESSION_ID
        )
        original = await repo.load_artifact(
            outcome.job.current_artifact_id, session_id=FIXTURE_SESSION_ID
        )  # type: ignore[arg-type]
        second = await repo.get_job(context.job.id, session_id=FIXTURE_SESSION_ID)
    assert duplicate == outcome.job.current_artifact_id
    assert loaded is not None and loaded.layered_partial is not None
    assert loaded.layered_partial.overview_bullets[0].text == layered.overview_bullets[0].text
    assert original.is_current is True
    assert second.current_artifact_id is None


@pytest.mark.asyncio
async def test_snapshot_segments_must_match_canonical_rows(mi_db: None) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    segment = manifest.segments[0]
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await _seed_late_segment(db)
        prefix = await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        late = await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=_late_manifest())
        wrong_text = segment.model_copy(
            update={
                "text": f"{segment.text}!",
                "content_hash": segment_content_hash(f"{segment.text}!"),
            }
        )
        wrong_time = segment.model_copy(update={"end_ms": segment.end_ms + 25})
        missing = segment.model_copy(update={"id": uuid4()})
        foreign = segment.model_copy(update={"id": UUID(_shift_uuid(str(segment.id)))})
        for poisoned in (wrong_text, wrong_time, missing, foreign):
            with pytest.raises(SnapshotImmutableError):
                await repo.upsert_snapshot(
                    session_id=FIXTURE_SESSION_ID,
                    manifest=_replace_segment(manifest, 0, poisoned),
                )
        stored = await db.get(TranscriptSegment, segment.id)
        await db.commit()
    assert stored is not None
    assert stored.text == segment.text
    assert stored.end_ms == segment.end_ms
    assert prefix.snapshot_content_hash == manifest.snapshot_content_hash
    assert late.manifest.publication_boundary_sequence == 7
    async with get_sessionmaker()() as db:
        current = await MeetingIntelligenceRepository(db).current_source_hash(FIXTURE_SESSION_ID)
    assert current == late.snapshot_content_hash


@pytest.mark.asyncio
async def test_stale_metadata_epoch_cannot_replace_a_newer_equal_boundary(mi_db: None) -> None:
    del mi_db
    newer = _metadata_variant({"meeting_date": "2026-10-21", "timezone": "Asia/Bangkok"})
    stale = _metadata_variant({"meeting_date": "2026-10-01", "timezone": "Asia/Bangkok"})
    later = _metadata_variant({"meeting_date": "2026-10-28", "timezone": "Asia/Bangkok"})
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=build_s01_s06_manifest())
        await db.commit()
        captured = await repo.observed_source_epoch(FIXTURE_SESSION_ID)
    newer_done = asyncio.Event()

    async def promote_newer() -> None:
        async with get_sessionmaker()() as db:
            repo = MeetingIntelligenceRepository(db)
            epoch = await repo.observed_source_epoch(FIXTURE_SESSION_ID)
            await repo.upsert_snapshot(
                session_id=FIXTURE_SESSION_ID,
                manifest=newer,
                supersede_previous=True,
                expected_source_epoch=epoch,
            )
            await db.commit()
        newer_done.set()

    async def arrive_stale() -> str | None:
        await newer_done.wait()
        async with get_sessionmaker()() as db:
            repo = MeetingIntelligenceRepository(db)
            await repo.upsert_snapshot(
                session_id=FIXTURE_SESSION_ID,
                manifest=stale,
                supersede_previous=True,
                expected_source_epoch=captured,
            )
            await db.commit()
            return await repo.current_source_hash(FIXTURE_SESSION_ID)

    await promote_newer()
    current = await arrive_stale()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        epoch = await repo.observed_source_epoch(FIXTURE_SESSION_ID)
        await repo.upsert_snapshot(
            session_id=FIXTURE_SESSION_ID,
            manifest=later,
            supersede_previous=True,
            expected_source_epoch=epoch,
        )
        await db.commit()
        promoted = await repo.current_source_hash(FIXTURE_SESSION_ID)
        advanced = await repo.observed_source_epoch(FIXTURE_SESSION_ID)
    assert current == newer.snapshot_content_hash
    assert promoted == later.snapshot_content_hash
    assert advanced == epoch + 1


@pytest.mark.asyncio
async def test_unresolved_work_pages_are_bounded_and_skip_cancelled_jobs(mi_db: None) -> None:
    del mi_db
    manifest = build_s01_s06_manifest()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.upsert_snapshot(session_id=FIXTURE_SESSION_ID, manifest=manifest)
        created: list[tuple[object, object]] = []
        for mark in ("5", "6", "8"):
            job = await _direct_job(repo, manifest, mark)
            attempt = await repo.begin_attempt(job_id=job.id, session_id=FIXTURE_SESSION_ID)
            await db.commit()
            created.append((job, attempt))
        cancelled_job, cancelled_attempt = created[1]
        await repo.cancel_attempt(
            attempt_id=cancelled_attempt.id,  # type: ignore[attr-defined]
            session_id=FIXTURE_SESSION_ID,
            generation=cancelled_attempt.generation,  # type: ignore[attr-defined]
        )
        await db.commit()
        seen: list[object] = []
        cursor = None
        pages = 0
        while True:
            page = await repo.list_unresolved_work(
                session_id=FIXTURE_SESSION_ID, limit=1, cursor=cursor
            )
            pages += 1
            assert len(page.items) <= 1
            seen.extend(item.job.id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
            assert pages < 6
        with pytest.raises(MeetingIntelligenceRepositoryError):
            await repo.list_unresolved_work(session_id=FIXTURE_SESSION_ID, cursor="not-a-cursor")
    assert cancelled_job.id not in seen  # type: ignore[attr-defined]
    assert {created[0][0].id, created[2][0].id} <= set(seen)  # type: ignore[attr-defined]
