"""Seed the public S01–S06 canonical session for the #74 development API.

This is the login-free fixture path #75 can point at a running API. It does not
start Codex, Groq, or any other model. The deterministic provider is required
and is never the default for a normal API process.

Refuses the shared ``recantor_test`` database. Alembic upgrade runs only when
``RECANTOR_PG_OWNED=1`` and the URL database is an owned issue database.
"""

from __future__ import annotations

import asyncio
import os
import sys

from meeting_intelligence_db import (
    FIXTURE_SESSION_ID,
    SEGMENT_SPECS,
    SESSION_B,
    alembic_config,
    ensure_segments,
    ensure_session,
    shifted_specs,
)
from sqlalchemy.engine.url import make_url

from alembic import command
from recantor.db import get_engine, get_sessionmaker
from recantor.meeting_essence.scheduling.triggers import note_transcript_publication
from recantor.settings import get_settings

_SHARED = "recantor_test"


def _database_name() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is required")
    name = make_url(url).database or ""
    if name == _SHARED:
        raise SystemExit("refusing to migrate or seed shared recantor_test")
    return name


def _require_explicit_fixture() -> None:
    kind = os.environ.get("RECANTOR_MEETING_ESSENCE_PROVIDER", "").strip().casefold()
    fixture = os.environ.get("RECANTOR_MEETING_ESSENCE_FIXTURE", "").strip()
    if kind != "deterministic" or not fixture:
        raise SystemExit(
            "set RECANTOR_MEETING_ESSENCE_PROVIDER=deterministic and "
            "RECANTOR_MEETING_ESSENCE_FIXTURE to the public JSON fixture"
        )


async def _seed() -> None:
    async with get_sessionmaker()() as db:
        await ensure_session(db, FIXTURE_SESSION_ID)
        await ensure_session(db, SESSION_B)
        await ensure_segments(db, FIXTURE_SESSION_ID, SEGMENT_SPECS)
        await ensure_segments(db, SESSION_B, shifted_specs())
        await note_transcript_publication(db, session_id=FIXTURE_SESSION_ID, publication_sequence=6)
        await db.commit()
    await get_engine().dispose()


def main() -> None:
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    name = _database_name()
    _require_explicit_fixture()
    if os.environ.get("RECANTOR_PG_OWNED") == "1":
        if not name.startswith("recantor_issue"):
            raise SystemExit(f"refusing owned migration of {name}")
        get_settings.cache_clear()
        command.upgrade(alembic_config(), "head")
    asyncio.run(_seed())
    session = FIXTURE_SESSION_ID
    print(f"seeded_session={session}")
    print(f"database={name}")
    print(f"POST /api/v1/sessions/{session}/meeting-essence/derivations")
    print(f"GET /api/v1/sessions/{session}/meeting-essence/derivations/{{dispatch_id}}")
    print(
        "GET /api/v1/sessions/"
        f"{session}/meeting-essence/artifacts/{{artifact_id}}/results/{{layer}}"
    )
    print("provider=deterministic fixture-only; no model call")


if __name__ == "__main__":
    sys.exit(main())
