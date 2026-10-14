import asyncio
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, text

from recantor.db import Base, get_engine, get_sessionmaker
from recantor.models import RecordingChunk, RecordingGap, RecordingSession
from recantor.recording import get_audio_storage
from recantor.settings import get_settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytest_plugins = ["meeting_intelligence_db"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    marker = pytest.mark.skip(reason="#45 media proofs require ffmpeg and ffprobe runtime")
    for item in items:
        if item.fspath.basename == "test_media_processing.py":
            item.add_marker(marker)


@pytest_asyncio.fixture
async def clean_recording_state(tmp_path: Path) -> AsyncIterator[None]:
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    settings = get_settings()
    previous_storage_path = settings.audio_storage_path
    settings.audio_storage_path = str(tmp_path / "audio")
    get_audio_storage.cache_clear()

    async with get_sessionmaker()() as db:
        await db.execute(delete(RecordingGap))
        await db.execute(delete(RecordingChunk))
        await db.execute(delete(RecordingSession))
        await db.commit()

    yield

    root = Path(settings.audio_storage_path).expanduser().resolve()
    shutil.rmtree(root, ignore_errors=True)
    get_audio_storage.cache_clear()
    settings.audio_storage_path = previous_storage_path

    from meeting_intelligence_db import cluster_owned

    # The owned disposable cluster keeps the Alembic-migrated schema for the
    # whole session. A hosted service database drops tables and the stamp
    # together so the next module migrates instead of trusting a stale head.
    if not cluster_owned():
        async with get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del session, exitstatus
    try:
        asyncio.get_running_loop()
        loop_running = True
    except RuntimeError:
        loop_running = False
    if get_engine.cache_info().currsize and not loop_running:
        get_engine().sync_engine.dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
