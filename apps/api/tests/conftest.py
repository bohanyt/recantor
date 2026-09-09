import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import delete

from recantor.db import Base, get_engine, get_sessionmaker
from recantor.models import RecordingChunk, RecordingGap, RecordingSession
from recantor.recording import get_audio_storage
from recantor.settings import get_settings


@pytest.fixture
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

    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
