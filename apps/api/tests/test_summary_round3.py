"""Negative proofs for #73 correction round 3."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from meeting_intelligence_db import alembic_config
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_meeting_essence_scheduler import SESSION_A, SESSION_B, SESSION_C, _seed_text

from alembic import command
from recantor.db import get_engine, get_sessionmaker
from recantor.main import app
from recantor.meeting_essence.scheduling.host_authority import reset_for_tests
from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
from recantor.meeting_essence.scheduling.jobs import (
    SummaryRetryDenied,
    SummarySubmitStatus,
    cancel_summary_dispatch,
    config_fingerprint,
    execute_next_summary_job,
    reconcile_summary_work,
    submit_summary_work,
)
from recantor.meeting_essence.scheduling.models import (
    MIHostInvocationLease,
    MISummaryDispatch,
    MISummaryDispatchStatus,
    MISummaryRequestReservation,
    MISummarySessionState,
)
from recantor.meeting_essence.scheduling.process_liveness import identity_for_pid
from recantor.meeting_essence.scheduling.provider_factory import (
    SummaryProviderFactory,
    set_summary_provider_factory,
)
from recantor.models import (
    RecordingGap,
    RecordingSession,
    SessionState,
    STTJob,
    STTJobState,
    TranscriptionUtterance,
    TranscriptSegment,
)
from recantor.settings import get_settings


async def _point_at_session_a(db) -> None:
    marker = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa0")
    await db.execute(
        text(
            "INSERT INTO mi_summary_scan_cursor (id, canonical_after, state_after) "
            "VALUES (1, :sid, :sid) "
            "ON CONFLICT (id) DO UPDATE SET canonical_after = :sid, state_after = :sid"
        ),
        {"sid": marker},
    )


def _low_id(index: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{index:012d}")


def _owned_ids() -> list[UUID]:
    return [_low_id(index) for index in range(1, 33)] + [
        UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
        SESSION_A,
        SESSION_B,
        SESSION_C,
        UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
    ]


@pytest.fixture(autouse=True)
async def _round3_clean(mi_db):
    del mi_db
    set_summary_provider_factory(None)
    reset_for_tests()
    async with get_sessionmaker()() as db, db.begin():
        await db.execute(
            delete(TranscriptSegment).where(
                TranscriptSegment.session_id.in_([_low_id(index) for index in range(1, 33)])
            )
        )
        await _reset_scheduler_rows(db)
    yield
    set_summary_provider_factory(None)
    async with get_sessionmaker()() as db, db.begin():
        owned = _owned_ids()
        await db.execute(delete(STTJob).where(STTJob.session_id.in_(owned)))
        await db.execute(
            delete(TranscriptionUtterance).where(TranscriptionUtterance.session_id.in_(owned))
        )
        await db.execute(delete(RecordingGap).where(RecordingGap.session_id.in_(owned)))
        await db.execute(delete(TranscriptSegment).where(TranscriptSegment.session_id.in_(owned)))
        await _reset_scheduler_rows(db)


async def _reset_scheduler_rows(db) -> None:
    await db.execute(delete(MISummaryRequestReservation))
    await db.execute(delete(MISummaryDispatch))
    await db.execute(delete(MISummarySessionState))
    await db.execute(
        text(
            """
            INSERT INTO mi_summary_session_states (
                session_id, observed_publication_high_water,
                highest_processed_publication, work_intent
            )
            SELECT id, 0, 0, 'cancelled'
            FROM recording_sessions
            ON CONFLICT (session_id) DO UPDATE
            SET work_intent = 'cancelled'
            """
        )
    )
    await db.execute(
        update(MIHostInvocationLease)
        .where(MIHostInvocationLease.id == 1)
        .values(
            holder_token=None,
            holder_pid=None,
            holder_pid_created_ms=None,
            holder_start_ticks=None,
            holder_entrypoint=None,
            lease_expires_at=None,
            acquired_at=None,
            active_invocations=0,
            holder_state="free",
            owned_runtime_json="[]",
            termination_acked=False,
            quarantine_reason=None,
            invocation_admitted=False,
            spawns_process_tree=False,
        )
    )
    await db.execute(text("DELETE FROM mi_summary_metrics"))
    await db.execute(
        text("UPDATE mi_summary_scan_cursor SET canonical_after = NULL, state_after = NULL")
    )


@pytest.mark.asyncio
async def test_cancelled_prefix_cannot_hide_a_later_session(mi_db, monkeypatch) -> None:
    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    get_settings.cache_clear()
    for index in range(1, 33):
        await _seed_text(_low_id(index), [f"Batal {index}."])
    eligible = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    await _seed_text(eligible, ["Yang harus jalan."])
    fingerprint = config_fingerprint()
    async with get_sessionmaker()() as db, db.begin():
        for index in range(1, 33):
            db.add(
                MISummarySessionState(
                    session_id=_low_id(index),
                    observed_publication_high_water=1,
                    highest_processed_publication=0,
                    work_intent="blocked",
                    desired_config_fingerprint=fingerprint,
                )
            )
        await db.execute(
            text("UPDATE mi_summary_scan_cursor SET canonical_after = NULL, state_after = NULL")
        )
    found = False
    for _ in range(6):
        async with get_sessionmaker()() as db:
            await reconcile_summary_work(db)
            await db.commit()
            dispatch = await db.scalar(
                select(MISummaryDispatch.id).where(
                    MISummaryDispatch.session_id == eligible,
                    MISummaryDispatch.status == MISummaryDispatchStatus.PENDING.value,
                )
            )
        if dispatch is not None:
            found = True
            break
    assert found is True
    async with get_sessionmaker()() as db:
        blocked_open = int(
            await db.scalar(
                select(func.count())
                .select_from(MISummaryDispatch)
                .where(
                    MISummaryDispatch.session_id.in_([_low_id(index) for index in range(1, 33)]),
                    MISummaryDispatch.status.in_(
                        [
                            MISummaryDispatchStatus.PENDING.value,
                            MISummaryDispatchStatus.CLAIMED.value,
                        ]
                    ),
                )
            )
            or 0
        )
    assert blocked_open == 0


@pytest.mark.asyncio
async def test_same_boundary_changes_enqueue_once(mi_db, monkeypatch) -> None:
    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("MEETING_ESSENCE_REQUESTED_MODEL", "model-a")
    get_settings.cache_clear()
    await _seed_text(SESSION_A, ["Satu kalimat."])
    async with get_sessionmaker()() as db, db.begin():
        session = await db.get(RecordingSession, SESSION_A)
        assert session is not None
        session.state = SessionState.COMPLETE.value
        session.final_sequence = 1
        session.audio_completeness = "full"
        await _point_at_session_a(db)
    async with get_sessionmaker()() as db:
        first = await reconcile_summary_work(db)
        await db.commit()
    assert first.created == 1
    async with get_sessionmaker()() as db, db.begin():
        state = await db.get(MISummarySessionState, SESSION_A)
        assert state is not None
        assert state.finalize_epoch >= 1
        state.highest_processed_publication = state.observed_publication_high_water
        state.processed_config_fingerprint = state.desired_config_fingerprint
        state.processed_config_epoch = state.desired_config_epoch
        state.processed_source_fingerprint = state.desired_source_fingerprint
        state.processed_coverage_fingerprint = state.desired_coverage_fingerprint
        state.processed_stt_fingerprint = state.desired_stt_fingerprint
        state.processed_finalize_epoch = state.finalize_epoch
        await db.execute(
            text("DELETE FROM mi_summary_dispatches WHERE session_id = :sid"),
            {"sid": SESSION_A},
        )
        await db.execute(
            text("DELETE FROM mi_summary_request_reservations WHERE session_id = :sid"),
            {"sid": SESSION_A},
        )
    async with get_sessionmaker()() as db:
        await _point_at_session_a(db)
        quiet = await reconcile_summary_work(db)
        await db.commit()
    assert quiet.created == 0
    utterance_id = uuid4()
    async with get_sessionmaker()() as db, db.begin():
        db.add(
            TranscriptionUtterance(
                id=utterance_id,
                session_id=SESSION_A,
                sequence=1,
                producer_key="utt-round3",
                start_ms=0,
                end_ms=400,
                content_type="audio/wav",
                sha256="b" * 64,
                byte_length=8,
                storage_key="tests/round3.wav",
            )
        )
        await db.flush()
        db.add(
            STTJob(utterance_id=utterance_id, session_id=SESSION_A, state=STTJobState.PENDING.value)
        )
    async with get_sessionmaker()() as db:
        await _point_at_session_a(db)
        stt_open = await reconcile_summary_work(db)
        await db.commit()
        stt_repeat = await reconcile_summary_work(db)
        await db.commit()
    assert stt_open.created == 1
    assert stt_repeat.created == 0
    async with get_sessionmaker()() as db:
        before_stt = await db.scalar(
            select(MISummarySessionState.desired_stt_fingerprint).where(
                MISummarySessionState.session_id == SESSION_A
            )
        )
    async with get_sessionmaker()() as db, db.begin():
        job = await db.scalar(select(STTJob).where(STTJob.session_id == SESSION_A))
        assert job is not None
        job.state = STTJobState.SUCCEEDED.value
    async with get_sessionmaker()() as db:
        await _point_at_session_a(db)
        stt_done = await reconcile_summary_work(db)
        await db.commit()
        after_stt = await db.scalar(
            select(MISummarySessionState.desired_stt_fingerprint).where(
                MISummarySessionState.session_id == SESSION_A
            )
        )
    assert stt_done.created == 0
    assert after_stt != before_stt
    async with get_sessionmaker()() as db, db.begin():
        state = await db.get(MISummarySessionState, SESSION_A)
        assert state is not None
        state.processed_stt_fingerprint = state.desired_stt_fingerprint
        await db.execute(
            text("DELETE FROM mi_summary_dispatches WHERE session_id = :sid"),
            {"sid": SESSION_A},
        )
        await db.execute(
            text("DELETE FROM mi_summary_request_reservations WHERE session_id = :sid"),
            {"sid": SESSION_A},
        )
    async with get_sessionmaker()() as db:
        await _point_at_session_a(db)
        settled = await reconcile_summary_work(db)
        await db.commit()
    assert settled.created == 0
    monkeypatch.setenv("MEETING_ESSENCE_REQUESTED_MODEL", "model-b")
    get_settings.cache_clear()
    async with get_sessionmaker()() as db:
        await _point_at_session_a(db)
        changed = await reconcile_summary_work(db)
        await db.commit()
        unchanged = await reconcile_summary_work(db)
        await db.commit()
    assert changed.created == 1
    assert unchanged.created == 0
    async with get_sessionmaker()() as db, db.begin():
        state = await db.get(MISummarySessionState, SESSION_A)
        assert state is not None
        state.processed_config_fingerprint = state.desired_config_fingerprint
        state.processed_config_epoch = state.desired_config_epoch
        await db.execute(
            text("DELETE FROM mi_summary_dispatches WHERE session_id = :sid"),
            {"sid": SESSION_A},
        )
        await db.execute(
            text("DELETE FROM mi_summary_request_reservations WHERE session_id = :sid"),
            {"sid": SESSION_A},
        )
    async with get_sessionmaker()() as db:
        await _point_at_session_a(db)
        quiet_config = await reconcile_summary_work(db)
        await db.commit()
    assert quiet_config.created == 0


@pytest.mark.asyncio
async def test_concurrent_admission_is_serialized(mi_db, monkeypatch) -> None:
    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_QUEUE_MAX_DEPTH", "1")
    get_settings.cache_clear()
    await _seed_text(SESSION_A, ["A"])
    await _seed_text(SESSION_C, ["C"])

    async def _submit(session_id: UUID, fingerprint: str) -> SummarySubmitStatus:
        async with get_sessionmaker()() as db:
            result = await submit_summary_work(
                db,
                session_id=session_id,
                client_request_id=uuid4(),
                payload_fingerprint=fingerprint,
            )
            await db.commit()
            return result.status

    first, second = await asyncio.gather(
        _submit(SESSION_A, "a" * 64),
        _submit(SESSION_C, "c" * 64),
    )
    assert {first, second} == {SummarySubmitStatus.ACCEPTED, SummarySubmitStatus.QUEUE_FULL}
    accepted = SESSION_A if first == SummarySubmitStatus.ACCEPTED else SESSION_C
    promoted = await _submit(accepted, "b" * 64)
    assert promoted == SummarySubmitStatus.COALESCED
    async with get_sessionmaker()() as db:
        open_rows = int(
            await db.scalar(
                select(func.count())
                .select_from(MISummaryDispatch)
                .where(
                    MISummaryDispatch.status.in_(
                        [
                            MISummaryDispatchStatus.PENDING.value,
                            MISummaryDispatchStatus.CLAIMED.value,
                        ]
                    )
                )
            )
            or 0
        )
        state = await db.get(MISummarySessionState, accepted)
    assert open_rows == 1
    assert state is not None
    assert state.desired_config_fingerprint == "b" * 64
    assert int(state.desired_config_epoch) >= 1


@pytest.mark.asyncio
async def test_wrong_database_does_not_start_a_second_process(mi_db) -> None:
    del mi_db
    admin_url = os.environ["DATABASE_URL"]
    probe = "recantor_issue73_authority_probe"
    probe_url = make_url(admin_url).set(database=probe).render_as_string(hide_password=False)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    other = None
    handle = None
    permit = HostInvocationPermit()
    try:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": probe},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{probe}"'))
            connection.execute(text(f'CREATE DATABASE "{probe}"'))
        previous = os.environ["DATABASE_URL"]
        os.environ["DATABASE_URL"] = probe_url
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
        command.upgrade(alembic_config(), "head")
        os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
        other = create_async_engine(probe_url)
        handle = await permit.acquire(entrypoint="authority-owner")
        assert await permit.register_owned_runtime(handle, [identity_for_pid(child.pid)]) is True
        foreign = HostInvocationPermit(
            sessionmaker=async_sessionmaker(other, expire_on_commit=False)
        )
        assert await foreign.try_acquire(entrypoint="wrong-database") is None
        assert child.poll() is None
        async with other.connect() as connection:
            held = await connection.scalar(
                text(
                    "SELECT count(*) FROM mi_host_invocation_lease "
                    "WHERE holder_token IS NOT NULL OR active_invocations <> 0"
                )
            )
        assert int(held or 0) == 0
        assert await permit.read_active_witness() == 1
        child.kill()
        child.wait(timeout=5)
        assert await permit.acknowledge_termination(handle) is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if handle is not None:
            await permit.release(handle)
        if other is not None:
            await other.dispose()
        admin.dispose()
        os.environ["DATABASE_URL"] = admin_url
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
        with create_engine(admin_url, isolation_level="AUTOCOMMIT").connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{probe}"'))


@pytest.mark.asyncio
async def test_registration_failure_does_not_resume_the_child(mi_db, tmp_path: Path) -> None:
    del mi_db
    marker = tmp_path / "ran.txt"
    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="register-gate")
    await permit.release(handle)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,pathlib; sys.stdin.readline(); pathlib.Path(sys.argv[1]).write_text('x')",
            str(marker),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        registered = await permit.register_owned_runtime(handle, [identity_for_pid(proc.pid)])
        assert registered is False
        assert proc.poll() is None
        assert not marker.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_provider_build_failure_releases_the_permit(mi_db, monkeypatch) -> None:
    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    get_settings.cache_clear()
    await _seed_text(SESSION_A, ["Bangun gagal."])

    class _BadFactory(SummaryProviderFactory):
        adapter_id = "bad"

        def build(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("build failed")

    set_summary_provider_factory(_BadFactory())
    try:
        async with get_sessionmaker()() as db:
            await reconcile_summary_work(db)
            await db.commit()
            with pytest.raises(RuntimeError, match="build failed"):
                await execute_next_summary_job(db, entrypoint="pytest-build-fail")
        permit = HostInvocationPermit()
        assert await permit.read_active_witness() == 0
    finally:
        set_summary_provider_factory(None)


@pytest.mark.asyncio
async def test_cancel_returns_a_blocked_provider_and_retry_of_claim_is_refused(
    mi_db, monkeypatch
) -> None:
    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    get_settings.cache_clear()
    session_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    await _seed_text(session_id, ["Jangan lanjut."])
    started = asyncio.Event()

    class _Blocking:
        spawns_process_tree = False

        async def run_stage(self, **kwargs: object) -> None:
            del kwargs
            started.set()
            await asyncio.sleep(30)

    async def _run() -> object:
        async with get_sessionmaker()() as db:
            return await execute_next_summary_job(
                db, entrypoint="pytest-cancel", provider=_Blocking()
            )

    async with get_sessionmaker()() as db, db.begin():
        from recantor.meeting_essence.scheduling.triggers import note_transcript_publication

        await note_transcript_publication(db, session_id=session_id, publication_sequence=1)
    queued = False
    for _ in range(8):
        async with get_sessionmaker()() as db:
            await reconcile_summary_work(db)
            await db.commit()
            queued = (
                await db.scalar(
                    select(MISummaryDispatch.id).where(
                        MISummaryDispatch.session_id == session_id,
                        MISummaryDispatch.status == MISummaryDispatchStatus.PENDING.value,
                    )
                )
                is not None
            )
        if queued:
            break
    assert queued is True
    task = asyncio.create_task(_run())
    await asyncio.wait_for(started.wait(), timeout=10)
    async with get_sessionmaker()() as db, db.begin():
        dispatch_id = await db.scalar(
            select(MISummaryDispatch.id).where(MISummaryDispatch.session_id == session_id)
        )
        with pytest.raises(SummaryRetryDenied):
            from recantor.meeting_essence.scheduling.jobs import retry_summary_dispatch

            await retry_summary_dispatch(db, dispatch_id=dispatch_id, session_id=session_id)
        await cancel_summary_dispatch(db, dispatch_id=dispatch_id, session_id=session_id)
    outcome = await asyncio.wait_for(task, timeout=10)
    assert outcome.status.value == "cancelled"
    permit = HostInvocationPermit()
    assert await permit.read_active_witness() == 0


@pytest.mark.asyncio
async def test_http_stop_does_not_wait_on_summary_sql(mi_db) -> None:
    del mi_db
    writer = "writer-stop-round3"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/sessions/live",
            json={
                "client_request_id": str(uuid4()),
                "writer_id": writer,
                "recovery_token": f"recantor-recovery-capability::{writer}::phase1c",
            },
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        payload = b"abc"
        import hashlib

        digest = hashlib.sha256(payload).hexdigest()
        chunk = await client.put(
            f"/api/v1/sessions/{session_id}/chunks/1",
            params={
                "writer_id": writer,
                "capture_epoch": 1,
                "monotonic_start_ms": 0,
                "monotonic_end_ms": 200,
                "sha256": digest,
                "content_type": "audio/webm;codecs=opus",
            },
            content=payload,
            headers={"content-type": "application/octet-stream"},
        )
        assert chunk.status_code == 200, chunk.text

        async def _stalled(*args: object, **kwargs: object) -> None:
            del args, kwargs
            await asyncio.sleep(2)

        from recantor.meeting_essence.scheduling import triggers

        original = triggers._write_finalized
        triggers._write_finalized = _stalled  # type: ignore[assignment]
        try:
            started_at = time.monotonic()
            stopped = await client.post(
                f"/api/v1/sessions/{session_id}/finalize",
                json={
                    "writer_id": writer,
                    "capture_epoch": 1,
                    "final_sequence": 1,
                    "final_monotonic_end_ms": 200,
                    "gap_sequences": [],
                },
            )
            elapsed = time.monotonic() - started_at
        finally:
            triggers._write_finalized = original  # type: ignore[assignment]
    assert stopped.status_code == 200, stopped.text
    assert elapsed < 0.5
    assert stopped.json()["complete"] is True
    async with get_sessionmaker()() as db:
        await reconcile_summary_work(db)
        await db.commit()
        state = await db.get(MISummarySessionState, UUID(session_id))
    assert state is not None
    assert state.audio_final_sequence == 1
    assert state.frozen_transcript_sequence == 0
    assert state.frozen_artifact_publication == 0
