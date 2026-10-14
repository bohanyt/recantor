"""Reusable PostgreSQL fixtures. This module is not a test module."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from meeting_intelligence_fixtures import FIXTURE_SESSION_ID, SEGMENT_SPECS
from sqlalchemy import delete
from sqlalchemy.engine.url import make_url

from alembic import command
from recantor.db import get_engine, get_sessionmaker
from recantor.meeting_essence.persistence.repository import MeetingIntelligenceRepository
from recantor.models import RecordingSession, SessionKind, SessionState, TranscriptSegment
from recantor.settings import get_settings

API_ROOT = Path(__file__).resolve().parents[1]
SESSION_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail("DATABASE_URL is required; this file has no hardcoded database default")
    return url


def cluster_owned() -> bool:
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


def pg_is_running() -> bool:
    return _pg_ctl("status").returncode == 0


def start_pg() -> None:
    if not cluster_owned():
        return
    if pg_is_running():
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
    if result.returncode != 0 or not pg_is_running():
        pytest.fail(
            "failed to start the disposable PostgreSQL cluster this fixture owns\n"
            f"pg_ctl exit {result.returncode}\n"
            f"server log:\n{_read_server_log(log_path)}"
        )


def stop_pg() -> None:
    if not cluster_owned():
        return
    if pg_is_running():
        _pg_ctl("stop", "-m", "fast")


def require_owned_cluster() -> None:
    if not cluster_owned():
        pytest.skip(
            "process and PostgreSQL restart proof runs on the disposable cluster "
            "identified by RECANTOR_PG_OWNED=1"
        )


def alembic_config() -> Config:
    os.environ["DATABASE_URL"] = database_url()
    get_settings.cache_clear()
    return Config(str(API_ROOT / "alembic.ini"))


def shift_uuid(value: str) -> str:
    return value.replace("11111111", "12121212")


def shifted_specs() -> list[tuple[str, int, int, int, str]]:
    return [
        (shift_uuid(segment_id), sequence, start_ms, end_ms, text)
        for segment_id, sequence, start_ms, end_ms, text in SEGMENT_SPECS
    ]


async def ensure_session(db, session_id: UUID) -> None:  # type: ignore[no-untyped-def]
    if await db.get(RecordingSession, session_id) is None:
        db.add(
            RecordingSession(
                id=session_id,
                client_request_id=uuid4(),
                kind=SessionKind.LIVE.value,
                state=SessionState.COMPLETE.value,
            )
        )


async def ensure_segments(
    db,  # type: ignore[no-untyped-def]
    session_id: UUID,
    specs: list[tuple[str, int, int, int, str]],
) -> None:
    for segment_id, sequence, start_ms, end_ms, segment_text in specs:
        if await db.get(TranscriptSegment, UUID(segment_id)) is None:
            await db.execute(
                delete(TranscriptSegment).where(
                    TranscriptSegment.session_id == session_id,
                    TranscriptSegment.sequence == sequence,
                )
            )
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


@pytest.fixture(scope="session")
def synthetic_postgresql():
    """Use DATABASE_URL as-is. Start and stop PostgreSQL only when this process owns it."""
    url = database_url()
    owned = cluster_owned()
    if owned:
        start_pg()
    try:
        yield url
    finally:
        if owned:
            stop_pg()
            assert not pg_is_running()


@pytest.fixture(scope="module")
def mi_db_migrated(synthetic_postgresql: str):
    os.environ["DATABASE_URL"] = synthetic_postgresql
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    command.upgrade(alembic_config(), "head")
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
async def mi_db(mi_db_migrated: None, synthetic_postgresql: str, monkeypatch: pytest.MonkeyPatch):
    del mi_db_migrated
    monkeypatch.setenv("DATABASE_URL", synthetic_postgresql)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    async with get_sessionmaker()() as db:
        await ensure_session(db, FIXTURE_SESSION_ID)
        await ensure_session(db, SESSION_B)
        await ensure_segments(db, FIXTURE_SESSION_ID, SEGMENT_SPECS)
        await ensure_segments(db, SESSION_B, shifted_specs())
        await db.commit()
    yield
    await get_engine().dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    async with get_sessionmaker()() as db:
        repo = MeetingIntelligenceRepository(db)
        await repo.delete_session_intelligence(FIXTURE_SESSION_ID)
        await repo.delete_session_intelligence(SESSION_B)
        await db.commit()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def migration_database_url(base_url: str, database: str) -> str:
    url = make_url(base_url).set(database=database)
    return url.render_as_string(hide_password=False)
